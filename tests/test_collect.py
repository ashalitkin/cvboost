from __future__ import annotations

import httpx
import pytest

from cvboost import cli, connectors
from cvboost.collect import collect
from cvboost.connectors.wwr import WWRConnector
from cvboost.http import HttpClient
from tests.conftest import FIXTURES, NOW, PUBLIC_RESOLVER

CATS = ["remote-back-end-programming-jobs", "remote-sales-and-marketing-jobs"]


def fixture_transport(overrides=None):
    overrides = overrides or {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url in overrides:
            r = overrides[url]
            if isinstance(r, Exception):
                raise r
            return r
        if "remotive.com" in url:
            return httpx.Response(200, content=(FIXTURES / "remotive/response.json").read_bytes())
        if "remoteok.com" in url:
            return httpx.Response(200, content=(FIXTURES / "remoteok/response.json").read_bytes())
        cat = url.rsplit("/", 1)[-1].removesuffix(".rss")
        return httpx.Response(200, content=(FIXTURES / "wwr" / f"{cat}.xml").read_bytes())

    return httpx.MockTransport(handler)


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def make_http(overrides=None, requests=None):
    clock = FakeClock()
    inner = fixture_transport(overrides)
    transport = inner
    if requests is not None:

        def counting(req):
            requests.append(str(req.url))
            return inner.handle_request(req)

        transport = httpx.MockTransport(counting)
    return HttpClient(
        transport, sleep=clock.sleep, clock=clock.now, resolver=PUBLIC_RESOLVER
    ), clock.sleeps


def all_connectors():
    return [
        connectors.REGISTRY["remotive"](),
        connectors.REGISTRY["remoteok"](),
        WWRConnector(CATS),
    ]


def count(store, table):
    return store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@pytest.fixture
def synced(store):
    store.sync_sources(connectors.REGISTRY, NOW)
    return store


def test_remotive_enabled_by_default_in_registry(synced):
    assert synced.source_state("remotive").enabled is True
    assert synced.source_state("remoteok").enabled is True


def test_collect_twice_no_duplicates(synced):
    synced.conn.execute("UPDATE sources SET enabled = 1 WHERE name = 'remotive'")
    http, _ = make_http()
    r1 = collect(synced, http, all_connectors(), limit=None, force=True, now=NOW)
    jobs1 = count(synced, "jobs")
    assert [r.status for r in r1] == ["ok", "ok", "ok"]
    assert jobs1 == 5 + 5 + 10 and all(r.new == r.found for r in r1)
    later = NOW.replace(hour=13)
    r2 = collect(synced, http, all_connectors(), limit=None, force=True, now=later)
    assert all(r.new == 0 for r in r2)
    assert count(synced, "jobs") == jobs1
    assert count(synced, "sightings") == 2 * jobs1
    assert count(synced, "source_runs") == 6


def test_changed_description_updates_row(synced):
    http, _ = make_http()
    c = [connectors.REGISTRY["remoteok"]()]
    collect(synced, http, c, limit=None, force=True, now=NOW)
    body = (FIXTURES / "remoteok/response.json").read_text()
    changed = body.replace("About the company", "About the firm", 1)
    assert changed != body
    http2, _ = make_http(
        {"https://remoteok.com/api": httpx.Response(200, content=changed.encode())}
    )
    collect(synced, http2, c, limit=None, force=True, now=NOW.replace(hour=13))
    assert count(synced, "jobs") == 5
    n = synced.conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE description_text LIKE '%About the firm%'"
    ).fetchone()[0]
    assert n == 1


