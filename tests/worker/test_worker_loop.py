from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from cvboost.config import Settings
from cvboost.locks import try_lock
from cvboost.storage.db import Store, connect, migrate
from cvboost.worker import (
    backfill_night,
    calls_for_cycle,
    run_cycle,
    run_worker,
    worker_healthy,
)
from cvboost.worker_config import WorkerConfig
from tests.conftest import NOW
from tests.worker.helpers import FakeClock, FakeConnector, ScoreRecorder, make_deps


@pytest.fixture
def wstore(tmp_path):
    conn = connect(tmp_path / "w.sqlite")
    migrate(conn)
    return Store(conn)


def cycles(store):
    return [dict(r) for r in store.conn.execute("SELECT * FROM worker_cycles ORDER BY id")]


def sync(store, *conns):
    store.sync_sources({c.name: type(c) for c in conns}, NOW)


def test_cycle_collects_triages_scores(wstore, tmp_path):
    c = FakeConnector(ids=["1", "2"])
    sync(wstore, c)
    rec = ScoreRecorder(scored=2, calls=1)
    deps = make_deps(tmp_path, [c], score_fn=rec)
    st = run_cycle(wstore, deps)
    assert st.status == "ok" and st.new_jobs == 2 and st.scored == 2
    assert (
        wstore.conn.execute("SELECT COUNT(*) FROM verdicts WHERE stage='rules'").fetchone()[0] == 2
    )
    assert rec.calls[0]["max_calls"] == 1
    [cy] = cycles(wstore)
    assert cy["status"] == "ok" and cy["new_jobs"] == 2 and cy["finished_at"]
    assert wstore.state_get_dt("heartbeat") is not None
    # not due again within the same tick
    st2 = run_cycle(wstore, deps)
    assert st2.sources_run == 0 and c.fetches == 1


def test_collect_failure_does_not_stop_triage_score_notify(wstore, tmp_path):
    ok = FakeConnector("okay", ids=["1"])
    bad = FakeConnector("bad", boom=True)
    sync(wstore, ok, bad)
    notified = []
    rec = ScoreRecorder()
    deps = make_deps(tmp_path, [bad, ok], score_fn=rec, notify=lambda s, n, st: notified.append(1))
    st = run_cycle(wstore, deps)
    assert st.sources_failed == 1 and st.status == "partial"  # a failed source is not a step error
    assert len(rec.calls) == 1 and notified == [1]


def test_step_exception_is_isolated_and_next_cycle_is_fine(wstore, tmp_path, caplog):
    c = FakeConnector(ids=["1"])
    sync(wstore, c)
    rec = ScoreRecorder()
    notified = []

    def broken_triage(*a, **k):
        raise RuntimeError("triage exploded")

    deps = make_deps(tmp_path, [c], score_fn=rec, notify=lambda s, n, st: notified.append(1))
    import cvboost.worker as w

    orig = w.triage
    w.triage = broken_triage
    try:
        st = run_cycle(wstore, deps)
    finally:
        w.triage = orig
    assert st.status == "error" and "triage: RuntimeError" in st.errors
    assert len(rec.calls) == 1 and notified == [1]
    [cy] = cycles(wstore)
    assert cy["status"] == "error" and "triage" in cy["error"]
    deps.clock.advance(minutes=11)
    assert run_cycle(wstore, deps).status == "ok"


def test_pipeline_busy_skips_cycle(wstore, tmp_path):
    c = FakeConnector(ids=["1"])
    sync(wstore, c)
    held = try_lock(tmp_path / "pipeline.lock")
    assert held is not None
    deps = make_deps(tmp_path, [c], score_fn=ScoreRecorder())
    st = run_cycle(wstore, deps)
    assert st.status == "error" and st.errors == ["pipeline busy"] and c.fetches == 0
    held.release()
    assert run_cycle(wstore, deps).status == "ok"


def test_collect_deadline_defers_sources(wstore, tmp_path):
    a, b = FakeConnector("a", ids=["1"]), FakeConnector("b", ids=["2"])
    sync(wstore, a, b)
    clock = FakeClock(step=200)  # every clock read costs 200 s
    deps = make_deps(
        tmp_path, [a, b], clock=clock, cfg={"collect_budget_s": 300, "cycle_deadline_s": 900}
    )
    deps.score_fn = ScoreRecorder()
    st = run_cycle(wstore, deps)
    assert st.sources_run + st.sources_deferred == 2 and st.sources_deferred >= 1


def test_score_skipped_when_no_time_left(wstore, tmp_path):
    c = FakeConnector(ids=["1"])
    sync(wstore, c)
    rec = ScoreRecorder()
    deps = make_deps(
        tmp_path,
        [c],
        clock=FakeClock(step=100),
        cfg={"collect_budget_s": 300, "cycle_deadline_s": 400},
        score_fn=rec,
    )
    st = run_cycle(wstore, deps)  # llm timeout 180 s: latest start = t0 + 220 s
    assert rec.calls == [] and st.score_stopped == "no time left in this cycle"


def test_stop_before_score_skips_it(wstore, tmp_path):
    c = FakeConnector(ids=["1"])
    sync(wstore, c)
    rec = ScoreRecorder()
    deps = make_deps(tmp_path, [c], score_fn=rec)
    deps.stop.set()
    run_cycle(wstore, deps)
    assert rec.calls == []


# --- backfill ---


def lisbon(h, m=0):
    # 2026-11-06 is winter time: Lisbon == UTC
    return NOW.replace(hour=h, minute=m)


