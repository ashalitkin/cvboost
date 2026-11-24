"""Source health rows: one formatter for `cvboost sources` and the web panel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cvboost import connectors
from cvboost.schedule import next_due_at
from cvboost.storage.db import Store

MAX_INTERVAL_S = 7 * 24 * 3600


def fmt_dt(dt: datetime | None) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M") if dt else "-"


def fmt_interval(seconds: float) -> str:
    return f"{seconds / 3600:g}h" if seconds >= 3600 else f"{seconds / 60:g}m"


@dataclass(frozen=True)
class SourceRow:
    name: str
    kind: str
    enabled: bool
    interval: str
    interval_override_s: int | None
    min_interval_s: int  # the connector's own default: the ToS floor for an override
    last_run: str
    last_success: str
    fails: int
    next_due: str
    jobs: int
    last_error: str  # external text: never trusted
    attribution: str


def source_rows(
    store: Store, now: datetime, unconfigured: frozenset[str] = frozenset()
) -> list[SourceRow]:
    """`unconfigured`: connector names without credentials (shown as "not configured")."""
    rows = []
    health = store.source_health()
    jobs_by_name = {h.name: h.jobs_total for h in health}
    sub_sources = {n for c in connectors.REGISTRY.values() for n in c.job_sources}
    for h in health:
        if h.name in sub_sources:  # email:<platform> belong to the e-mail connector's row
            continue
        cls = connectors.REGISTRY.get(h.name)
        default = cls.interval if cls else timedelta(hours=1)
        interval = timedelta(seconds=h.interval_override_s) if h.interval_override_s else default
        due = "-"
        if h.enabled:
            due = fmt_dt(next_due_at(interval, h.last_started_at, h.consecutive_failures, now))
        last = "not configured" if h.name in unconfigured else "-"
        if h.name in unconfigured:
            due = "-"
        elif h.last_status:
            last = f"{fmt_dt(h.last_started_at)} {h.last_status} {h.last_found}/{h.last_new}"
        rows.append(
            SourceRow(
                name=h.name,
                kind=cls.kind if cls else "?",
                enabled=h.enabled,
                interval=fmt_interval(interval.total_seconds()),
                interval_override_s=h.interval_override_s,
                min_interval_s=int(default.total_seconds()),
                last_run=last,
                last_success=fmt_dt(h.last_success_at),
                fails=h.consecutive_failures,
                next_due=due,
                jobs=h.jobs_total
                + sum(jobs_by_name.get(n, 0) for n in (cls.job_sources if cls else ())),
                last_error=(h.last_error or "-").replace("\n", " "),
                attribution=cls.attribution if cls else "",
            )
        )
    return rows