def test_failing_sources_do_not_stop_others(synced, monkeypatch):
    monkeypatch.setattr("cvboost.http.random.uniform", lambda a, b: 1.0)
    synced.conn.execute("UPDATE sources SET enabled = 1 WHERE name = 'remotive'")
    overrides = {
        "https://remoteok.com/api": httpx.Response(500),
        "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss": (
            httpx.ReadTimeout("slow")
        ),
    }
    http, sleeps = make_http(overrides)
    res = collect(synced, http, all_connectors(), limit=None, force=True, now=NOW)
    by = {r.source: r for r in res}
    assert by["remotive"].status == "ok"
    assert by["remoteok"].status == "error" and by["remoteok"].http_status == 500
    # one of two WWR feeds fails: the run is ok with a partial note (see test_collect_partial)
    assert by["wwr"].status == "ok" and by["wwr"].error_type == "partial"
    # per failing request: 2s pause, 3s remaining host delay, 4s pause, 1s remaining host delay;
    # then the next WWR feed waits out the 5s per-host delay
    assert sleeps[:8] == [2, 3, 4, 1, 2, 3, 4, 1] and len(sleeps) == 9
    runs = {r["source"]: r for r in synced.conn.execute("SELECT * FROM source_runs")}
    assert runs["remoteok"]["status"] == "error" and runs["remoteok"]["http_status"] == 500
    assert runs["wwr"]["status"] == "ok" and "ReadTimeout" in runs["wwr"]["error_message"]
    assert count(synced, "jobs") == 5 + 5  # remotive + the WWR feed that worked


def test_exactly_three_attempts():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(500)

    http = HttpClient(httpx.MockTransport(handler), sleep=lambda s: None, resolver=PUBLIC_RESOLVER)
    from cvboost.http import FetchError

    with pytest.raises(FetchError):
        connectors.REGISTRY["remoteok"]().fetch(http)
    assert len(calls) == 3


def test_cli_sources_and_collect_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CVBOOST_DB", str(tmp_path / "cli.sqlite"))
    transport = fixture_transport({"https://remoteok.com/api": httpx.Response(500)})
    monkeypatch.setattr(
        cli,
        "HttpClient",
        lambda: HttpClient(transport, sleep=lambda s: None, resolver=PUBLIC_RESOLVER),
    )
    rc = cli.main(["collect", "--source", "remoteok", "--force"])
    assert rc == 1
    capsys.readouterr()
    assert cli.main(["sources"]) == 0
    out = capsys.readouterr().out
    assert "remoteok" in out and "FetchError" in out and "HTTP 500" in out
    assert "remotive" in out


