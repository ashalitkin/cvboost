"""Adzuna / Reed / ITJobs.pt (#19): fixtures, keys never leak, call budget, pause + alert."""

from __future__ import annotations

import base64
import json
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from cvboost import api_budget
from cvboost.collect import collect_source
from cvboost.connectors.adzuna import AdzunaConnector
from cvboost.connectors.itjobs import ITJobsConnector
from cvboost.connectors.reed import ReedConnector
from cvboost.http import HttpClient
from cvboost.inbox import attribution_for
from tests.conftest import FIXTURES, PUBLIC_RESOLVER

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
APP_ID, APP_KEY, REED_KEY, IT_KEY = (
    "EXAMPLEAPPID",
    "k3y-adzuna-SECRET",
    "reed-SECRET-key",
    "it-SECRET",
)


@pytest.fixture(autouse=True)
def synced(store):
    from cvboost import connectors

    store.sync_sources(connectors.REGISTRY, NOW)


@pytest.fixture(autouse=True)
def keys(monkeypatch, tmp_path):
    monkeypatch.setenv("ADZUNA_APP_ID", APP_ID)
    monkeypatch.setenv("ADZUNA_APP_KEY", APP_KEY)
    monkeypatch.setenv("REED_API_KEY", REED_KEY)
    monkeypatch.setenv("ITJOBS_API_KEY", IT_KEY)
    monkeypatch.setenv(api_budget.ENV_PATH, str(tmp_path / "none.yaml"))  # defaults


def _client(handler):
    return HttpClient(httpx.MockTransport(handler), resolver=PUBLIC_RESOLVER, sleep=lambda s: None)


def _ok(name):
    body = (FIXTURES / name).read_bytes()
    return lambda request: httpx.Response(200, content=body)


# --- fixtures ---


def test_adzuna_fixture():
    c = AdzunaConnector()
    raws = c.parse((FIXTURES / "adzuna/gb-search.json").read_bytes(), f"{c.base_url}/gb/search/1")
    assert len(raws) == 6
    jobs = [c.normalise(r, NOW) for r in raws]
    first = jobs[0]
    assert first.company == "Northwind Analytics" and first.title.startswith("Senior Data Eng")
    assert (first.salary_min, first.salary_max, first.salary_currency) == (85000, 95000, "GBP")
    assert first.salary_period == "year" and first.employment_type == "full_time"
    assert first.posted_at == datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
    assert "utm_" not in first.url and first.url.startswith("https://www.adzuna.co.uk/")
    assert jobs[1].salary_min is None  # predicted salary is Adzuna's estimate
    assert jobs[2].employment_type == "contract"
    assert jobs[3].employment_type == "part_time"
    assert all(j.url and j.company and j.posted_at for j in jobs)


def test_adzuna_currency_by_country():
    c = AdzunaConnector()
    body = (FIXTURES / "adzuna/gb-search.json").read_bytes()
    job = c.normalise(c.parse(body, f"{c.base_url}/de/search/1")[0], NOW)
    assert job.salary_currency == "EUR"


def test_reed_fixture():
    c = ReedConnector()
    raws = c.parse((FIXTURES / "reed/search.json").read_bytes(), "x")
    jobs = [c.normalise(r, NOW) for r in raws]
    assert len(jobs) == 6
    assert (jobs[0].salary_min, jobs[0].salary_period, jobs[0].salary_currency) == (
        70000,
        "year",
        "GBP",
    )
    assert jobs[0].posted_at == datetime(2026, 11, 5, tzinfo=UTC)
    assert jobs[1].salary_min is None and jobs[1].salary_text == "£450-£550"  # a day rate
    assert jobs[2].salary_text is None and jobs[2].salary_min is None
    assert jobs[0].url == "https://www.reed.co.uk/jobs/senior-analytics-engineer/50000001"


