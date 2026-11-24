from __future__ import annotations

import logging
import threading
from datetime import timedelta

from cvboost import cli
from cvboost.config import Settings
from cvboost.locks import try_lock
from cvboost.notify.wiring import attach_telegram
from cvboost.storage.db import Store, connect
from cvboost.worker import run_worker
from tests.conftest import NOW
from tests.notify.helpers import OWNER, TOKEN
from tests.worker.helpers import FakeConnector, ScoreRecorder, make_deps
from tests.worker.test_worker_loop import settings_for


def test_no_token_warns_once_and_still_collects_alerts(tmp_path, caplog):
    deps = make_deps(tmp_path, [FakeConnector()])
    s = Settings(
        db_path=tmp_path / "x", log_level="INFO", rules_path=tmp_path, llm_config_path=tmp_path
    )
    with caplog.at_level(logging.WARNING):
        assert attach_telegram(deps, s) is False
    assert "not set" in caplog.text and TOKEN not in caplog.text
    assert deps.notify is not None and deps.start_bot is None


def test_with_token_wires_notify_bot_and_start_alert(tmp_path):
    deps = make_deps(tmp_path, [FakeConnector()])
    s = Settings(
        db_path=tmp_path / "x",
        log_level="INFO",
        rules_path=tmp_path,
        llm_config_path=tmp_path,
        telegram_token=TOKEN,
        telegram_chat_id=OWNER,
    )
    assert attach_telegram(deps, s) is True
    assert deps.notify and deps.start_bot and deps.on_start
    for close in deps.closers:
        close()


def test_worker_starts_and_joins_the_bot_thread(tmp_path):
    s = settings_for(tmp_path)
    started = []

    def bot(settings, cfg, stop):
        def loop():
            stop.wait(5)
            started.append("stopped")

        t = threading.Thread(target=loop)
        t.start()
        started.append("started")
        return t

    stop = threading.Event()
    rec = ScoreRecorder()
    rec.on_call = stop.set

    def factory(se, st):
        d = make_deps(se.data_dir, [FakeConnector()], score_fn=rec, stop=st)
        d.start_bot = bot
        return d

    assert run_worker(s, deps_factory=factory, stop=stop) == 0
    assert started == ["started", "stopped"]  # joined before exit
    lock = try_lock(s.data_dir / "worker.lock")
    assert lock is not None
    lock.release()


def test_once_mode_starts_no_bot(tmp_path):
    s = settings_for(tmp_path)
    called = []

    def factory(se, st):
        d = make_deps(se.data_dir, [FakeConnector()], score_fn=ScoreRecorder())
        d.start_bot = lambda *a: called.append(1)
        return d

    run_worker(s, once=True, deps_factory=factory)
    assert called == []


def test_cli_alerts_command(tmp_path, monkeypatch, capsys):
    from tests.rules_helpers import add, sync

    monkeypatch.setenv("CVBOOST_DB", str(tmp_path / "cli.sqlite"))
    st = Store(connect(tmp_path / "cli.sqlite"))
    from cvboost.storage.db import migrate

    migrate(st.conn)
    sync(st, "fake")
    jid = add(st, "fake", "1", company="Acme", title="Data Engineer")
    aid = st.insert_alert(
        canonical_id=jid, job_id=jid, llm_verdict_id=None, fit=77, status="pending", now=NOW
    )
    st.record_decision(
        jid,
        "rejected",
        channel="telegram",
        reason="rate",
        alert_id=aid,
        now=NOW + timedelta(minutes=1),
    )
    assert cli.main(["alerts"]) == 0
    out = capsys.readouterr().out
    assert (
        "Acme - Data Engineer" in out and "pending" in out and "rejected" in out and "rate" in out
    )
