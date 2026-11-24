"""Bot thread: long polling `getUpdates`, one update at a time into `callbacks.handle_update`.

Runs next to the worker loop (which may be busy with a long collect/LLM step), with its own SQLite
connection and its own HttpClient. The offset is saved after each handled update, so a crash
replays at most one update; `decisions.tg_update_id` makes the replay harmless.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from cvboost.config import Settings
from cvboost.notify.callbacks import handle_update
from cvboost.notify.redact import redact
from cvboost.notify.telegram import TelegramClient, TelegramError
from cvboost.storage.db import Store, connect
from cvboost.worker_config import WorkerConfig

log = logging.getLogger("cvboost.bot")

LOCKED_PAUSE_S = 2
CONFLICT_PAUSE_S = 60  # 409: a webhook is set or another poller runs on the same bot
REVOKED_PAUSE_S = 30 * 60  # 401: the token is wrong or revoked
MAX_BACKOFF_S = 300
POISON_AFTER = 3  # an update that fails this many times in a row is skipped


def run_bot(
    stop: threading.Event,
    settings: Settings,
    cfg: WorkerConfig,
    tg: TelegramClient,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    owner = settings.telegram_chat_id
    assert owner is not None
    token = settings.telegram_token
    conn = connect(settings.db_path, busy_ms=15000)
    store = Store(conn)
    offset = int(store.state_get("tg_offset") or 0) or None
    backoff = 5.0
    failures: dict[int, int] = {}
    try:
        while not stop.is_set():
            try:
                updates = tg.get_updates(offset, timeout_s=cfg.telegram.poll_timeout_s)
            except TelegramError as exc:
                log.warning("getUpdates failed: %s", redact(str(exc), token))
                if exc.status == 409:
                    stop.wait(CONFLICT_PAUSE_S)
                elif exc.status == 401:
                    log.error("Telegram rejected the bot token (401); retrying in 30 min")
                    stop.wait(REVOKED_PAUSE_S)
                else:
                    stop.wait(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_S)
                continue
            except Exception as exc:
                log.warning("getUpdates crashed: %s", redact(f"{type(exc).__name__}", token))
                stop.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)
                continue
            backoff = 5.0
            for update in updates:
                uid = update.get("update_id")
                try:
                    handle_update(store, tg, owner, update, clock())
                except Exception as exc:
                    locked = isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc)
                    n = failures.get(uid, 0) + 1 if isinstance(uid, int) else POISON_AFTER
                    log.warning(
                        "update %s failed (%d): %s",
                        uid,
                        n,
                        "database locked" if locked else redact(type(exc).__name__, token),
                    )
                    if locked or n < POISON_AFTER:
                        if not locked:
                            failures[uid] = n
                        stop.wait(LOCKED_PAUSE_S if locked else backoff)
                        break  # keep the offset: the update is retried
                    log.error("update %s skipped after %d failures", uid, n)
                failures.pop(uid, None)
                if isinstance(uid, int):
                    offset = uid + 1
                    try:
                        store.state_set("tg_offset", str(offset), clock())
                    except sqlite3.Error:  # kept in memory; a replay after a crash is harmless
                        log.warning("could not save the update offset")
    finally:
        conn.close()
