"""Shared LLM budget logic: the local day, daily caps, usage-limit / auth pauses.

Used by `score` and by the CV step: the subscription is one, so caps and pauses are common.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cvboost.llm import client as C
from cvboost.llm.config import LLMConfig
from cvboost.storage.db import Store

PAUSE_STATUSES = {C.USAGE_LIMIT, C.AUTH_ERROR, C.REFUSED_AUTH_METHOD}


def local_day(now: datetime, tz: str) -> str:
    return now.astimezone(ZoneInfo(tz)).date().isoformat()


def fmt_local(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


def daily_gate(store: Store, cfg: LLMConfig, alias: str, day: str) -> tuple[str, str] | None:
    """(reason code, text) of the first closed daily gate (call cap, then cost cap)."""
    cap = cfg.daily_call_caps.get(alias, 0)
    used = store.calls_today(alias, day)
    if used >= cap:
        return "daily_cap", f"daily cap {alias} {used}/{cap} reached"
    cost = store.cost_today(day)
    if cost >= cfg.daily_cost_cap_usd:
        return (
            "daily_cost_cap",
            f"daily cost cap ${cfg.daily_cost_cap_usd:.2f} reached (${cost:.4f} used)",
        )
    return None


def pause_for(store: Store, cfg: LLMConfig, status: str, at: datetime) -> datetime | None:
    if status == C.USAGE_LIMIT:
        k = store.consecutive_limit_hits() + 1
        lp = cfg.limit_pause
        return at + timedelta(minutes=min(lp.first_minutes * 2 ** (k - 1), lp.max_minutes))
    if status in PAUSE_STATUSES:
        return at + timedelta(minutes=cfg.limit_pause.auth_minutes)
    return None
