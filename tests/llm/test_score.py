from __future__ import annotations

from datetime import timedelta

from cvboost import cli
from cvboost.llm import client as C
from cvboost.llm.client import LLMResult
from cvboost.llm.prompts.registry import score_version
from cvboost.score import score
from tests.conftest import NOW
from tests.llm.helpers import RV, THRESHOLD, FakeClient, make_cfg, ok_result, seed


def run(store, client, cfg=None, now=NOW, **kw):
    return score(store, client, cfg or make_cfg(), RV, THRESHOLD, lambda: now, **kw)


def count(store, sql):
    return store.conn.execute(sql).fetchone()[0]


def test_valid_json_writes_verdicts_cache_and_second_run_is_free(store):
    seed(store, 3)
    client = FakeClient(
        lambda r: ok_result(r, {"j2": {"eligible": False, "eligibility_reason": "US only"}})
    )
    st = run(store, client)
    assert st.scored == 3 and st.keep == 2 and st.reject == 1 and st.errors == 0
    assert count(store, "SELECT COUNT(*) FROM verdicts WHERE stage='llm'") == 3
    assert (
        count(store, "SELECT COUNT(*) FROM verdicts WHERE stage='llm' AND decision='reject'") == 1
    )
    assert count(store, "SELECT COUNT(*) FROM llm_cache") == 3
    row = store.conn.execute("SELECT * FROM llm_calls WHERE kind='score'").fetchone()
    assert row["status"] == "ok" and row["n_items"] == 3
    assert row["input_tokens"] == 100 and row["total_cost_usd"] == 0.01
    assert row["model"] == "claude-haiku-5-5"
    assert client.preflights == 1

    again = FakeClient()
    st2 = run(store, again)
    assert len(again.requests) == 0 and again.preflights == 0
    assert st2.cached == 3 and st2.pending == 0
    assert count(store, "SELECT COUNT(*) FROM verdicts WHERE stage='llm'") == 3
    assert score_version() in {
        r[0] for r in store.conn.execute("SELECT rule_version FROM verdicts")
    }
    got = store.llm_verdicts(score_version())
    assert len(got) == 3 and {g.decision for g in got} == {"keep", "reject"}


def test_repost_with_same_block_served_from_cache(store, monkeypatch):
    ids = seed(store, 1)
    run(store, FakeClient())
    g = store.kept_canonicals(RV, THRESHOLD)[0]
    # a second group whose rendered block is identical (repost): cache hit, no call
    twin = g.__class__(**{**g.__dict__, "canonical_id": 999, "representative_id": ids[0]})
    monkeypatch.setattr(type(store), "kept_canonicals", lambda self, rv, th: [g, twin])
    c = FakeClient()
    st = run(store, c)
    assert len(c.requests) == 0 and st.cached == 2


def test_invalid_items_get_one_retry_then_llm_error_rest_kept(store):
    seed(store, 3)
    bad = lambda r: ok_result(r, {"j2": {"fit": 150}})  # noqa: E731
    # first answer: j2 invalid (fit 150), j3 missing; retry also invalid
    first = ok_result_missing_j3(bad)
    client = FakeClient(
        first,
        lambda r: LLMResult(status=C.INVALID_OUTPUT),
    )
    st = run(store, client)
    assert len(client.requests) == 2
    assert client.requests[1].stdin_text.count("<job ref=") == 2
    assert st.scored == 1 and st.errors == 2
    assert count(store, "SELECT COUNT(*) FROM llm_item_errors") == 2
    statuses = [r[0] for r in store.conn.execute("SELECT status FROM llm_calls WHERE kind='score'")]
    assert statuses == ["partial", "invalid_output"]
    assert cli_exit_for(st) == 1


def ok_result_missing_j3(make):
    def f(req):
        res = make(req)
        items = [i for i in res.structured["verdicts"] if i["ref"] != "j3"]
        return LLMResult(**{**res.__dict__, "structured": {"verdicts": items}})

    return f