def test_backfill_budget(wstore):
    cfg = WorkerConfig()
    assert calls_for_cycle(wstore, cfg, lisbon(3)) == (4, "2026-11-06")
    assert calls_for_cycle(wstore, cfg, lisbon(12)) == (1, None)
    wstore.state_set("backfill:2026-11-06", "18", NOW)
    assert calls_for_cycle(wstore, cfg, lisbon(3)) == (2, "2026-11-06")
    wstore.state_set("backfill:2026-11-06", "20", NOW)
    assert calls_for_cycle(wstore, cfg, lisbon(3)) == (1, None)
    assert backfill_night(lisbon(6, 0), cfg) is None and backfill_night(lisbon(1, 59), cfg) is None


def test_backfill_counts_calls_in_the_window(wstore, tmp_path):
    c = FakeConnector(ids=["1"])
    sync(wstore, c)
    rec = ScoreRecorder(calls=3)
    deps = make_deps(tmp_path, [c], clock=FakeClock(lisbon(3)), score_fn=rec)
    run_cycle(wstore, deps)
    assert rec.calls[0]["max_calls"] == 4
    assert wstore.state_get("backfill:2026-11-06") == "3"


def test_backfill_window_across_midnight():
    cfg = WorkerConfig.model_validate({"backfill": {"window": ["23:00", "05:00"]}})
    assert backfill_night(lisbon(23, 30), cfg) == "2026-11-06"
    assert backfill_night(lisbon(1), cfg) == "2026-11-05"
    assert backfill_night(lisbon(12), cfg) is None
    assert (
        backfill_night(lisbon(3), WorkerConfig.model_validate({"backfill": {"enabled": False}}))
        is None
    )


# --- run_worker ---


def settings_for(tmp_path) -> Settings:
    return Settings(
        db_path=tmp_path / "data" / "w.sqlite",
        log_level="INFO",
        rules_path=tmp_path / "rules.yaml",
        llm_config_path=tmp_path / "llm.yaml",
        worker_config_path=tmp_path / "worker.yaml",
    )


def test_run_worker_once_and_lock_released(tmp_path):
    s = settings_for(tmp_path)
    c = FakeConnector(ids=["1"])
    holder = {}

    def factory(settings, stop):
        deps = make_deps(settings.data_dir, [c], score_fn=ScoreRecorder())
        holder["deps"] = deps
        return deps

    assert run_worker(s, once=True, deps_factory=factory) == 0
    assert c.fetches == 1
    lock = try_lock(s.data_dir / "worker.lock")
    assert lock is not None  # released
    lock.release()
    conn = connect(s.db_path)
    assert Store(conn).state_get("alerts_since") is not None
    assert Store(conn).conn.execute("SELECT COUNT(*) FROM worker_cycles").fetchone()[0] == 1


def test_run_worker_stops_on_event_from_score(tmp_path):
    s = settings_for(tmp_path)
    c = FakeConnector(ids=["1"])
    stop = threading.Event()
    rec = ScoreRecorder()
    rec.on_call = stop.set

    def factory(settings, st):
        return make_deps(settings.data_dir, [c], score_fn=rec, stop=st)

    assert run_worker(s, deps_factory=factory, stop=stop) == 0
    assert len(rec.calls) == 1
    lock = try_lock(s.data_dir / "worker.lock")
    assert lock is not None
    lock.release()


def test_run_worker_loops_with_tick_pause(tmp_path):
    s = settings_for(tmp_path)
    c = FakeConnector(ids=["1"])
    stop = threading.Event()
    waits = []
    real_wait = stop.wait

    def wait(t):
        waits.append(t)
        if len(waits) >= 2:
            stop.set()
        return real_wait(0)

    stop.wait = wait  # type: ignore[method-assign]

    def factory(settings, st):
        return make_deps(
            settings.data_dir, [c], clock=FakeClock(step=60), score_fn=ScoreRecorder(), stop=st
        )

    run_worker(s, deps_factory=factory, stop=stop)
    assert len(waits) == 2 and all(w >= 30 for w in waits)
    assert waits[0] < 600  # tick minus the time the cycle took


def test_interrupted_cycle_is_marked_on_start(tmp_path):
    s = settings_for(tmp_path)
    conn = connect(s.db_path)
    migrate(conn)
    st = Store(conn)
    st.start_cycle(NOW - timedelta(hours=1))
    conn.close()
    c = FakeConnector(ids=["1"])
    run_worker(
        s,
        once=True,
        deps_factory=lambda se, sto: make_deps(se.data_dir, [c], score_fn=ScoreRecorder()),
    )
    conn = connect(s.db_path)
    statuses = [r[0] for r in conn.execute("SELECT status FROM worker_cycles ORDER BY id")]
    assert statuses == ["interrupted", "ok"]


def test_health(tmp_path, monkeypatch):
    s = settings_for(tmp_path)
    cfgfile = tmp_path / "worker.yaml"
    cfgfile.write_text("tick_minutes: 10\n")
    assert worker_healthy(s) is False  # no DB yet
    conn = connect(s.db_path)
    migrate(conn)
    st = Store(conn)
    assert worker_healthy(s, NOW) is False  # no heartbeat
    st.state_set("heartbeat", (NOW - timedelta(minutes=20)).isoformat(), NOW)
    assert worker_healthy(s, NOW) is True
    st.state_set("heartbeat", (NOW - timedelta(minutes=31)).isoformat(), NOW)
    assert worker_healthy(s, NOW) is False
