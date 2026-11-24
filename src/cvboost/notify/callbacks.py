"""Button presses from Telegram (callback_query updates) -> `decisions`.

Only the owner is served: the update must come from the owner's user id AND from a message in the
owner's chat. Everything else is dropped without a reply and without a trace in the DB.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from cvboost.notify import format as F
from cvboost.notify.redact import redact
from cvboost.notify.telegram import TelegramClient, TelegramError
from cvboost.storage.db import Store

log = logging.getLogger("cvboost.callbacks")


def _owner_callback(update: dict[str, Any], owner_id: int) -> dict[str, Any] | None:
    cq = update.get("callback_query")
    if not isinstance(cq, dict):
        return None  # plain messages and every other update type are ignored
    sender = cq.get("from")
    msg = cq.get("message")
    chat = msg.get("chat") if isinstance(msg, dict) else None
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return None
    if sender.get("id") != owner_id or chat.get("id") != owner_id:
        return None
    if not isinstance(cq.get("id"), str) or not isinstance(msg.get("message_id"), int):
        return None
    return cq


def _safely(what: str, fn, *args) -> None:
    """After the DB write, Telegram failures only get logged: the decision is already stored."""
    try:
        fn(*args)
    except TelegramError as exc:
        log.warning("%s failed: %s", what, redact(str(exc)))


def handle_update(
    store: Store, tg: TelegramClient, owner_id: int, update: dict[str, Any], now: datetime
) -> None:
    cq = _owner_callback(update, owner_id)
    if cq is None:
        log.debug("ignored update %s", update.get("update_id"))
        return
    cb_id: str = cq["id"]
    chat_id: int = cq["message"]["chat"]["id"]
    message_id: int = cq["message"]["message_id"]
    m = F.CALLBACK_RE.match(cq.get("data") or "")
    if m is None:
        _safely("answer", tg.answer_callback, cb_id, "Unknown button")
        return
    op, alert_id, arg = m.group(1), int(m.group(2)), m.group(3)
    if (op == "r") != (arg is not None):  # a reason goes with 'r' and only with 'r'
        _safely("answer", tg.answer_callback, cb_id, "Unknown button")
        return
    alert = store.alert(alert_id)
    if alert is None or alert["message_id"] != message_id:
        _safely("answer", tg.answer_callback, cb_id, "Job not found")
        return
    uid = update.get("update_id")
    uid = uid if isinstance(uid, int) else None
    job_id = alert["job_id"]
    common = dict(channel="telegram", alert_id=alert_id, tg_update_id=uid, now=now)

    state: str | None
    toast: str | None = None
    if op == "i":
        store.record_decision(job_id, "interested", **common)
        state, toast = "interested", "Marked as interested"
    elif op == "n":
        state = "reasons"
    elif op == "r":
        store.record_decision(job_id, "rejected", reason=arg, **common)
        state = f"rejected:{arg}"
    elif op == "cv":
        _, _, created = store.request_cv(job_id, **common)
        state = "cv_queued"
        toast = "CV request created" if created else "CV already queued"
    elif op == "u":
        store.record_decision(job_id, "undo", **common)
        state, toast = "initial", "Undone"
    elif op == "b":
        state = "initial"
    else:  # 'x': a status button, nothing to do
        state = None

    if state is not None:
        _safely("edit_markup", tg.edit_markup, chat_id, message_id, F.keyboard(alert_id, state))
    _safely("answer", tg.answer_callback, cb_id, toast)