def test_itjobs_fixture():
    c = ITJobsConnector()
    jobs = [
        c.normalise(r, NOW) for r in c.parse((FIXTURES / "itjobs/search.json").read_bytes(), "x")
    ]
    assert len(jobs) == 6
    a = jobs[0]
    assert a.location_text == "Lisboa" and a.remote is True and a.employment_type == "full_time"
    assert (a.salary_min, a.salary_max, a.salary_currency, a.salary_period) == (
        40000,
        55000,
        "EUR",
        "year",
    )
    assert a.url == "https://www.itjobs.pt/oferta/300001/data-engineer"
    assert jobs[1].remote is None and jobs[2].employment_type == "freelance"
    assert jobs[3].employment_type == "part_time" and jobs[4].salary_min is None


def test_attributions():
    assert attribution_for("adzuna") == "Jobs by Adzuna"
    assert attribution_for("reed") == "via Reed"
    assert attribution_for("itjobs") == "via ITJobs.pt"


# --- not configured ---


@pytest.mark.parametrize("cls", [AdzunaConnector, ReedConnector, ITJobsConnector])
def test_not_configured_makes_no_request(cls, store, monkeypatch):
    for v in ("ADZUNA_APP_ID", "ADZUNA_APP_KEY", "REED_API_KEY", "ITJOBS_API_KEY"):
        monkeypatch.delenv(v)

    def boom(request):
        raise AssertionError("no request without a key")

    c = cls()
    r = collect_source(store, _client(boom), c, limit=None, now=NOW)
    assert (r.status, r.skip_reason) == ("skipped", "not_configured")
    assert store.conn.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0] == 0


def test_partial_credentials_is_not_configured(store, monkeypatch):
    monkeypatch.delenv("ADZUNA_APP_KEY")
    r = collect_source(
        store, _client(_ok("adzuna/gb-search.json")), AdzunaConnector(), limit=None, now=NOW
    )
    assert r.skip_reason == "not_configured"


# --- request shape and key handling ---


def test_adzuna_keys_go_in_query_params_and_run_stores_jobs(store):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=(FIXTURES / "adzuna/gb-search.json").read_bytes())

    r = collect_source(store, _client(handler), AdzunaConnector(), limit=None, now=NOW)
    assert r.status == "ok" and r.new == 6 and len(seen) == 6
    q = dict(seen[0].url.params)
    assert q["app_id"] == APP_ID and q["app_key"] == APP_KEY and q["what"] == "analytics engineer"
    assert q["results_per_page"] == "50"
    assert str(seen[0].url).startswith("https://api.adzuna.com/v1/api/jobs/gb/search/1?")
    assert seen[0].headers["Accept"] == "application/json"


def test_reed_uses_basic_auth_and_no_key_in_url(store):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=(FIXTURES / "reed/search.json").read_bytes())

    r = collect_source(store, _client(handler), ReedConnector(), limit=None, now=NOW)
    assert r.status == "ok" and len(seen) == 4
    token = base64.b64decode(seen[0].headers["Authorization"].split()[1]).decode()
    assert token == f"{REED_KEY}:"
    assert REED_KEY not in str(seen[0].url)


def test_itjobs_key_param(store):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=(FIXTURES / "itjobs/search.json").read_bytes())

    r = collect_source(store, _client(handler), ITJobsConnector(), limit=None, now=NOW)
    assert r.status == "ok" and dict(seen[0].url.params)["api_key"] == IT_KEY


def test_stored_payload_never_holds_the_key(store):
    body = json.dumps(
        {
            "results": [
                {
                    "id": "1",
                    "title": "T",
                    "company": {"display_name": "C"},
                    "description": f"see app_key={APP_KEY}",
                    "created": "2026-11-09T10:00:00Z",
                    "redirect_url": f"https://www.adzuna.co.uk/jobs/land/ad/1?utm_source={APP_ID}&k={APP_KEY}",
                }
            ]
        }
    ).encode()
    collect_source(
        store,
        _client(lambda r: httpx.Response(200, content=body)),
        AdzunaConnector(),
        limit=1,
        now=NOW,
    )
    dump = "\n".join(str(tuple(row)) for row in store.conn.execute("SELECT * FROM jobs"))
    assert APP_KEY not in dump and APP_ID not in dump and "***" in dump


