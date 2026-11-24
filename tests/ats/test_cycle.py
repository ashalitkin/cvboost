"""One collect run of an ATS connector: slice per cycle, time budget, 404, interval, failures."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from cvboost import ats_state
from cvboost.collect import collect
from cvboost.connectors.ashby import AshbyConnector
from cvboost.connectors.greenhouse import GreenhouseConnector
from cvboost.connectors.lever import LeverConnector
from tests.ats.helpers import ats_http, fixture_response, write_companies
from tests.conftest import NOW


@pytest.fixture(autouse=True)
def _no_jitter(monkeypatch):
    monkeypatch.setattr("cvboost.http.random.uniform", lambda a, b: 1.0)


def state(store, ats, slug):
    return store.conn.execute(
        "SELECT * FROM ats_companies WHERE ats=? AND slug=?", (ats, slug)
    ).fetchone()


def run(store, connector, http, *, at=NOW, force=False):
    store.sync_sources({connector.name: type(connector)}, at)
    [res] = collect(store, http, [connector], limit=None, force=force, now=at)
    return res


def test_stores_matching_postings_only_with_salary(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("ashby", "linear", "Linear")])
    c = AshbyConnector(path)
    res = run(store, c, ats_http({"linear": fixture_response("ashby", "linear")}))
    assert res.status == "ok" and res.found == 3 and res.new == 3 and res.invalid == 0
    rows = store.conn.execute(
        "SELECT title, company, salary_min FROM jobs ORDER BY title"
    ).fetchall()
    assert [r["title"] for r in rows] == [
        "Account Executive, Growth", "Analytics Engineer", "Product Marketing Manager",
    ]  # fmt: skip
    assert {r["company"] for r in rows} == {"Linear"}
    s = state(store, "ashby", "linear")
    assert s["last_ok_at"] and s["last_found"] == 3 and s["active"] == 1


def two_404s(store, connector_cls, path, http):
    """Two checks 6 h + 1 min apart, as the worker would make them."""
    run(store, connector_cls(path), http, force=True)
    run(store, connector_cls(path), http, at=NOW + timedelta(hours=6, minutes=1), force=True)


def test_first_404_is_only_a_strike_second_one_makes_it_inactive(store, tmp_path):
    path = write_companies(
        tmp_path / "c.yaml", [("lever", "gone-co", "Gone"), ("lever", "findem", "F")]
    )
    seen: list[str] = []
    http = ats_http({"findem": fixture_response("lever", "findem")}, seen)
    res = run(store, LeverConnector(path), http)
    assert res.status == "ok"  # one dead company is not a failed run
    s = state(store, "lever", "gone-co")
    assert s["active"] == 1 and s["gone_strikes"] == 1  # one 404 is not enough
    run(store, LeverConnector(path), http, at=NOW + timedelta(hours=6, minutes=1), force=True)
    s = state(store, "lever", "gone-co")
    assert s["active"] == 0 and s["inactive_reason"] == "HTTP 404 x2" and s["inactive_since"]
    # a week later gone-co is not asked again, findem is
    seen.clear()
    run(store, LeverConnector(path), http, at=NOW + timedelta(days=7), force=True)
    assert seen == ["api.lever.co/findem"]


def test_two_404s_less_than_six_hours_apart_do_not_count(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("lever", "gone-co", "Gone")])
    seen: list[str] = []
    http = ats_http({}, seen)
    run(store, LeverConnector(path), http)
    run(store, LeverConnector(path), http, at=NOW + timedelta(hours=3), force=True)
    assert len(seen) == 1  # not due: the second check waits for 6 h
    assert state(store, "lever", "gone-co")["active"] == 1


def test_a_200_between_two_404s_resets_the_strike(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("lever", "flaky", "Flaky")])
    answers = [httpx.Response(404), httpx.Response(200, json=[]), httpx.Response(404)]
    http = ats_http({"flaky": lambda: answers.pop(0)})
    for h in (0, 7, 14):
        run(store, LeverConnector(path), http, at=NOW + timedelta(hours=h), force=True)
    s = state(store, "lever", "flaky")
    assert s["active"] == 1 and s["gone_strikes"] == 1


def test_410_counts_like_404(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("greenhouse", "old-co", "Old")])
    http = ats_http({"old-co": httpx.Response(410)})
    two_404s(store, GreenhouseConnector, path, http)
    assert state(store, "greenhouse", "old-co")["active"] == 0


def test_inactive_stays_inactive_when_the_yaml_still_lists_it(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("ashby", "dead", "Dead")])
    seen: list[str] = []
    http = ats_http({}, seen)
    two_404s(store, AshbyConnector, path, http)
    assert state(store, "ashby", "dead")["active"] == 0
    seen.clear()
    run(store, AshbyConnector(path), http, at=NOW + timedelta(days=20), force=True)
    assert seen == []


def test_inactive_company_is_rechecked_after_30_days_and_can_return(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("ashby", "back", "Back")])
    answers = [httpx.Response(404), httpx.Response(404), httpx.Response(200, json={"jobs": []})]
    http = ats_http({"back": lambda: answers.pop(0)})
    two_404s(store, AshbyConnector, path, http)
    assert state(store, "ashby", "back")["active"] == 0
    run(store, AshbyConnector(path), http, at=NOW + timedelta(days=31), force=True)
    s = state(store, "ashby", "back")
    assert s["active"] == 1 and s["inactive_since"] is None and s["gone_strikes"] == 0


def test_inactive_recheck_that_is_404_again_waits_another_30_days(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("ashby", "dead", "Dead")])
    seen: list[str] = []
    http = ats_http({}, seen)
    two_404s(store, AshbyConnector, path, http)
    run(store, AshbyConnector(path), http, at=NOW + timedelta(days=31), force=True)
    assert len(seen) == 3 and state(store, "ashby", "dead")["active"] == 0
    run(store, AshbyConnector(path), http, at=NOW + timedelta(days=45), force=True)
    assert len(seen) == 3


def test_reactivate(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("ashby", "dead", "Dead")])
    two_404s(store, AshbyConnector, path, ats_http({}))
    assert ats_state.reactivate(store, "ashby", "dead") is True
    s = state(store, "ashby", "dead")
    assert s["active"] == 1 and s["next_due_at"] is None and s["gone_strikes"] == 0
    assert ats_state.reactivate(store, "ashby", "nobody") is False


def test_company_interval_is_six_hours_with_ten_percent_jitter(store, tmp_path, monkeypatch):
    path = write_companies(tmp_path / "c.yaml", [("lever", "findem", "F")])
    seen: list[str] = []
    http = ats_http({"findem": fixture_response("lever", "findem")}, seen)
    monkeypatch.setattr("cvboost.ats_state.random.uniform", lambda a, b: 1.0)
    run(store, LeverConnector(path), http)
    assert len(seen) == 1
    run(store, LeverConnector(path), http, at=NOW + timedelta(hours=5, minutes=59), force=True)
    assert len(seen) == 1  # not due
    run(store, LeverConnector(path), http, at=NOW + timedelta(hours=6, minutes=1), force=True)
    assert len(seen) == 2


@pytest.mark.parametrize(("factor", "hours"), [(0.9, 5.4), (1.1, 6.6)])
def test_jitter_bounds(store, tmp_path, monkeypatch, factor, hours):
    path = write_companies(tmp_path / "c.yaml", [("lever", "findem", "F")])
    http = ats_http({"findem": httpx.Response(200, json=[])})
    asked = []
    monkeypatch.setattr(
        "cvboost.ats_state.random.uniform", lambda a, b: asked.append((a, b)) or factor
    )
    run(store, LeverConnector(path), http)
    assert asked == [(0.9, 1.1)]
    due = store.conn.execute("SELECT next_due_at FROM ats_companies").fetchone()[0]
    assert datetime.fromisoformat(due) == NOW + timedelta(hours=hours)


def test_cycle_company_limit_and_round_robin(store, tmp_path):
    slugs = [f"co-{i:02d}" for i in range(10)]
    path = write_companies(tmp_path / "c.yaml", [("lever", s, s) for s in slugs])

    class Small(LeverConnector):
        max_companies = 4

    seen: list[str] = []
    http = ats_http({s: httpx.Response(200, json=[]) for s in slugs}, seen)
    for i in range(3):  # three 10-minute cycles
        run(store, Small(path), http, at=NOW + timedelta(minutes=10 * i), force=True)
        assert len(seen) == min(4 * (i + 1), 10)
    assert sorted(seen) == sorted(f"api.lever.co/{s}" for s in slugs)  # each exactly once
    run(store, Small(path), http, at=NOW + timedelta(minutes=40), force=True)
    assert len(seen) == 10  # all are within 6h: nothing more


def test_time_budget_stops_the_run_and_leaves_the_rest_for_the_next_cycle(store, tmp_path):
    slugs = [f"co-{i}" for i in range(6)]
    path = write_companies(tmp_path / "c.yaml", [("lever", s, s) for s in slugs])
    t = [0.0]

    def tick() -> float:
        t[0] += 10.0  # every check of the clock costs 10 s
        return t[0]

    class Short(LeverConnector):
        time_budget_s = 25.0

    seen: list[str] = []
    http = ats_http({s: httpx.Response(200, json=[]) for s in slugs}, seen)
    run(store, Short(path, monotonic=tick), http)
    assert 0 < len(seen) < 6
    done = len(seen)
    assert state(store, "lever", slugs[done])["last_checked_at"] is None  # untouched, still due
    run(store, Short(path), http, at=NOW + timedelta(minutes=10), force=True)
    assert len(seen) == 6


def test_real_limits_are_short_enough_for_a_10_minute_cycle():
    for cls in (GreenhouseConnector, LeverConnector, AshbyConnector):
        assert cls.min_delay_s >= 1.0
        assert cls.max_companies * cls.min_delay_s <= 60
        assert cls.time_budget_s <= 120
        assert cls.company_interval == timedelta(hours=6)
        assert cls.interval <= timedelta(minutes=10)


def test_all_companies_failing_is_a_failed_run_and_counts(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("ashby", "a-co", "A"), ("ashby", "b-co", "B")])
    http = ats_http({"a-co": httpx.Response(503), "b-co": httpx.Response(500)})
    res = run(store, AshbyConnector(path), http)
    assert res.status == "error" and res.http_status in (500, 503)
    s = state(store, "ashby", "a-co")
    assert s["active"] == 1 and s["consecutive_failures"] == 1 and s["last_status"] == 503
    assert s["last_ok_at"] is None


def test_one_failing_company_is_a_partial_run(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("ashby", "linear", "L"), ("ashby", "bad", "B")])
    http = ats_http({"linear": fixture_response("ashby", "linear"), "bad": httpx.Response(503)})
    res = run(store, AshbyConnector(path), http)
    assert res.status == "ok" and res.error_type == "partial" and res.found == 3
    assert state(store, "ashby", "bad")["consecutive_failures"] == 1
    assert state(store, "ashby", "linear")["consecutive_failures"] == 0


def test_bad_json_counts_as_a_failure_not_a_crash(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("greenhouse", "weird", "W")])
    res = run(
        store, GreenhouseConnector(path), ats_http({"weird": httpx.Response(200, text="<html>")})
    )
    assert (
        res.status == "error" and state(store, "greenhouse", "weird")["consecutive_failures"] == 1
    )


def test_only_the_fixed_host_is_contacted_and_removal_from_yaml_stops_polling(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("lever", "findem", "F"), ("lever", "x-co", "X")])
    seen: list[str] = []
    http = ats_http(
        {"findem": fixture_response("lever", "findem"), "x-co": httpx.Response(200, json=[])}, seen
    )
    run(store, LeverConnector(path), http)
    assert {s.split("/")[0] for s in seen} == {"api.lever.co"}
    write_companies(path, [("lever", "findem", "F")])
    seen.clear()
    run(store, LeverConnector(path), http, at=NOW + timedelta(days=1), force=True)
    assert seen == ["api.lever.co/findem"]


def test_invalid_slugs_in_the_yaml_are_dropped_not_fetched(store, tmp_path):
    path = write_companies(
        tmp_path / "c.yaml",
        [("lever", "../etc", "x"), ("lever", "A_B", "x"), ("lever", "findem", "F")],
    )
    seen: list[str] = []
    http = ats_http({"findem": fixture_response("lever", "findem")}, seen)
    res = run(store, LeverConnector(path), http)
    assert res.status == "ok" and seen == ["api.lever.co/findem"]
    assert store.conn.execute("SELECT COUNT(*) FROM ats_companies").fetchone()[0] == 1


def test_a_slug_that_slipped_into_the_batch_is_refused(store, tmp_path):
    c = LeverConnector(tmp_path / "none.yaml")
    c._batch = ["../x", "findem"]
    c._store, c._now = store, NOW
    seen: list[str] = []
    out = c.fetch_partial(ats_http({"findem": httpx.Response(200, json=[])}, seen))
    assert seen == ["api.lever.co/findem"] and out.requests_total == 1


def test_nothing_due_writes_no_run_at_all(store, tmp_path):
    res = run(store, LeverConnector(tmp_path / "absent.yaml"), ats_http({}))
    assert res.status == "skipped" and res.skip_reason == "nothing due"
    assert store.conn.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0] == 0


def test_counts(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("lever", "findem", "F"), ("lever", "gone", "G")])
    run(store, LeverConnector(path), ats_http({"findem": fixture_response("lever", "findem")}))
    run(store, LeverConnector(path), ats_http({}), at=NOW + timedelta(hours=7), force=True)
    assert ats_state.counts(store) == {"lever": {"active": 1, "inactive": 1}}


def test_migration_0008_is_additive_and_constrains(store):
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(ats_companies)")}
    assert {"ats", "slug", "active", "last_checked_at", "inactive_since"} <= cols
    import sqlite3

    for bad in (("workday", "x"), ("lever", ""), ("lever", "x" * 65)):
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute(
                "INSERT INTO ats_companies (ats, slug, added_at) VALUES (?, ?, 'x')", bad
            )


def test_registry_hands_the_companies_path_to_ats_connectors(tmp_path):
    from cvboost import connectors

    got = connectors.get(["greenhouse", "lever", "ashby"], companies_path=tmp_path / "x.yaml")
    assert [type(c).__name__ for c in got] == [
        "GreenhouseConnector", "LeverConnector", "AshbyConnector",
    ]  # fmt: skip
    assert all(c.companies_path == tmp_path / "x.yaml" for c in got)
    assert {c.name for c in connectors.get(None)} >= {"greenhouse", "lever", "ashby"}


def test_cli_collect_runs_an_ats_source_from_the_configured_list(tmp_path, monkeypatch, capsys):
    from cvboost import cli

    path = write_companies(tmp_path / "c.yaml", [("lever", "findem", "Findem")])
    monkeypatch.setenv("CVBOOST_DB", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("CVBOOST_COMPANIES", str(path))
    http = ats_http({"findem": fixture_response("lever", "findem")})
    monkeypatch.setattr("cvboost.cli.HttpClient", lambda: http)
    assert cli.main(["collect", "--source", "lever", "--force"]) == 0
    assert "lever: ok, found 3, new 3" in capsys.readouterr().out
