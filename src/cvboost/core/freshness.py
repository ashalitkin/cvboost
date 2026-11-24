"""Is a posting still alive? One pure function for the Telegram alerts (#5) and the panel (#6).

Not part of `rules.yaml`: rule verdicts are cached by input hash, but staleness depends on the
clock. Callers apply this at read time.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from cvboost.textutil import parse_rfc822


def _expiry(source: str, raw: Mapping[str, Any] | None) -> datetime | None:
    """The source's own expiry timestamp from the stored raw payload; None if absent/unparsable."""
    if not raw:
        return None
    if source == "himalayas":
        v = raw.get("expiryDate")
        if isinstance(v, bool) or not isinstance(v, int | float):
            return None
        try:
            return datetime.fromtimestamp(v, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if source == "wwr":
        v = raw.get("expires_at")
        return parse_rfc822(v) if isinstance(v, str) and v.strip() else None
    return None


def closed_reason(
    source: str,
    posted_at: datetime | None,
    first_seen_at: datetime,
    raw: Mapping[str, Any] | None,
    now: datetime,
    max_age_days: int,
) -> str | None:
    """'expired' (the source says so), 'stale' (older than max_age_days) or None (alive).

    `raw` is the stored raw payload (`jobs.raw_json`). A malformed expiry value is not evidence
    of closure.
    """
    expires = _expiry(source, raw)
    if expires is not None and expires < now:
        return "expired"
    born = posted_at or first_seen_at
    if born < now - timedelta(days=max_age_days):
        return "stale"
    return None