@pytest.mark.parametrize(
    ("cls", "secret", "status"),
    [
        (AdzunaConnector, APP_KEY, 500),
        (AdzunaConnector, APP_KEY, 401),
        (ReedConnector, REED_KEY, 500),
        (ITJobsConnector, IT_KEY, 403),
    ],
)
def test_errors_and_logs_never_hold_the_key(cls, secret, status, store, caplog):
    caplog.set_level(logging.DEBUG)
    r = collect_source(
        store, _client(lambda req: httpx.Response(status)), cls(), limit=None, now=NOW
    )
    assert r.status == "error" and r.http_status == status
    blob = "\n".join(
        [caplog.text, r.error_message or ""]
        + [str(tuple(row)) for row in store.conn.execute("SELECT * FROM source_runs")]
        + [str(tuple(row)) for row in store.conn.execute("SELECT * FROM system_alerts")]
    )
    assert secret not in blob and base64.b64encode(f"{secret}:".encode()).decode() not in blob
    assert APP_ID not in blob


def test_transport_error_never_holds_the_key(store, caplog):
    def handler(request):
        raise httpx.ConnectError(f"cannot connect {request.url}")

    r = collect_source(store, _client(handler), AdzunaConnector(), limit=None, now=NOW)
    assert r.status == "error"
    assert APP_KEY not in (r.error_message or "") and APP_KEY not in caplog.text


def test_auth_error_stops_the_run_after_one_call(store):
    n = []

    def handler(request):
        n.append(1)
        return httpx.Response(401)

    collect_source(store, _client(handler), ReedConnector(), limit=None, now=NOW)
    assert len(n) == 1


def test_rotation_moves_through_the_plan(store):
    c = AdzunaConnector()
    plan = c.plan()
    seen = []

    def handler(request):
        seen.append(dict(request.url.params)["what"] + request.url.path[-12:])
        return httpx.Response(200, content=b'{"results": []}')

    for i in range(2):
        collect_source(
            store, _client(handler), AdzunaConnector(), limit=None, now=NOW + timedelta(hours=i)
        )
    expected = [dict(p.params)["what"] + p.url[-12:] for p in plan[:12]]
    assert seen == expected


# --- budget ---


def test_budget_counts_per_day_and_month(store):
    collect_source(store, _client(_ok("reed/search.json")), ReedConnector(), limit=None, now=NOW)
    assert store.api_calls_day("reed", NOW) == 4
    assert store.api_calls_day("reed", NOW + timedelta(days=1)) == 0
    collect_source(
        store,
        _client(_ok("reed/search.json")),
        ReedConnector(),
        limit=None,
        now=NOW + timedelta(days=1),
    )
    assert store.api_calls_month("reed", NOW) == 8
    assert store.api_calls_month("reed", datetime(2026, 12, 1, tzinfo=UTC)) == 0


def _limits(monkeypatch, tmp_path, text):
    p = tmp_path / "budget.yaml"
    p.write_text(text)
    monkeypatch.setenv(api_budget.ENV_PATH, str(p))


def test_near_limit_alerts_once_then_pause_at_limit(store, monkeypatch, tmp_path):
    _limits(monkeypatch, tmp_path, "adzuna: {per_day: 100, per_month: 7}\n")
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, content=b'{"results": []}')

    collect_source(store, _client(handler), AdzunaConnector(), limit=None, now=NOW)  # 6 calls
    kinds = [tuple(r) for r in store.conn.execute("SELECT kind, text FROM system_alerts")]
    assert len(calls) == 6 and len(kinds) == 1
    assert (
        kinds[0][0] == "api_budget"
        and "Adzuna limit is close" in kinds[0][1]
        and "#34" in kinds[0][1]
    )
    # second run: 1 more call reaches 7, the next is refused
    collect_source(
        store, _client(handler), AdzunaConnector(), limit=None, now=NOW + timedelta(hours=6)
    )
    assert len(calls) == 7 and store.api_calls_month("adzuna", NOW) == 7
    texts = [r[0] for r in store.conn.execute("SELECT text FROM system_alerts ORDER BY id")]
    assert any("limit reached" in t and "#34" in t for t in texts)
    # paused: no request at all, a skipped result, no run row
    r = collect_source(
        store, _client(handler), AdzunaConnector(), limit=None, now=NOW + timedelta(hours=12)
    )
    assert (r.status, r.skip_reason) == ("skipped", "budget") and len(calls) == 7
    # alerts are deduplicated per period
    assert store.conn.execute("SELECT COUNT(*) FROM system_alerts").fetchone()[0] == 2
    # next month: runs again
    r = collect_source(
        store,
        _client(handler),
        AdzunaConnector(),
        limit=None,
        now=datetime(2026, 12, 2, tzinfo=UTC),
    )
    assert r.status == "ok"


