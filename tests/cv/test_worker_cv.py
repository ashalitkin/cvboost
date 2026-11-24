from __future__ import annotations

import yaml

from cvboost.cv.step import run_cv_step
from cvboost.notify.alerts import send_pending
from cvboost.worker import run_cycle
from cvboost.worker_config import WorkerConfig
from tests.conftest import NOW
from tests.cv.helpers import (
    ROOT,
    FakeRenderer,
    cv_cfg,
    make_settings,
    ok_draft,
    queue_request,
)
from tests.llm.helpers import FakeClient
from tests.notify.helpers import OWNER, TOKEN, FakeTelegram
from tests.worker.helpers import FakeClock, ScoreRecorder, make_deps


def cv_deps(tmp_path, client, store_settings=None, **over):
    settings = store_settings or make_settings(tmp_path)
    cfg = cv_cfg()
    clock = FakeClock()
    renderer = FakeRenderer()
    deps = make_deps(
        tmp_path,
        [],
        clock=clock,
        score_fn=ScoreRecorder(),
        llm_client=client,
        llm_cfg=cfg,
        cv_step=lambda store, c, clk: run_cv_step(store, c, cfg, settings, renderer, clk),
        **over,
    )
    return deps, renderer


def test_one_cv_call_per_cycle(store, tmp_path):
    queue_request(store, 2)
    client = FakeClient(ok_draft(), ok_draft())
    deps, _ = cv_deps(tmp_path, client)
    st = run_cycle(store, deps)
    assert st.status == "ok" and st.cv_done == 1 and len(client.requests) == 1
    calls = store.conn.execute("SELECT COUNT(*) FROM llm_calls WHERE kind='cv'").fetchone()[0]
    assert calls == 1
    assert [
        r["status"] for r in store.conn.execute("SELECT status FROM cv_requests ORDER BY id")
    ] == [
        "done",
        "queued",
    ]
    st2 = run_cycle(store, deps)
    assert st2.cv_done == 1 and len(client.requests) == 2


def test_cv_runs_after_notify_and_notifies_again_when_it_finished(store, tmp_path):
    queue_request(store, 1)
    order: list[str] = []
    deps, _ = cv_deps(tmp_path, FakeClient(ok_draft()))
    deps.notify = lambda s, now, stats: order.append("notify")
    inner = deps.cv_step
    deps.cv_step = lambda s, c, clk: (order.append("cv"), inner(s, c, clk))[1]
    run_cycle(store, deps)
    assert order == ["notify", "cv", "notify"]


def test_idle_cv_step_does_not_renotify(store, tmp_path):
    order: list[str] = []
    deps, _ = cv_deps(tmp_path, FakeClient(ok_draft()))
    deps.notify = lambda s, now, stats: order.append("notify")
    run_cycle(store, deps)
    assert order == ["notify"]


def test_cv_failure_does_not_break_the_cycle(store, tmp_path):
    deps, _ = cv_deps(tmp_path, FakeClient())

    def boom(*a):
        raise RuntimeError("cv exploded")

    deps.cv_step = boom
    st = run_cycle(store, deps)
    assert st.status == "error" and "cv: RuntimeError" in st.errors


def test_cv_off_when_no_step(store, tmp_path):
    queue_request(store, 1)
    deps, _ = cv_deps(tmp_path, FakeClient(ok_draft()))
    deps.cv_step = None
    assert run_cycle(store, deps).cv_done == 0


def test_cv_ready_reaches_telegram_with_the_panel_link_intact(store, tmp_path):
    ((rid, _, cid),) = queue_request(store)
    run_cv_step(
        store,
        FakeClient(ok_draft()),
        cv_cfg(),
        make_settings(tmp_path),
        FakeRenderer(),
        FakeClock(),
    )
    fake = FakeTelegram()
    cfg = WorkerConfig.model_validate({})
    send_pending(store, fake.client(), cfg, OWNER, NOW, token=TOKEN, kept_hashes={})
    [msg] = [p for p in fake.of("sendMessage")]
    assert f"https://panel.example.com/jobs/{cid}" in msg["text"]
    assert msg["text"].startswith("CV ready:")


def test_untrusted_job_text_is_neutralised_in_cv_alerts(store, tmp_path):
    ((rid, jid, cid),) = queue_request(store)
    store.conn.execute(
        "UPDATE jobs SET title='Dev @everyone http://evil.example/x', "
        "company='a.b.example' WHERE id=?",
        (jid,),
    )
    run_cv_step(
        store,
        FakeClient(ok_draft()),
        cv_cfg(),
        make_settings(tmp_path),
        FakeRenderer(),
        FakeClock(),
    )
    text = store.conn.execute("SELECT text FROM system_alerts").fetchone()[0]
    assert "evil.example" not in text and "@everyone" not in text and "a.b.example" not in text


