"""Plug Telegram into the worker: the notify step, the bot thread, the start-up alert."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from cvboost.config import Settings
from cvboost.core.rules import rule_version
from cvboost.http import HttpClient
from cvboost.llm.prompts import score_v1 as P
from cvboost.notify.alerts import (
    queue_system_alerts,
    select_candidates,
    send_pending,
    worker_started_alert,
)
from cvboost.notify.bot import run_bot
from cvboost.notify.redact import redact
from cvboost.notify.telegram import TelegramClient
from cvboost.storage.db import Store

if TYPE_CHECKING:
    from cvboost.worker import CycleStats, WorkerDeps

log = logging.getLogger("cvboost.telegram")


def attach_telegram(deps: WorkerDeps, settings: Settings) -> bool:
    """Wire alerts and buttons into `deps`. False (with one warning) if token/chat id are unset."""
    token, chat_id = settings.telegram_token, settings.telegram_chat_id
    if not token or chat_id is None:
        log.warning(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set: alerts are collected but not sent"
        )
        deps.notify = _collect_only(deps)
        return False
    send_http = HttpClient()
    deps.closers.append(send_http.close)
    deps.notify = make_notify_step(deps, TelegramClient(token, send_http), chat_id, token)

    def start_bot(s: Settings, cfg, stop: threading.Event) -> threading.Thread:
        bot_http = HttpClient()  # own client: HttpClient is not thread-safe
        deps.closers.append(bot_http.close)
        bot_tg = TelegramClient(token, bot_http)
        try:
            log.info("telegram bot: @%s", bot_tg.get_me().get("username"))
        except Exception as exc:
            log.warning("getMe failed: %s", redact(str(exc), token))
        t = threading.Thread(
            target=run_bot, args=(stop, s, cfg, bot_tg), name="telegram-bot", daemon=False
        )
        t.start()
        return t

    deps.start_bot = start_bot
    deps.on_start = lambda store, now: worker_started_alert(store, deps.cfg, now)
    return True


def make_notify_step(
    deps: WorkerDeps, tg: TelegramClient, chat_id: int, token: str | None = None
) -> Callable[[Store, datetime, CycleStats], None]:
    def notify(store: Store, now: datetime, stats: CycleStats) -> None:
        kept = _select(store, deps, now)
        _system(store, deps, now, stats)
        r = send_pending(store, tg, deps.cfg, chat_id, now, token=token, kept_hashes=kept)
        stats.alerts_sent, stats.alerts_summarized = r.sent, r.summarized

    return notify


def _kept_hashes(store: Store, deps: WorkerDeps) -> dict[int, str]:
    """canonical_id -> the input hash `score` computes for the group today (same code path)."""
    rv = rule_version(deps.rules)
    out: dict[int, str] = {}
    for g in store.kept_canonicals(rv, deps.rules.relevance.threshold):
        job = store.job_for_llm(g.representative_id, rv)
        body = P.render_body(job, g, deps.llm_cfg.scoring.description)
        out[g.canonical_id] = P.item_hash(body)
    return out


def _select(store: Store, deps: WorkerDeps, now: datetime) -> dict[int, str] | None:
    """Record new alert candidates. Returns the current input hashes (None if that failed, which
    only turns off the staleness re-check: cards already pending still go out)."""
    try:
        kept = _kept_hashes(store, deps)
        with store.transaction():
            select_candidates(store, deps.cfg, now, kept_hashes=kept)
        return kept
    except Exception:
        log.exception("selecting alert candidates failed")
        return None


def _system(store: Store, deps: WorkerDeps, now: datetime, stats: CycleStats) -> None:
    """System alerts in their own transaction: a bad value there must not hold back the cards."""
    try:
        with store.transaction():
            queue_system_alerts(
                store, deps.cfg, now, score_code=stats.score_code, score_text=stats.score_stopped
            )
    except Exception:
        log.exception("queueing system alerts failed")


def _collect_only(deps: WorkerDeps):
    def notify(store: Store, now: datetime, stats: CycleStats) -> None:
        _select(store, deps, now)

    return notify