def test_day_limit_pauses_until_tomorrow(store, monkeypatch, tmp_path):
    _limits(monkeypatch, tmp_path, "reed: {per_day: 4, per_month: 600}\n")
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, content=b'{"results": []}')

    collect_source(store, _client(handler), ReedConnector(), limit=None, now=NOW)
    r = collect_source(
        store, _client(handler), ReedConnector(), limit=None, now=NOW + timedelta(hours=6)
    )
    assert r.skip_reason == "budget" and len(calls) == 4
    collect_source(
        store, _client(handler), ReedConnector(), limit=None, now=NOW + timedelta(days=1)
    )
    assert len(calls) == 8


def test_alert_goes_to_telegram_text_without_keys(store, monkeypatch, tmp_path):
    _limits(monkeypatch, tmp_path, "itjobs: {per_day: 100, per_month: 3}\n")
    collect_source(
        store, _client(_ok("itjobs/search.json")), ITJobsConnector(), limit=None, now=NOW
    )
    texts = [r[0] for r in store.conn.execute("SELECT text FROM system_alerts")]
    assert texts and all(IT_KEY not in t for t in texts)
    assert "ITJobs.pt" in texts[0] and "#34" not in texts[0]


def test_broken_budget_file_does_not_lift_limits(monkeypatch, tmp_path):
    _limits(monkeypatch, tmp_path, "adzuna: {per_day: -1}\n")
    limit, _, problem = api_budget.load_limit("adzuna", AdzunaConnector.default_limit)
    assert problem
    assert limit == AdzunaConnector.default_limit


def test_repo_budget_config_is_below_defaults():
    import os

    os.environ.pop(api_budget.ENV_PATH, None)
    limit, warn, problem = api_budget.load_limit("adzuna", AdzunaConnector.default_limit)
    assert problem is None
    assert limit.per_month <= 1000 and warn == 0.8


def test_normalise_long_inputs_is_fast():
    c = AdzunaConnector()
    body = json.dumps(
        {
            "results": [
                {
                    "id": "1",
                    "title": "x" * 30000,
                    "description": " " * 30000 + "a" * 30000,
                    "company": {"display_name": "C"},
                    "redirect_url": "https://www.adzuna.co.uk/ad/1",
                    "created": "2026-11-09T10:00:00Z",
                }
            ]
        }
    ).encode()
    t0 = time.monotonic()
    c.normalise(c.parse(body, f"{c.base_url}/gb/search/1")[0], NOW)
    assert time.monotonic() - t0 < 1.0


# --- security follow-ups ---


def test_no_retries_one_http_request_per_counted_call(store):
    n = []

    def handler(request):
        n.append(1)
        return httpx.Response(503)

    collect_source(store, _client(handler), ReedConnector(), limit=None, now=NOW)
    assert len(n) == store.api_calls_day("reed", NOW) == 4