def test_build_deps_wires_the_step_only_when_enabled(tmp_path, monkeypatch):
    from cvboost.worker import build_deps

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    import threading

    llm = yaml.safe_load((ROOT / "tests" / "fixtures" / "config" / "llm.yaml").read_text())
    llm["cv"]["enabled"] = True
    on = tmp_path / "llm_on.yaml"
    on.write_text(yaml.safe_dump(llm))
    s_on = make_settings(
        tmp_path,
        worker_config_path=ROOT / "tests" / "fixtures" / "config" / "worker.yaml",
        llm_config_path=on,
    )
    assert build_deps(s_on, threading.Event()).cv_step is not None
    llm["cv"]["enabled"] = False
    p = tmp_path / "llm.yaml"
    p.write_text(yaml.safe_dump(llm))
    s2 = make_settings(
        tmp_path,
        worker_config_path=ROOT / "tests" / "fixtures" / "config" / "worker.yaml",
        llm_config_path=p,
    )
    assert build_deps(s2, threading.Event()).cv_step is None


def test_compose_grace_period_covers_the_longest_llm_call():
    c = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    llm = yaml.safe_load((ROOT / "tests" / "fixtures" / "config" / "llm.yaml").read_text())
    longest = max(llm["timeout_s"], llm["cv"]["timeout_s"])
    for name in ("worker",):
        grace = int(str(c["services"][name]["stop_grace_period"]).rstrip("s"))
        assert grace >= longest + 35
    for name in ("app", "worker"):
        assert c["services"][name]["environment"]["CVBOOST_KITS_DIR"] == "/data/kits"


def test_dockerfile_installs_the_pdf_libraries():
    text = (ROOT / "Dockerfile").read_text()
    for pkg in ("libpango-1.0-0", "libpangoft2-1.0-0", "libharfbuzz-subset0", "fonts-liberation2"):
        assert pkg in text
    assert "chromium" not in text.lower() and "pandoc" not in text.lower()


# --- fast path: "Готовить CV" starts between cycles (owner 2026-11-10) ---------------------


def test_cv_fast_path_runs_one_kit_without_a_cycle(store, tmp_path):
    from cvboost.worker import run_cv_now

    queue_request(store, 1)
    client = FakeClient(ok_draft())
    deps, _ = cv_deps(tmp_path, client)
    st = run_cv_now(store, deps)
    assert st.cv_done == 1 and len(client.requests) == 1


def test_cv_fast_path_runs_queued_kits_back_to_back(store, tmp_path):
    from cvboost.worker import run_cv_now

    queue_request(store, 2)
    client = FakeClient(ok_draft(), ok_draft())
    deps, _ = cv_deps(tmp_path, client)
    st = run_cv_now(store, deps)
    assert st.cv_done == 2 and len(client.requests) == 2
    assert store.newest_queued_cv_request() == 0


def test_cv_fast_path_skips_when_pipeline_busy(store, tmp_path):
    from cvboost.locks import pipeline_lock
    from cvboost.worker import run_cv_now

    queue_request(store, 1)
    client = FakeClient(ok_draft())
    deps, _ = cv_deps(tmp_path, client)
    with pipeline_lock(tmp_path):
        st = run_cv_now(store, deps)
    assert st.cv_done == 0 and client.requests == []
    assert store.conn.execute("SELECT status FROM cv_requests").fetchone()[0] == "queued"


def test_wait_polls_new_requests_once_each(store, tmp_path, monkeypatch):
    import cvboost.worker as w

    deps, _ = cv_deps(tmp_path, FakeClient())
    calls = []
    monkeypatch.setattr(w, "run_cv_now", lambda s, d: calls.append(s.newest_queued_cv_request()))
    monkeypatch.setattr(w, "CV_POLL_S", 0)
    _, (second, _, _) = queue_request(store, 2)
    # the second request appears only during the wait (owner presses the button)
    store.conn.execute("UPDATE cv_requests SET status='done' WHERE id=?", (second,))
    ticks = iter(range(10))

    def fake_wait(_t):
        n = next(ticks)
        if n == 1:
            store.conn.execute("UPDATE cv_requests SET status='queued' WHERE id=?", (second,))
        if n >= 4:
            deps.clock.advance(hours=1)
        return False

    monkeypatch.setattr(deps.stop, "wait", fake_wait)
    w._wait_with_cv_poll(store, deps, 600)
    assert calls == [second]
