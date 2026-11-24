"""Partial success per feed, real clocks per source, collect deadline."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from cvboost.collect import collect
from cvboost.connectors.wwr import WWRConnector
from cvboost.http import HttpClient
from tests.conftest import FIXTURES, NOW, PUBLIC_RESOLVER
from tests.test_collect import FakeClock, make_http  # noqa: F401
from tests.worker.helpers import FakeClock as StepClock
from tests.worker.helpers import FakeConnector

GOOD = [
    "remote-back-end-programming-jobs",
    "remote-sales-and-marketing-jobs",
]
BAD = "remote-product-jobs"
URL = "https://weworkremotely.com/categories/{}.rss"


def wwr_http(failing: dict[str, httpx.Response | Exception]):
    def handler(req: httpx.Request) -> httpx.Response:
        cat = str(req.url).rsplit("/", 1)[-1].removesuffix(".rss")
        if cat in failing:
            r = failing[cat]
            if isinstance(r, Exception):
                raise r
            return r
        return httpx.Response(200, content=(FIXTURES / "wwr" / f"{cat}.xml").read_bytes())

    return HttpClient(httpx.MockTransport(handler), sleep=lambda s: None, resolver=PUBLIC_RESOLVER)


@pytest.fixture(autouse=True)
def _no_jitter(monkeypatch):
    monkeypatch.setattr("cvboost.http.random.uniform", lambda a, b: 1.0)


def test_one_feed_503_others_saved_run_ok_with_note(store):
    c = WWRConnector([GOOD[0], BAD, GOOD[1]])
    store.sync_sources({"wwr": WWRConnector}, NOW)
    [res] = collect(
        store, wwr_http({BAD: httpx.Response(503)}), [c], limit=None, force=True, now=NOW
    )
    assert res.status == "ok" and res.error_type == "partial" and res.found == 10
    run = store.conn.execute("SELECT * FROM source_runs").fetchone()
    assert run["status"] == "ok" and run["error_type"] == "partial"
    assert f"1/3 feeds failed: /categories/{BAD}.rss HTTP 503" in run["error_message"]
    assert "weworkremotely.com" not in run["error_message"]  # path only, no host
    assert store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 10
    assert store.source_state("wwr").consecutive_failures == 0


def test_partial_success_resets_backoff(store):
    c = WWRConnector([GOOD[0], BAD])
    store.sync_sources({"wwr": WWRConnector}, NOW)
    http = wwr_http({BAD: httpx.Response(503)})
    store.conn.execute(
        "INSERT INTO source_runs (source, started_at, status) VALUES ('wwr', ?, 'error')",
        (NOW.isoformat(),),
    )
    assert store.source_state("wwr").consecutive_failures == 1
    collect(store, http, [c], limit=None, force=True, now=NOW.replace(hour=13))
    assert store.source_state("wwr").consecutive_failures == 0


def test_parse_error_in_one_feed_is_partial(store):
    c = WWRConnector(GOOD + [BAD])
    store.sync_sources({"wwr": WWRConnector}, NOW)
    http = wwr_http({BAD: httpx.Response(200, content=b"<rss><not-closed>")})
    [res] = collect(store, http, [c], limit=None, force=True, now=NOW)
    assert res.status == "ok" and res.error_type == "partial" and "ParseError" in res.error_message


def test_all_feeds_failing_is_an_error(store):
    c = WWRConnector([BAD, "remote-devops-sysadmin-jobs"])
    store.sync_sources({"wwr": WWRConnector}, NOW)
    http = wwr_http({BAD: httpx.Response(503), "remote-devops-sysadmin-jobs": httpx.Response(503)})
    [res] = collect(store, http, [c], limit=None, force=True, now=NOW)
    assert res.status == "error" and res.http_status == 503
    assert store.source_state("wwr").consecutive_failures == 1


def test_first_page_kept_when_later_page_fails(store):
    from cvboost.connectors.base import Connector, FetchOutcome  # noqa: F401

    c = WWRConnector([GOOD[0]])
    out = c.fetch_partial(wwr_http({}))
    assert len(out.raws) == 5 and out.failures == [] and out.requests_total == 1


# --- real clock per source ---


def test_each_source_gets_its_own_start_and_duration(store):
    a, b = FakeConnector("a", ids=["1"]), FakeConnector("b", ids=["2"])
    store.sync_sources({"a": type(a), "b": type(b)}, NOW)
    clock = StepClock(NOW, step=3)
    collect(store, None, [a, b], limit=None, force=True, clock=clock)  # type: ignore[arg-type]
    rows = store.conn.execute("SELECT * FROM source_runs ORDER BY id").fetchall()
    assert len(rows) == 2
    for r in rows:
        assert r["duration_ms"] > 0
        assert datetime.fromisoformat(r["finished_at"]) > datetime.fromisoformat(r["started_at"])
    assert rows[0]["started_at"] != rows[1]["started_at"]


def test_deadline_defers_without_a_run_row(store):
    a, b = FakeConnector("a", ids=["1"]), FakeConnector("b", ids=["2"])
    store.sync_sources({"a": type(a), "b": type(b)}, NOW)
    clock = StepClock(NOW, step=10)
    deadline = NOW.replace(second=25)  # reads: t0=0, a: 10 (check) ... b's check lands past 25
    res = collect(store, None, [a, b], limit=None, force=True, clock=clock, deadline=deadline)  # type: ignore[arg-type]
    assert [r.status for r in res] == ["ok", "skipped"] and res[1].skip_reason == "deferred"
    assert store.conn.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0] == 1
    assert b.fetches == 0
    # it is still due next time
    assert store.source_state("b").last_started_at is None


def test_most_overdue_source_runs_first(store):
    a, b = FakeConnector("a", ids=["1"]), FakeConnector("b", ids=["2"])
    store.sync_sources({"a": type(a), "b": type(b)}, NOW)
    for name, hour in (("a", 11), ("b", 8)):  # b ran longer ago: more overdue
        store.conn.execute(
            "INSERT INTO source_runs (source, started_at, status) VALUES (?, ?, 'ok')",
            (name, NOW.replace(hour=hour).isoformat()),
        )
    order = []
    orig_a, orig_b = a.fetch_partial, b.fetch_partial
    a.fetch_partial = lambda *x, **k: order.append("a") or orig_a(*x, **k)  # type: ignore[method-assign]
    b.fetch_partial = lambda *x, **k: order.append("b") or orig_b(*x, **k)  # type: ignore[method-assign]
    res = collect(store, None, [a, b], limit=None, force=False, now=NOW)  # type: ignore[arg-type]
    assert order == ["b", "a"] and [r.source for r in res] == ["a", "b"]


def test_should_stop_defers_the_remaining_sources(store):
    a, b = FakeConnector("a", ids=["1"]), FakeConnector("b", ids=["2"])
    store.sync_sources({"a": type(a), "b": type(b)}, NOW)
    flag = {"stop": False}
    orig = a.fetch_partial

    def fetch_then_stop(*x, **k):
        flag["stop"] = True  # SIGTERM arrives while the first source is being fetched
        return orig(*x, **k)

    a.fetch_partial = fetch_then_stop  # type: ignore[method-assign]
    res = collect(
        store,
        None,
        [a, b],
        limit=None,
        force=True,
        now=NOW,  # type: ignore[arg-type]
        should_stop=lambda: flag["stop"],
    )
    assert [r.status for r in res] == ["ok", "skipped"] and res[1].skip_reason == "deferred"
    assert (
        b.fetches == 0 and store.conn.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0] == 1
    )
