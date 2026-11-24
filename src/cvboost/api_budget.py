"""Call budget of the keyed APIs (#19): counts per UTC day and month, alerts, pause.

Never makes a paid call: when a limit is reached the source just stops until the next
day / month (or until the owner raises the limit in `config/api_budget.yaml`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cvboost.storage.db import Store

DEFAULT_PATH = Path("config/api_budget.yaml")
ENV_PATH = "CVBOOST_API_BUDGET_PATH"
ALERT_KIND = "api_budget"


class Limit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    per_day: int = Field(gt=0)
    per_month: int = Field(gt=0)


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    warn_ratio: float = Field(default=0.8, gt=0, le=1)
    adzuna: Limit | None = None
    reed: Limit | None = None
    itjobs: Limit | None = None


def load_limit(api: str, default: Limit) -> tuple[Limit, float, str | None]:
    """(limit, warn_ratio, problem). A missing file = the defaults. A file that does not parse, or
    a section that does not validate, gives a `problem` text (the caller logs and alerts) and the
    limits become min(what parsed, defaults) per field: a broken file never lifts a limit."""
    path = Path(os.environ.get(ENV_PATH) or DEFAULT_PATH)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default, 0.8, None
    except OSError as exc:
        return default, 0.8, f"cannot read {path.name} ({type(exc).__name__})"
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return default, 0.8, f"{path.name} is not valid YAML"
    if not isinstance(data, dict):
        return default, 0.8, f"{path.name} is not a mapping"
    problems: list[str] = []
    warn = 0.8
    if "warn_ratio" in data:
        try:
            warn = BudgetConfig.model_validate({"warn_ratio": data["warn_ratio"]}).warn_ratio
        except ValidationError:
            problems.append("warn_ratio")
    limit = default
    if data.get(api) is not None:
        try:
            limit = Limit.model_validate(data[api])
        except ValidationError:
            problems.append(api)
    unknown = sorted(set(data) - set(BudgetConfig.model_fields))
    problems += [f"unknown key {k}" for k in unknown]
    if problems:  # a damaged file never lifts a limit
        limit = Limit(
            per_day=min(limit.per_day, default.per_day),
            per_month=min(limit.per_month, default.per_month),
        )
    problem = (
        f"invalid in {path.name}: " + ", ".join(str(x) for x in problems) if problems else None
    )
    return limit, warn, problem


@dataclass(frozen=True)
class BudgetStatus:
    ok: bool
    reason: str  # "" | "day" | "month"
    day_used: int
    month_used: int


def status(store: Store, api: str, limit: Limit, now: datetime) -> BudgetStatus:
    d, m = store.api_calls_day(api, now), store.api_calls_month(api, now)
    reason = "month" if m >= limit.per_month else "day" if d >= limit.per_day else ""
    return BudgetStatus(not reason, reason, d, m)


def _alert(store: Store, api: str, label: str, key: str, text: str, now: datetime) -> None:
    store.insert_system_alert(ALERT_KIND, f"{api}:{key}", text, now)


def _hint(api: str) -> str:
    if api == "adzuna":
        return "decide about payment (#34)"
    return "decide whether a paid plan is needed"


def reserve(
    store: Store, api: str, label: str, limit: Limit, warn_ratio: float, now: datetime
) -> bool:
    """Count one call if the budget allows it. False = the limit is reached: do not call.

    Raises the system alerts (deduplicated per period): near (warn_ratio of the month or day
    limit) and exhausted.
    """
    st = status(store, api, limit, now)
    month = now.astimezone(UTC).strftime("%Y-%m")
    day = now.astimezone(UTC).date().isoformat()
    if not st.ok:
        if st.reason == "month":
            _alert(
                store,
                api,
                label,
                f"month:{month}:exhausted",
                f"{label} limit reached: {st.month_used}/{limit.per_month} calls in {month}. "
                f"The source is paused until next month — {_hint(api)}.",
                now,
            )
        else:
            _alert(
                store,
                api,
                label,
                f"day:{day}:exhausted",
                f"Daily {label} limit reached: {st.day_used}/{limit.per_day}. "
                "The source is paused until tomorrow.",
                now,
            )
        return False
    store.api_calls_add(api, now)
    d, m = st.day_used + 1, st.month_used + 1
    if m >= limit.per_month * warn_ratio:
        _alert(
            store,
            api,
            label,
            f"month:{month}:near",
            f"{label} limit is close: {m}/{limit.per_month} calls in {month} — {_hint(api)}.",
            now,
        )
    if d >= limit.per_day:
        # only the day cap itself is worth a message: normal runs stay below it
        _alert(
            store,
            api,
            label,
            f"day:{day}:near",
            f"Daily {label} limit hit: {d}/{limit.per_day}.",
            now,
        )
    return True
