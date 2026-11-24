"""Pure scheduling helpers: when is a source next due."""

from __future__ import annotations

from datetime import datetime, timedelta

MAX_BACKOFF = timedelta(hours=24)


def next_due_at(
    interval: timedelta,
    last_started_at: datetime | None,
    consecutive_failures: int,
    now: datetime,
) -> datetime:
    if last_started_at is None:
        return now
    if consecutive_failures <= 0:
        return last_started_at + interval
    delay = min(interval * 2 ** min(consecutive_failures, 20), MAX_BACKOFF)
    return last_started_at + delay


def is_due(
    interval: timedelta,
    last_started_at: datetime | None,
    consecutive_failures: int,
    now: datetime,
) -> bool:
    return now >= next_due_at(interval, last_started_at, consecutive_failures, now)