def cli_exit_for(st):
    return 1 if st.errors else 0


def test_error_batch_twice_other_batch_survives(store):
    seed(store, 3)
    cfg = make_cfg(scoring={"batch_size": 2, "max_calls_per_run": 5})
    err = LLMResult(status=C.ERROR, error_message="boom")
    client = FakeClient(err, err)  # batch A fails twice, batch B uses the default ok
    st = run(store, client, cfg)
    assert len(client.requests) == 3
    assert st.scored == 1 and st.errors == 2
    assert count(store, "SELECT COUNT(*) FROM llm_item_errors") == 2

    # next run skips errored groups; --retry-errors sends them again
    c2 = FakeClient()
    st2 = run(store, c2, cfg)
    assert len(c2.requests) == 0 and st2.skipped_errors == 2
    c3 = FakeClient()
    st3 = run(store, c3, cfg, retry_errors=True)
    assert len(c3.requests) == 1 and st3.scored == 2
    assert count(store, "SELECT COUNT(*) FROM llm_item_errors") == 0


def test_duplicate_ref_makes_both_invalid(store):
    seed(store, 2)

    def dup(req):
        res = ok_result(req)
        items = res.structured["verdicts"]
        items.append(dict(items[0]))
        return res

    st = run(store, FakeClient(dup, LLMResult(status=C.ERROR)))
    assert st.scored == 1 and st.errors == 1  # j1 duplicated -> invalid, j2 fine


def test_daily_call_cap_blocks_everything_and_says_why(store):
    seed(store, 2)
    cfg = make_cfg(daily_call_caps={"haiku": 2, "opus": 10})
    for _ in range(2):
        store.insert_llm_call(
            kind="score",
            model_alias="haiku",
            prompt_version="x",
            n_items=1,
            attempt=1,
            now=NOW,
            local_day="2026-11-06",
            status="ok",
        )
    c = FakeClient()
    st = run(store, c, cfg)
    assert len(c.requests) == 0 and c.preflights == 0
    assert "daily cap haiku 2/2" in st.stopped


def test_yesterday_does_not_count_and_lisbon_day_boundary(store):
    seed(store, 1)
    cfg = make_cfg(daily_call_caps={"haiku": 1, "opus": 10})
    store.insert_llm_call(
        kind="score",
        model_alias="haiku",
        prompt_version="x",
        n_items=1,
        attempt=1,
        now=NOW,
        local_day="2026-11-05",
        status="ok",
    )
    c = FakeClient()
    assert run(store, c, cfg).scored == 1
    # summer: 23:30 UTC is already the next day in Lisbon (UTC+1)
    from datetime import UTC, datetime

    from cvboost.score import local_day

    assert local_day(datetime(2026, 7, 1, 23, 30, tzinfo=UTC), "Europe/London") == "2026-07-02"
    assert local_day(datetime(2026, 7, 1, 22, 30, tzinfo=UTC), "Europe/London") == "2026-07-01"


def test_daily_cost_cap(store):
    seed(store, 1)
    cfg = make_cfg(daily_cost_cap_usd=1.0)
    store.insert_llm_call(
        kind="score",
        model_alias="haiku",
        prompt_version="x",
        n_items=1,
        attempt=1,
        now=NOW,
        local_day="2026-11-06",
        status="ok",
        total_cost_usd=1.5,
    )
    c = FakeClient()
    st = run(store, c, cfg)
    assert len(c.requests) == 0 and "cost cap" in st.stopped


def test_per_run_cap(store):
    seed(store, 2)
    cfg = make_cfg(scoring={"batch_size": 1, "max_calls_per_run": 1})
    c = FakeClient()
    st = run(store, c, cfg)
    assert len(c.requests) == 1 and st.scored == 1
    assert "per-run cap 1 reached; 1 groups left" in st.stopped
    assert run(store, c, cfg, max_calls=5).scored == 1  # the rest next run