def test_cli_collect_ok_then_jobs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CVBOOST_DB", str(tmp_path / "cli.sqlite"))
    transport = fixture_transport()
    monkeypatch.setattr(
        cli,
        "HttpClient",
        lambda: HttpClient(transport, sleep=lambda s: None, resolver=PUBLIC_RESOLVER),
    )
    assert cli.main(["collect", "--source", "remoteok"]) == 0
    capsys.readouterr()
    assert cli.main(["jobs", "--latest", "3"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 + 3 and "Feltsense" in out[1]


def test_cli_unknown_source(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CVBOOST_DB", str(tmp_path / "cli.sqlite"))
    assert cli.main(["collect", "--source", "nope"]) == 2
    assert "unknown source" in capsys.readouterr().err


def test_not_due_and_disabled_are_skipped(synced):
    synced.conn.execute("UPDATE sources SET enabled = 0 WHERE name = 'remotive'")
    http, _ = make_http()
    cs = all_connectors()
    res = collect(synced, http, cs, limit=None, force=False, now=NOW)
    st = {r.source: r.status for r in res}
    assert st == {"remotive": "skipped", "remoteok": "ok", "wwr": "ok"}
    res2 = collect(synced, http, cs, limit=None, force=False, now=NOW.replace(minute=30))
    assert {r.status for r in res2} == {"skipped"}
    assert count(synced, "source_runs") == 2  # skipped writes no rows
    res3 = collect(synced, http, cs, limit=None, force=False, now=NOW.replace(hour=14))
    assert {r.source: r.status for r in res3}["remoteok"] == "ok"


def test_limit(synced):
    http, _ = make_http()
    res = collect(synced, http, [connectors.REGISTRY["remoteok"]()], limit=2, force=True, now=NOW)
    assert res[0].found == 2 and count(synced, "jobs") == 2


def test_force_does_not_bypass_disabled(synced):
    synced.conn.execute("UPDATE sources SET enabled = 0 WHERE name = 'remotive'")
    requests: list[str] = []
    http, _ = make_http(requests=requests)
    res = collect(synced, http, all_connectors(), limit=None, force=True, now=NOW)
    st = {r.source: (r.status, r.skip_reason) for r in res}
    assert st["remotive"] == ("skipped", "disabled")
    assert not any("remotive.com" in u for u in requests)
    assert count(synced, "source_runs") == 2


def test_explicitly_named_disabled_source_runs(synced):
    synced.conn.execute("UPDATE sources SET enabled = 0 WHERE name = 'remotive'")
    requests: list[str] = []
    http, _ = make_http(requests=requests)
    c = [connectors.REGISTRY["remotive"]()]
    res = collect(synced, http, c, limit=None, force=True, now=NOW, named=frozenset({"remotive"}))
    assert res[0].status == "ok" and any("remotive.com" in u for u in requests)


def test_one_invalid_record_others_saved(synced):
    body = (FIXTURES / "wwr/remote-back-end-programming-jobs.xml").read_text()
    bad = body.replace("Vertican: Senior", "Vertican Senior", 1)
    assert bad != body
    url = "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss"
    http, _ = make_http({url: httpx.Response(200, content=bad.encode())})
    c = [WWRConnector(["remote-back-end-programming-jobs"])]
    res = collect(synced, http, c, limit=None, force=True, now=NOW)
    assert (res[0].status, res[0].found, res[0].new, res[0].invalid) == ("ok", 5, 4, 1)
    assert count(synced, "jobs") == 4


def test_all_invalid_is_error(synced):
    body = (FIXTURES / "wwr/remote-back-end-programming-jobs.xml").read_text()
    bad = body.replace("</title>", "</title>").replace(": ", " - ")
    url = "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss"
    http, _ = make_http({url: httpx.Response(200, content=bad.encode())})
    c = [WWRConnector(["remote-back-end-programming-jobs"])]
    res = collect(synced, http, c, limit=None, force=True, now=NOW)
    assert res[0].status == "error" and res[0].error_type == "AllInvalid"
    run = synced.conn.execute("SELECT * FROM source_runs").fetchone()
    assert run["status"] == "error" and run["error_type"] == "AllInvalid" and run["invalid"] == 5
    assert count(synced, "jobs") == 0


def test_write_failure_rolls_back_everything(synced, monkeypatch):
    real = synced.upsert_job
    calls = []

    def flaky(job, run_id, now):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("disk full")
        return real(job, run_id, now)

    monkeypatch.setattr(synced, "upsert_job", flaky)
    http, _ = make_http()
    res = collect(
        synced, http, [connectors.REGISTRY["remoteok"]()], limit=None, force=True, now=NOW
    )
    assert res[0].status == "error" and res[0].error_type == "RuntimeError"
    assert count(synced, "jobs") == 0 and count(synced, "sightings") == 0
    run = synced.conn.execute("SELECT * FROM source_runs").fetchone()
    assert run["status"] == "error" and run["error_type"] == "RuntimeError"
    assert not synced.conn.in_transaction


def test_interval_override_is_honoured(synced):
    http, _ = make_http()
    c = [connectors.REGISTRY["remoteok"]()]  # default interval 1h
    synced.conn.execute("UPDATE sources SET interval_override_s = 7200 WHERE name='remoteok'")
    assert collect(synced, http, c, limit=None, force=False, now=NOW)[0].status == "ok"
    at_90m = NOW.replace(hour=13, minute=30)
    assert collect(synced, http, c, limit=None, force=False, now=at_90m)[0].status == "skipped"
    at_2h = NOW.replace(hour=14)
    assert collect(synced, http, c, limit=None, force=False, now=at_2h)[0].status == "ok"


def test_source_redirecting_to_http_is_an_error_others_stay_ok(synced):
    http, _ = make_http(
        {
            "https://remoteok.com/api": httpx.Response(
                302, headers={"Location": "http://remoteok.com/api"}
            )
        }
    )
    res = collect(synced, http, all_connectors(), limit=None, force=True, now=NOW)
    by = {r.source: r for r in res}
    assert by["remoteok"].status == "error" and by["remoteok"].error_type == "UnsafeURLError"
    assert by["remotive"].status == "ok" and by["wwr"].status == "ok"
    row = synced.conn.execute(
        "SELECT status, error_type FROM source_runs WHERE source = 'remoteok'"
    ).fetchone()
    assert (row["status"], row["error_type"]) == ("error", "UnsafeURLError")
