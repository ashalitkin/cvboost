from __future__ import annotations

import threading

import pytest

from cvboost.config import Settings
from cvboost.notify import bot as B
from cvboost.storage.db import Store, connect
from cvboost.worker_config import WorkerConfig
from tests.conftest import NOW
from tests.notify.helpers import OWNER, TOKEN, callback, err, ok


@pytest.fixture
def settings(s, tmp_path):
    return Settings(
        db_path=tmp_path / "n.sqlite",
        log_level="INFO",
        rules_path=tmp_path,
        llm_config_path=tmp_path,
        telegram_token=TOKEN,
        telegram_chat_id=OWNER,
    )


class Stopper(threading.Event):
    """wait() never sleeps and records the requested pauses. The loop ends when the fake
    Telegram has answered `polls` getUpdates calls (see FakeTelegram.stop_after)."""

    def __init__(self, polls: int = 1):
        super().__init__()
        self.polls, self.waits = polls, []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.is_set()


def run(settings, tg, stop, handle=None, monkeypatch=None):
    tg.stop_after = (stop, tg.polls + stop.polls)
    if handle is not None:
        monkeypatch.setattr(B, "handle_update", handle)
    B.run_bot(stop, settings, WorkerConfig(), tg.client(), clock=lambda: NOW)


def offset(settings):
    return Store(connect(settings.db_path)).state_get("tg_offset")


def test_offset_is_saved_per_update_and_used_next_time(settings, tg, sent, monkeypatch):
    seen = []
    tg.updates = [
        callback("x:1", message_id=1, update_id=10),
        callback("x:1", message_id=1, update_id=11),
    ]
    stop = Stopper(1)

    def handle(store, t, owner, update, now):
        seen.append(update["update_id"])

    run(settings, tg, stop, handle, monkeypatch)
    assert seen == [10, 11] and offset(settings) == "12"
    tg.calls.clear()
    stop2 = Stopper(1)
    run(settings, tg, stop2)
    assert tg.of("getUpdates")[0]["offset"] == 12
    assert tg.of("getUpdates")[0]["allowed_updates"] == ["callback_query"]


def test_handler_failure_keeps_offset_then_skips_a_poison_update(settings, tg, sent, monkeypatch):
    attempts = []
    tg.queue("getUpdates", *[ok([callback("x:1", message_id=1, update_id=5)])] * 3)

    def handle(*a):
        attempts.append(1)
        raise RuntimeError("boom")

    stop = Stopper(3)
    run(settings, tg, stop, handle, monkeypatch)
    assert len(attempts) == 3  # POISON_AFTER, then the update is skipped
    assert offset(settings) == "6"
    assert tg.of("getUpdates")[0].get("offset") is None
    assert tg.of("getUpdates")[1].get("offset") is None  # not advanced while retrying


def test_database_locked_keeps_offset_and_retries(settings, tg, sent, monkeypatch):
    import sqlite3

    calls = []
    tg.queue(
        "getUpdates",
        ok([callback("x:1", message_id=1, update_id=5)]),
        ok([callback("x:1", message_id=1, update_id=5)]),
    )

    def handle(*a):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")

    stop = Stopper(2)
    run(settings, tg, stop, handle, monkeypatch)
    assert len(calls) == 2 and B.LOCKED_PAUSE_S in stop.waits and offset(settings) == "6"


def test_409_and_401_and_errors_back_off(settings, tg):
    tg.queue(
        "getUpdates",
        err(409, "Conflict: terminated by other getUpdates request"),
        err(401, "Unauthorized"),
        err(502, "Bad Gateway"),
        err(502, "Bad Gateway"),
    )
    stop = Stopper(4)
    run(settings, tg, stop)
    assert stop.waits == [B.CONFLICT_PAUSE_S, B.REVOKED_PAUSE_S, 5.0, 10.0]


def test_transport_errors_do_not_kill_the_thread_or_log_the_token(settings, tg, caplog):
    import httpx

    tg.queue("getUpdates", httpx.ConnectError("nope " + TOKEN), httpx.ReadTimeout("slow"))
    stop = Stopper(2)
    run(settings, tg, stop)
    assert stop.waits == [5.0, 10.0]
    assert TOKEN not in caplog.text


def test_end_to_end_button_press(settings, s, tg, sent):
    tg.updates = [callback(f"i:{sent['id']}", message_id=sent["message_id"], update_id=77)]
    stop = Stopper(1)
    run(settings, tg, stop)
    d = s.conn.execute("SELECT action, tg_update_id FROM decisions").fetchone()
    assert (d["action"], d["tg_update_id"]) == ("interested", 77)