def test_usage_limit_pauses_with_doubling(store):
    seed(store, 3)
    cfg = make_cfg(scoring={"batch_size": 1, "max_calls_per_run": 5})
    limit = LLMResult(status=C.USAGE_LIMIT, error_message="hit your limit")
    c = FakeClient(limit)
    st = run(store, c, cfg)
    assert len(c.requests) == 1 and "paused until" in st.stopped
    assert st.errors == 0 and count(store, "SELECT COUNT(*) FROM llm_item_errors") == 0
    until = store.conn.execute("SELECT pause_until FROM llm_calls WHERE kind='score'").fetchone()[0]
    assert until.startswith("2026-11-06T12:30:00")

    c2 = FakeClient()
    st2 = run(store, c2, cfg, now=NOW + timedelta(minutes=10))
    assert len(c2.requests) == 0 and c2.preflights == 0 and "paused until" in st2.stopped

    # after the pause ends the second usage_limit in a row pauses for 60 minutes
    t = NOW + timedelta(minutes=31)
    c3 = FakeClient(limit)
    run(store, c3, cfg, now=t)
    until2 = store.conn.execute(
        "SELECT pause_until FROM llm_calls WHERE kind='score' ORDER BY id DESC"
    ).fetchone()[0]
    assert until2.startswith("2026-11-06T13:31:00")


def test_preflight_refusal_pauses_and_calls_nothing(store):
    seed(store, 1)
    c = FakeClient()
    c.preflight_result = C.PreflightResult(False, C.REFUSED_AUTH_METHOD, "api_key", "refused")
    st = run(store, c)
    assert len(c.requests) == 0 and "preflight failed" in st.stopped
    row = store.conn.execute("SELECT * FROM llm_calls").fetchone()
    assert row["kind"] == "preflight" and row["status"] == "refused_auth_method"
    assert row["pause_until"] is not None
    c2 = FakeClient()
    run(store, c2)
    assert c2.preflights == 0


def test_dry_run_never_calls_client(store):
    seed(store, 2)
    c = FakeClient()
    st = run(store, c, dry_run=True)
    assert c.requests == [] and c.preflights == 0
    assert st.pending == 2 and "dry-run batch 1: 2 items" in st.lines[0]


def test_old_groups_skipped_and_limit(store):
    seed(store, 3)
    cfg = make_cfg(scoring={"max_age_days": 1})
    st = run(
        store, FakeClient(), cfg, dry_run=True
    )  # postings are ~1 day old at NOW; first_seen is NOW
    assert st.pending == 3
    st = run(store, FakeClient(), make_cfg(), now=NOW + timedelta(days=60), dry_run=True)
    assert st.pending == 0 and st.cached == 0
    st = run(store, FakeClient(), limit=2)
    assert st.scored == 2