def test_broken_budget_file_warns_alerts_and_takes_minimum(store, monkeypatch, tmp_path, caplog):
    _limits(
        monkeypatch, tmp_path, "adzuna: {per_day: 9999, per_month: 99999}\nwarn_ratio: 5\nfoo: 1\n"
    )
    limit, warn, problem = api_budget.load_limit("adzuna", AdzunaConnector.default_limit)
    assert limit == AdzunaConnector.default_limit and warn == 0.8 and "warn_ratio" in problem
    caplog.set_level(logging.WARNING)
    collect_source(
        store, _client(_ok("adzuna/gb-search.json")), AdzunaConnector(), limit=None, now=NOW
    )
    assert "api_budget config" in caplog.text
    kinds = [r[0] for r in store.conn.execute("SELECT kind FROM system_alerts")]
    assert "api_budget_config" in kinds


def test_unparsable_budget_file_uses_defaults(monkeypatch, tmp_path):
    _limits(monkeypatch, tmp_path, "adzuna: [unclosed\n")
    limit, _, problem = api_budget.load_limit("adzuna", AdzunaConnector.default_limit)
    assert limit == AdzunaConnector.default_limit and problem


def test_valid_budget_file_can_raise_nothing_silently(monkeypatch, tmp_path):
    _limits(monkeypatch, tmp_path, "adzuna: {per_day: 50, per_month: 900}\n")
    limit, _, problem = api_budget.load_limit("adzuna", AdzunaConnector.default_limit)
    assert (limit.per_day, limit.per_month, problem) == (50, 900, None)


def test_telegram_links_and_attribution_for_adzuna_and_itjobs():
    from cvboost.notify.format import https_link

    assert https_link("adzuna", "https://www.adzuna.co.uk/jobs/land/ad/1")
    assert https_link("adzuna", "https://www.adzuna.com/details/1")
    assert https_link("adzuna", "https://evil.example.com/adzuna.co.uk") is None
    assert https_link("itjobs", "https://www.itjobs.pt/oferta/1/x")
    assert https_link("reed", "https://www.reed.co.uk/jobs/x/1")


def test_alert_text_carries_attribution():
    from cvboost.notify import format as F

    v = F.AlertView(
        title="T",
        company="C",
        rate=None,
        location=None,
        fit=70,
        profile=None,
        employment=None,
        seniority=None,
        summary="s",
        links=[
            ("adzuna", "https://www.adzuna.co.uk/jobs/land/ad/1"),
            ("itjobs", "https://www.itjobs.pt/oferta/1/x"),
        ],
    )
    text = F.render_alert(v)
    assert "Jobs by Adzuna: https://www.adzuna.co.uk/jobs/land/ad/1" in text
    assert "via ITJobs.pt: https://www.itjobs.pt/oferta/1/x" in text


def test_redacting_filter_hides_api_keys():
    from cvboost.notify.redact import RedactingFilter

    f = RedactingFilter(None)
    for secret in (APP_ID, APP_KEY, REED_KEY, IT_KEY):
        rec = logging.LogRecord("x", logging.WARNING, "p", 1, f"boom {secret} end", None, None)
        f.filter(rec)
        assert secret not in rec.getMessage()
    b64 = base64.b64encode(f"{REED_KEY}:".encode()).decode()
    rec = logging.LogRecord("x", logging.WARNING, "p", 1, f"Authorization: Basic {b64}", None, None)
    f.filter(rec)
    assert b64 not in rec.getMessage()


# --- #65: CTO queries stay inside the budget ---


@pytest.mark.parametrize("cls", [AdzunaConnector, ReedConnector, ITJobsConnector])
def test_tech_lead_queries_are_in_the_plan_without_more_calls_a_day(cls):
    import importlib

    c = cls()
    TECH_LEAD_QUERIES = importlib.import_module(cls.__module__).TECH_LEAD_QUERIES
    terms = [
        next(v for k, v in p.params.items() if k in ("what", "keywords", "q")) for p in c.plan()
    ]
    for q in TECH_LEAD_QUERIES:
        assert q in terms
    # the rotation takes calls_per_run per run: more queries = a longer rotation, not more calls
    per_day = c.calls_per_run * int(timedelta(days=1) / c.interval)
    limit, _, problem = api_budget.load_limit(c.api, c.default_limit)
    assert problem is None
    assert per_day <= limit.per_day
    assert per_day * 30 <= limit.per_month
