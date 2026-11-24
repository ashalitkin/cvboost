"""Round 2: prefilter terms from rules.yaml, shared ATS budget, backoff, --limit, RecursionError."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

from cvboost import ats_state
from cvboost.collect import collect
from cvboost.connectors.ats import (
    ATS_CYCLE_BUDGET_S,
    BASE_TERMS,
    UNWANTED_TERMS,
    ATSConnector,
    build_title_re,
    profile_title_terms,
    title_matches,
)
from cvboost.connectors.base import FetchOutcome
from cvboost.connectors.lever import LeverConnector
from cvboost.core.rules import load_config
from cvboost.worker import run_cycle
from tests.ats.helpers import ats_http, fixture_response, write_companies
from tests.conftest import NOW
from tests.worker.helpers import FakeClock, FakeConnector, ScoreRecorder, make_deps

RULES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "config" / "rules.yaml"
PROFILE_TERMS = sorted(set(profile_title_terms(RULES)))


@pytest.fixture(autouse=True)
def _no_jitter(monkeypatch):
    monkeypatch.setattr("cvboost.http.random.uniform", lambda a, b: 1.0)
    monkeypatch.setattr("cvboost.ats_state.random.uniform", lambda a, b: 1.0)


def state(store, ats, slug):
    return store.conn.execute(
        "SELECT * FROM ats_companies WHERE ats=? AND slug=?", (ats, slug)
    ).fetchone()


# ---- 1. prefilter built from rules.yaml ----


def test_the_rules_file_has_profile_terms():
    assert len(PROFILE_TERMS) > 30 and "analytics engineer" in PROFILE_TERMS


@pytest.mark.parametrize("term", [t for t in PROFILE_TERMS if t not in UNWANTED_TERMS])
def test_every_profile_title_term_passes_the_prefilter(term):
    rx = build_title_re(PROFILE_TERMS)
    assert title_matches(f"Senior {term.title()} Lead", rx)
    assert title_matches(term, rx)
    # and the connector built from the file does the same
    assert title_matches(f"Senior {term} Lead", LeverConnector(rules_path=RULES)._title_re)


@pytest.mark.parametrize(
    "title",
    [
        "DataOps Engineer", "DWH Developer", "ELT Engineer", "Snowflake Architect",
        "Attribution Analyst", "Measurement Lead", "Conversion Specialist", "LLM Engineer",
        "Agentic Systems Engineer", "MLOps Engineer", "LookML Developer", "Reporting Lead",
        "Analytical Engineer", "Machine-Learning Engineer", "Machine Learning Engineer",
        "Tableau Developer", "Marketing Technologist",
    ],
)  # fmt: skip
def test_requested_terms_pass(title):
    assert title_matches(title)
    assert title_matches(title, LeverConnector(rules_path=RULES)._title_re)


@pytest.mark.parametrize(
    "title",
    [
        "Database Administrator", "Accountant", "Maintenance Technician", "Trainer",
        "Senior Software Engineer", "PPC Specialist", "Paid Media Buyer",
        "Google Ads Specialist", "SEM Manager",
    ],
)  # fmt: skip
def test_unwanted_titles_stay_out(title):
    assert not title_matches(title)
    assert not title_matches(title, LeverConnector(rules_path=RULES)._title_re)


def test_paid_terms_in_rules_are_not_added(tmp_path):
    cfg = load_config(RULES)
    # after #14 the campaign-operator terms moved out of the profiles into unwanted_titles
    assert not set(PROFILE_TERMS) & {"ppc", "paid media", "paid search", "paid social", "sem"}
    assert "ppc" in " ".join(cfg.unwanted_titles)
    assert "ppc" in UNWANTED_TERMS and "ppc" not in BASE_TERMS
    assert not title_matches("PPC Specialist", build_title_re(PROFILE_TERMS))


def test_broken_rules_file_keeps_the_base_set(tmp_path):
    bad = tmp_path / "rules.yaml"
    bad.write_text("not: [valid")
    c = LeverConnector(rules_path=bad)
    assert title_matches("Data Engineer", c._title_re)
    assert profile_title_terms(tmp_path / "missing.yaml") == []


# ---- 2. shared budget, ATS last ----


class SlowATS(ATSConnector):
    """Fake ATS whose run takes as long as its budget allows (up to 300 s of fake time)."""

    kind: ClassVar = "ats"
    base_url: ClassVar[str] = "https://example.com/x"
    allowed_hosts: ClassVar = frozenset({"example.com"})
    tos_note: ClassVar[str] = ""
    attribution: ClassVar[str] = ""
    clock: FakeClock
    budgets: list

    def __init__(self, name, clock, budgets):
        super().__init__()
        self.name = name  # type: ignore[misc]
        self.clock, self.budgets = clock, budgets

    def company_url(self, slug):
        return ""

    def postings(self, data):
        return []

    def posting_id(self, item):
        return None

    def posting_title(self, item):
        return None

    def normalise(self, raw, fetched_at):
        raise NotImplementedError

    def before_run(self, store, now):
        self._batch = ["x"]

    def after_run(self, store, now):
        pass

    def fetch_partial(self, http, limit=None):
        self.budgets.append(self.cycle_budget_s)
        took = min(self.cycle_budget_s if self.cycle_budget_s is not None else 300, 300)
        self.clock.advance(seconds=took)
        return FetchOutcome([])


def test_ats_share_one_60s_budget_and_run_after_the_other_sources(tmp_path):
    from cvboost.storage.db import Store, connect, migrate

    store = Store(connect(tmp_path / "s.sqlite"))
    migrate(store.conn)
    clock, budgets, order = FakeClock(), [], []
    ats = [SlowATS(n, clock, budgets) for n in ("ats_a", "ats_b", "ats_c")]
    plain = FakeConnector("plain", ids=["1"])
    orig = plain.fetch_partial
    plain.fetch_partial = lambda http, limit=None: (order.append("plain"), orig(http, limit))[1]
    conns = [*ats, plain]  # registry order puts the ATS first: collect must still run them last
    store.sync_sources({c.name: type(c) for c in conns}, NOW)
    t0 = clock()
    res = collect(store, None, conns, limit=None, force=True, clock=clock,
                  deadline=t0 + timedelta(seconds=360))  # fmt: skip
    spent = (clock() - t0).total_seconds()
    assert order == ["plain"] and plain.fetches == 1
    assert budgets[0] == pytest.approx(ATS_CYCLE_BUDGET_S, abs=1)
    assert spent <= ATS_CYCLE_BUDGET_S + 1
    assert [r.skip_reason for r in res if r.source in ("ats_b", "ats_c")] == ["deferred"] * 2


def test_ats_budget_is_capped_by_the_collect_deadline(tmp_path):
    from cvboost.storage.db import Store, connect, migrate

    store = Store(connect(tmp_path / "s.sqlite"))
    migrate(store.conn)
    clock, budgets = FakeClock(), []
    a = SlowATS("ats_a", clock, budgets)
    store.sync_sources({"ats_a": SlowATS}, NOW)
    collect(store, None, [a], limit=None, force=True, clock=clock,
            deadline=clock() + timedelta(seconds=25))  # fmt: skip
    assert budgets[0] == pytest.approx(25, abs=1)


def test_the_real_ats_connectors_use_the_shared_budget_constant():
    assert ATS_CYCLE_BUDGET_S == 60.0


def test_worker_cycle_with_slow_ats_stays_inside_collect_budget_and_still_scores(tmp_path):
    from cvboost.storage.db import Store, connect, migrate

    store = Store(connect(tmp_path / "w.sqlite"))
    migrate(store.conn)
    clock, budgets = FakeClock(), []
    ats = [SlowATS(n, clock, budgets) for n in ("ats_a", "ats_b", "ats_c")]
    plain = FakeConnector("plain", ids=["1", "2"])
    conns = [*ats, plain]
    store.sync_sources({c.name: type(c) for c in conns}, NOW)
    rec = ScoreRecorder(scored=2, calls=1)
    deps = make_deps(tmp_path, conns, clock=clock, score_fn=rec)
    t0 = clock()
    st = run_cycle(store, deps)
    cfg = deps.cfg
    assert st.new_jobs == 2 and st.sources_deferred == 2
    assert (store.state_get_dt("heartbeat") - t0).total_seconds() <= cfg.collect_budget_s
    assert len(rec.calls) == 1 and st.score_stopped is None  # score was not skipped
    assert sum(1 for b in budgets if b is not None) == len(budgets) == 1


# ---- 3. host down: backoff, no empty ok runs ----


def test_backoff_30min_1h_then_6h(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("lever", "down", "Down")])
    seen: list[str] = []
    http = ats_http({"down": httpx.Response(503)}, seen)
    store.sync_sources({"lever": LeverConnector}, NOW)
    asked = []
    t = NOW
    for _ in range(4):  # fail, wait for its backoff, fail again
        collect(store, http, [LeverConnector(path)], limit=None, force=True, now=t)
        asked.append(t)
        due = store.conn.execute("SELECT next_due_at FROM ats_companies").fetchone()[0]
        t = __import__("datetime").datetime.fromisoformat(due)
    gaps = [b - a for a, b in zip(asked, asked[1:], strict=False)]
    assert gaps == [timedelta(minutes=30), timedelta(hours=1), timedelta(hours=6)]
    assert state(store, "lever", "down")["consecutive_failures"] == 4
    # it is not asked before its time
    n = len(seen)
    soon = t - timedelta(hours=5)  # 1 h after the last failure: its backoff is now 6 h
    collect(store, http, [LeverConnector(path)], limit=None, force=True, now=soon)
    assert len(seen) == n


def test_host_down_for_two_days_keeps_the_source_failing_and_alerts(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("lever", f"co-{i}", "x") for i in range(3)])
    http = ats_http({f"co-{i}": httpx.Response(503) for i in range(3)})
    store.sync_sources({"lever": LeverConnector}, NOW)
    results = []
    for k in range(6 * 24 * 2):  # worker ticks every 10 min for 48 h, NOT forced
        t = NOW + timedelta(minutes=10 * k)
        [r] = collect(store, http, [LeverConnector(path)], limit=None, force=False, now=t)
        results.append(r)
    assert not [r for r in results if r.status == "ok"]  # never a "healthy" run in between
    errors = [r for r in results if r.status == "error"]
    assert len(errors) >= 6
    st = store.source_state("lever")
    assert st.consecutive_failures >= 6  # source_failing alert threshold (worker.yaml: 6)
    # and the empty ticks wrote no run rows
    assert store.conn.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0] == len(errors)


def test_alert_fires_for_the_failing_ats_source(store, tmp_path):
    from cvboost.notify.alerts import queue_system_alerts
    from cvboost.worker_config import WorkerConfig

    path = write_companies(tmp_path / "c.yaml", [("lever", "co-0", "x")])
    http = ats_http({"co-0": httpx.Response(503)})
    store.sync_sources({"lever": LeverConnector}, NOW)
    for k in range(6 * 72):
        collect(store, http, [LeverConnector(path)], limit=None, force=False,
                now=NOW + timedelta(minutes=10 * k))  # fmt: skip
    h = {x.name: x for x in store.source_health()}["lever"]
    assert h.consecutive_failures >= 6
    assert (
        queue_system_alerts(store, WorkerConfig(), NOW, score_code=None) == 1
    )  # source_failing, once
    row = store.conn.execute("SELECT kind, text FROM system_alerts").fetchone()
    assert row["kind"] == "source_failing" and "lever" in row["text"]


# ---- 5. --limit, log, jitter helpers ----


def test_limit_that_cuts_a_company_does_not_mark_it_checked(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("lever", "findem", "F"), ("lever", "other", "O")])
    http = ats_http(
        {"findem": fixture_response("lever", "findem"), "other": httpx.Response(200, json=[])}
    )
    store.sync_sources({"lever": LeverConnector}, NOW)
    [res] = collect(store, http, [LeverConnector(path)], limit=2, force=True, now=NOW)
    assert res.found == 2
    s = state(store, "lever", "findem")
    assert s["last_ok_at"] is None and s["next_due_at"] is None  # still due, not "checked"
    assert state(store, "lever", "other")["last_ok_at"] is None  # not reached
    [res] = collect(
        store, http, [LeverConnector(path)], limit=None, force=True, now=NOW + timedelta(minutes=10)
    )
    assert res.found == 3
    assert state(store, "lever", "findem")["last_ok_at"] is not None


def test_time_budget_log_counts_the_companies_left(store, tmp_path, caplog):
    slugs = [f"co-{i}" for i in range(5)]
    path = write_companies(tmp_path / "c.yaml", [("lever", s, s) for s in slugs])
    t = [0.0]

    def tick():
        t[0] += 10.0
        return t[0]

    class Short(LeverConnector):
        time_budget_s = 25.0

    store.sync_sources({"lever": LeverConnector}, NOW)
    seen = []
    http = ats_http({s: httpx.Response(200, json=[]) for s in slugs}, seen)
    with caplog.at_level("INFO", logger="cvboost.ats"):
        collect(store, http, [Short(path, monotonic=tick)], limit=None, force=True, now=NOW)
    assert f"{5 - len(seen)} of 5 companies left" in caplog.text


def test_deeply_nested_json_is_a_company_failure_not_a_run_error(store, tmp_path):
    path = write_companies(
        tmp_path / "c.yaml", [("lever", "deep", "Deep"), ("lever", "findem", "F")]
    )
    deep = httpx.Response(200, content=b"[" * 200000 + b"]" * 200000)
    http = ats_http({"deep": deep, "findem": fixture_response("lever", "findem")})
    store.sync_sources({"lever": LeverConnector}, NOW)
    [res] = collect(store, http, [LeverConnector(path)], limit=None, force=True, now=NOW)
    assert res.status == "ok" and res.error_type == "partial" and res.found == 3
    assert state(store, "lever", "deep")["consecutive_failures"] == 1


def test_sync_runs_in_one_transaction(store, tmp_path, monkeypatch):
    path = write_companies(tmp_path / "c.yaml", [("lever", f"co-{i}", "x") for i in range(3)])
    calls = []
    real = ats_state.sync_companies

    def spy(st, companies, now):
        calls.append(st.conn.in_transaction)
        return real(st, companies, now)

    monkeypatch.setattr("cvboost.connectors.ats.ats_state.sync_companies", spy)
    store.sync_sources({"lever": LeverConnector}, NOW)
    collect(store, ats_http({}), [LeverConnector(path)], limit=None, force=True, now=NOW)
    assert calls == [True]


# ---- round 3: the rules-based prefilter is really used by parse() ------------------------------


def rules_with_extra_term(tmp_path):
    text = RULES.read_text()
    marker = "  profiles:\n    data_engineer:\n      title:\n"
    assert marker in text
    bad = tmp_path / "rules.yaml"
    bad.write_text(
        text.replace(
            marker, marker + "        - {weight: 0.5, terms: [experimentation scientist]}\n", 1
        )
    )
    return bad


def lever_body(*titles):
    import json

    return json.dumps(
        [
            {"id": f"id-{i}", "text": t, "hostedUrl": "https://jobs.lever.co/x/1"}
            for i, t in enumerate(titles)
        ]
    ).encode()


def test_parse_keeps_a_title_that_only_a_rules_profile_term_matches(tmp_path):
    body = lever_body("Experimentation Scientist", "Pastry Chef", "PPC Specialist", "Data Engineer")
    url = "https://api.lever.co/v0/postings/x?mode=json"
    plain = LeverConnector(rules_path=RULES)
    with_term = LeverConnector(rules_path=rules_with_extra_term(tmp_path))
    assert [r.payload["text"] for r in plain.parse(body, url)] == ["Data Engineer"]
    assert [r.payload["text"] for r in with_term.parse(body, url)] == [
        "Experimentation Scientist", "Data Engineer",
    ]  # fmt: skip


def test_parse_drops_ppc_specialist():
    body = lever_body("PPC Specialist", "Paid Media Buyer", "Senior Data Analyst")
    c = LeverConnector(rules_path=RULES)
    kept = [
        r.payload["text"] for r in c.parse(body, "https://api.lever.co/v0/postings/x?mode=json")
    ]
    assert kept == ["Senior Data Analyst"]


def test_inactive_company_failing_its_recheck_stays_on_the_30_day_schedule(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("lever", "dead", "D")])
    store.sync_sources({"lever": LeverConnector}, NOW)
    http404 = ats_http({})
    for h in (0, 7):
        collect(store, http404, [LeverConnector(path)], limit=None, force=True,
                now=NOW + timedelta(hours=h))  # fmt: skip
    assert state(store, "lever", "dead")["active"] == 0
    t = NOW + timedelta(days=31)
    collect(store, ats_http({"dead": httpx.Response(503)}), [LeverConnector(path)], limit=None,
            force=True, now=t)  # fmt: skip
    s = state(store, "lever", "dead")
    assert s["active"] == 0
    assert s["next_due_at"] == (t + ats_state.INACTIVE_RECHECK).isoformat()