def test_cli_score_usage_and_why(store, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CVBOOST_DB", str(store.conn.execute("PRAGMA database_list").fetchone()[2]))
    ids = seed(store, 1)
    monkeypatch.setattr(cli, "make_client", lambda cfg: FakeClient())
    assert cli.main(["score"]) == 0
    out = capsys.readouterr().out
    assert "scored 1 (keep 1 / reject 0)" in out
    assert cli.main(["score"]) == 0
    assert "cached 1" in capsys.readouterr().out
    assert cli.main(["usage"]) == 0
    out = capsys.readouterr().out
    assert "haiku" in out and "auth: claude.ai" in out and score_version() in out
    assert "pause: none" in out
    assert cli.main(["jobs", "--why", str(ids[0])]) == 0
    out = capsys.readouterr().out
    assert "llm: eligible=True fit=80" in out and "summary: Контракт" in out
    assert cli.main(["score", "--dry-run"]) == 0


def test_bad_llm_config_exit_2(tmp_path, monkeypatch):
    p = tmp_path / "llm.yaml"
    p.write_text("backend: claude_cli\nsurprise: 1\n")
    monkeypatch.setenv("CVBOOST_DB", str(tmp_path / "x.sqlite"))
    monkeypatch.setenv("CVBOOST_LLM_CONFIG", str(p))
    assert cli.main(["score", "--dry-run"]) == 2


def test_shipped_llm_config_loads():
    from pathlib import Path

    from cvboost.llm.config import load_llm_config

    cfg = load_llm_config(Path(__file__).parents[2] / "tests" / "fixtures" / "config" / "llm.yaml")
    assert cfg.score_alias == "haiku" and cfg.scoring.batch_size == 8


# ---------------------------------------------------------------- round 2

import pytest  # noqa: E402

from cvboost.llm.schema import JobVerdict  # noqa: E402
from tests.llm.helpers import block_of, verdict_dict  # noqa: E402


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advancing(self, minutes, make):
        """An answer that moves the clock before replying."""

        def f(req):
            self.t += timedelta(minutes=minutes)
            return make(req)

        return f


def rows(store, sql="SELECT status FROM llm_calls WHERE kind='score' ORDER BY id"):
    return [r[0] for r in store.conn.execute(sql)]


def test_client_exception_is_accounted_and_does_not_crash(store):
    seed(store, 2)
    client = FakeClient(RuntimeError("boom"), ValueError("again"))
    st = run(store, client)
    assert rows(store) == ["error", "error"]
    assert st.calls == 2 and st.errors == 2 and store.calls_today("haiku", "2026-11-06") == 2
    assert count(store, "SELECT COUNT(*) FROM llm_item_errors") == 2
    msg = store.conn.execute("SELECT error_message FROM llm_calls WHERE status='error'").fetchone()
    assert "boom" in msg[0]


def test_keyboard_interrupt_leaves_a_counted_started_row(store):
    seed(store, 1)
    with pytest.raises(KeyboardInterrupt):
        run(store, FakeClient(KeyboardInterrupt()))
    assert rows(store) == ["started"]
    assert store.calls_today("haiku", "2026-11-06") == 1


def test_clock_across_lisbon_midnight_and_pause_relative_to_call_time(store):
    from datetime import UTC, datetime

    seed(store, 3)
    clk = Clock(datetime(2026, 7, 1, 22, 50, tzinfo=UTC))  # 23:50 in Lisbon
    cfg = make_cfg(
        daily_call_caps={"haiku": 1, "opus": 1}, scoring={"batch_size": 1, "max_calls_per_run": 5}
    )
    client = FakeClient(clk.advancing(20, ok_result), clk.advancing(20, ok_result))
    st = score(store, client, cfg, RV, THRESHOLD, clk)
    # call 1 on 07-01, call 2 on 07-02 (00:30 Lisbon): cap 1 per day is not hit by the other day
    days = [r[0] for r in store.conn.execute("SELECT local_day FROM llm_calls WHERE kind='score'")]
    assert days == ["2026-07-01", "2026-07-02"] and st.scored == 2
    assert "daily cap haiku 1/1" in st.stopped  # third group: cap of the new day reached

    # usage limit: the pause starts when the call ended, not when the run started
    clk2 = Clock(datetime(2026, 7, 3, 10, 0, tzinfo=UTC))
    limit = clk2.advancing(20, lambda r: LLMResult(status=C.USAGE_LIMIT))
    score(store, FakeClient(limit), make_cfg(), RV, THRESHOLD, clk2)
    until = store.conn.execute(
        "SELECT pause_until FROM llm_calls WHERE status='usage_limit'"
    ).fetchone()[0]
    assert until.startswith("2026-07-03T10:50:00")  # 10:20 + 30 min


def test_retry_remaps_refs_to_the_right_jobs(store):
    seed(store, 3)

    def first(req):
        res = ok_result(req, {"j1": {"fit": 50}, "j2": {"fit": 150}})
        items = [i for i in res.structured["verdicts"] if i["ref"] != "j3"]
        return LLMResult(**{**res.__dict__, "structured": {"verdicts": items}})

    def retry(req):
        per = {}
        for ref in ("j1", "j2"):
            blk = block_of(req, ref)
            per[ref] = {"fit": 11 if "CompanyB" in blk else 22}
        return ok_result(req, per)

    client = FakeClient(first, retry)
    st = run(store, client)
    assert st.scored == 3 and len(client.requests) == 2
    fits = dict(
        store.conn.execute(
            "SELECT j.company, v.relevance FROM verdicts v JOIN jobs j ON j.id = v.job_id "
            "WHERE v.stage='llm'"
        ).fetchall()
    )
    assert fits == {"CompanyA": 50, "CompanyB": 11, "CompanyC": 22}


def test_missing_ref_is_distinguished_from_invalid(store):
    seed(store, 2)

    def only_j1(req):
        res = ok_result(req)
        items = [i for i in res.structured["verdicts"] if i["ref"] == "j1"]
        return LLMResult(**{**res.__dict__, "structured": {"verdicts": items}})

    st = run(store, FakeClient(only_j1, only_j1))
    # first batch: j2 missing; retry with one item (j1 now) answers fine -> nothing missing
    assert st.scored == 2 and st.errors == 0


def test_missing_ref_error_reason_stored(store):
    seed(store, 2)

    def none_ok(req):
        return LLMResult(status=C.OK, structured={"verdicts": []})

    run(store, FakeClient(none_ok, none_ok))
    assert {r[0] for r in store.conn.execute("SELECT error FROM llm_item_errors")} == {
        "missing_ref"
    }

    store.conn.execute("DELETE FROM llm_item_errors")

    def j1_only(req):
        res = ok_result(req)
        items = [i for i in res.structured["verdicts"] if i["ref"] == "j1"]
        return LLMResult(**{**res.__dict__, "structured": {"verdicts": items}})

    def second_missing(req):
        return LLMResult(status=C.OK, structured={"verdicts": []})

    # one valid + one missing, then the retry returns an empty list again
    run(store, FakeClient(j1_only, second_missing), retry_errors=True)
    errs = [r[0] for r in store.conn.execute("SELECT error FROM llm_item_errors")]
    assert errs == ["missing_ref"]


def test_gates_close_before_the_retry_and_leave_items_pending(store):
    seed(store, 3)

    def partial(req):
        return ok_result(req, {"j2": {"fit": 150}})

    cfg = make_cfg(scoring={"max_calls_per_run": 1})
    st = run(store, FakeClient(partial), cfg)
    assert st.calls == 1 and st.scored == 2 and st.errors == 0
    assert "per-run cap 1 reached" in st.stopped
    assert count(store, "SELECT COUNT(*) FROM llm_item_errors") == 0

    # same through the daily cap
    store.conn.execute("DELETE FROM verdicts WHERE stage='llm'")
    store.conn.execute("DELETE FROM llm_cache")
    store.conn.execute("DELETE FROM llm_calls")
    cfg2 = make_cfg(daily_call_caps={"haiku": 1, "opus": 1})
    st = run(store, FakeClient(partial), cfg2)
    assert st.calls == 1 and st.errors == 0 and "daily cap haiku 1/1" in st.stopped
    assert count(store, "SELECT COUNT(*) FROM llm_item_errors") == 0


def test_timeout_then_item_errors(store):
    seed(store, 2)
    st = run(store, FakeClient(LLMResult(status=C.TIMEOUT), LLMResult(status=C.TIMEOUT)))
    assert rows(store) == ["timeout", "timeout"] and st.errors == 2
    assert {r[0] for r in store.conn.execute("SELECT error FROM llm_item_errors")} == {"timeout"}


def test_auth_error_mid_run_pauses_and_keeps_rest_pending(store):
    seed(store, 3)
    cfg = make_cfg(scoring={"batch_size": 2, "max_calls_per_run": 5})
    st = run(
        store, FakeClient(ok_result, LLMResult(status=C.AUTH_ERROR, error_message="login")), cfg
    )
    assert st.scored == 2 and st.errors == 0 and "paused until" in st.stopped
    assert count(store, "SELECT COUNT(*) FROM llm_item_errors") == 0
    until = store.conn.execute(
        "SELECT pause_until FROM llm_calls WHERE status='auth_error'"
    ).fetchone()[0]
    assert until.startswith("2026-11-06T13:00:00")  # auth_minutes = 60


def test_budget_status_no_retry(store):
    seed(store, 1)
    st = run(store, FakeClient(LLMResult(status=C.BUDGET)))
    assert st.calls == 1 and st.errors == 1
    assert store.conn.execute("SELECT error FROM llm_item_errors").fetchone()[0] == "budget"


def test_ignore_pause_does_not_bypass_caps(store):
    seed(store, 1)
    store.insert_llm_call(
        kind="score",
        model_alias="haiku",
        prompt_version="x",
        n_items=1,
        attempt=1,
        now=NOW,
        local_day="2026-11-06",
        status="usage_limit",
        pause_until=NOW + timedelta(hours=1),
    )
    cfg = make_cfg(daily_call_caps={"haiku": 1, "opus": 1})
    c = FakeClient()
    st = run(store, c, cfg, ignore_pause=True)
    assert len(c.requests) == 0 and "daily cap haiku 1/1" in st.stopped


def test_circuit_breaker_stops_after_two_failed_batches(store):
    seed(store, 4)
    cfg = make_cfg(scoring={"batch_size": 1, "max_calls_per_run": 10})
    err = LLMResult(status=C.ERROR, error_message="down")
    c = FakeClient(default=err)
    c.default = lambda r: err
    st = run(store, c, cfg)
    assert st.calls == 4 and "circuit open: 2 consecutive failed batches" in st.stopped
    assert st.errors == 1 and count(store, "SELECT COUNT(*) FROM llm_item_errors") == 1
    assert count(store, "SELECT COUNT(*) FROM llm_calls WHERE pause_until IS NOT NULL") == 0
    # the second batch's item is still pending: it is retried by the next run
    c2 = FakeClient()
    st2 = run(store, c2, cfg)
    assert st2.pending == 3 and st2.scored == 3


def test_preflight_error_stops_without_pause(store):
    seed(store, 1)
    c = FakeClient()
    c.preflight_result = C.PreflightResult(False, C.ERROR, None, "timed out")
    st = run(store, c)
    assert len(c.requests) == 0 and "preflight failed" in st.stopped and "paused" not in st.stopped
    assert store.conn.execute("SELECT pause_until FROM llm_calls").fetchone()[0] is None
    c2 = FakeClient()
    assert run(store, c2).scored == 1 and c2.preflights == 1


def test_dry_run_works_during_a_pause(store):
    seed(store, 2)
    run(store, FakeClient(LLMResult(status=C.USAGE_LIMIT)))
    c = FakeClient()
    st = run(store, c, dry_run=True)
    assert st.pending == 2 and st.stopped is None and "dry-run batch 1" in st.lines[0]
    assert "paused" in run(store, c).stopped


def test_prompt_injection_taints_the_call(store):
    seed(store, 3)

    def first(req):
        return ok_result(req, {"j2": {"red_flags": ["Prompt_Injection"], "eligible": True}})

    client = FakeClient(first)
    st = run(store, client)
    assert len(client.requests) == 2
    assert client.requests[1].stdin_text.count("<job ref=") == 2
    assert block_of(client.requests[0], "j2") not in client.requests[1].stdin_text
    assert st.scored == 3 and st.errors == 0
    inj = store.conn.execute(
        "SELECT v.decision FROM verdicts v JOIN jobs j ON j.id=v.job_id "
        "WHERE v.stage='llm' AND j.company='CompanyB'"
    ).fetchone()
    assert inj[0] == "reject"
    assert count(store, "SELECT COUNT(*) FROM verdicts WHERE stage='llm' AND decision='keep'") == 2
    # the call that was tainted cached only the injecting job
    assert (
        store.conn.execute(
            "SELECT COUNT(*) FROM llm_cache WHERE call_id = (SELECT MIN(id) FROM llm_calls "
            "WHERE kind='score')"
        ).fetchone()[0]
        == 1
    )


def test_verdict_a_b_a_makes_a_latest_again_and_errors_view(store):
    ids = seed(store, 1)
    v = JobVerdict.model_validate(verdict_dict())
    kw = dict(job_id=ids[0], verdict=v, prompt_version="pv", call_id=None, model="m", cached=False)
    t = NOW
    assert store.insert_llm_verdict(item_hash="A", now=t, **kw)
    assert store.insert_llm_verdict(item_hash="B", now=t + timedelta(hours=1), **kw)
    assert not store.insert_llm_verdict(item_hash="B", now=t + timedelta(hours=2), **kw)
    assert store.insert_llm_verdict(item_hash="A", now=t + timedelta(hours=3), **kw)
    got = store.llm_verdicts("pv")
    assert len(got) == 1 and got[0].input_hash == "A" and got[0].model == "m"
    assert got[0].cached is False
    assert count(store, "SELECT COUNT(*) FROM verdicts WHERE stage='llm'") == 2
    store.item_error_put(ids[0], "H", "haiku", "pv", None, "timeout", NOW)
    cid = store.conn.execute("SELECT canonical_id FROM jobs WHERE id=?", (ids[0],)).fetchone()[0]
    assert store.llm_errors("pv") == {cid: "timeout"} and store.llm_errors("other") == {}


def test_upsert_same_hash_updates_decision_fields(store):
    ids = seed(store, 1)
    kw = dict(job_id=ids[0], prompt_version="pv", call_id=None, model="m", cached=False)
    a = JobVerdict.model_validate(verdict_dict())
    a2 = JobVerdict.model_validate(verdict_dict(eligible=False, fit=7, profile="none"))
    b = JobVerdict.model_validate(verdict_dict(fit=60))
    store.insert_llm_verdict(verdict=a, item_hash="A", now=NOW, **kw)
    store.insert_llm_verdict(verdict=b, item_hash="B", now=NOW + timedelta(hours=1), **kw)
    assert store.insert_llm_verdict(verdict=a2, item_hash="A", now=NOW + timedelta(hours=2), **kw)
    row = store.conn.execute(
        "SELECT decision, reasons_json, relevance, profile FROM verdicts "
        "WHERE input_hash='A' AND stage='llm'"
    ).fetchone()
    assert tuple(row) == ("reject", '["llm_ineligible"]', 7, None)


def test_circuit_breaker_resets_after_success(store):
    seed(store, 3)
    cfg = make_cfg(scoring={"batch_size": 1, "max_calls_per_run": 10})
    err = LLMResult(status=C.ERROR)
    st = run(store, FakeClient(err, err, ok_result, err, err), cfg)
    assert st.stopped is None and st.calls == 5 and st.scored == 1 and st.errors == 2


def test_injector_every_call_stops_at_per_run_cap(store):
    seed(store, 3)
    cfg = make_cfg(scoring={"max_calls_per_run": 2})

    def always(req):
        return ok_result(req, {"j1": {"red_flags": ["prompt_injection"]}})

    c = FakeClient(default=always)
    st = run(store, c, cfg)
    assert len(c.requests) == 2 and "per-run cap 2" in st.stopped
    assert st.scored == 2 and st.errors == 0
    assert count(store, "SELECT COUNT(*) FROM llm_item_errors") == 0
    assert run(store, FakeClient(), cfg).scored == 1  # the leftover is still pending
