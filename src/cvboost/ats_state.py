"""Per-company state of the ATS connectors (table ats_companies, migration 0008).

Scheduling lives in `next_due_at`:
- ok: in 6 h, with +-10% jitter so the list does not move in lockstep;
- failed (not 404/410): backoff 30 min, 1 h, then 6 h (a down host is not hammered);
- gone (404/410): first strike -> check again in 6 h; the second strike, a check 6 h or more
  after the first, makes the company inactive. An inactive company is re-checked after 30 days:
  a 200 brings it back, a 404 keeps it inactive.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Literal

from cvboost.companies import Company
from cvboost.storage.db import Store, iso

Outcome = Literal["ok", "gone", "failed"]

OK_INTERVAL = timedelta(hours=6)
JITTER = 0.10
GONE_RECHECK = timedelta(hours=6)  # between the two 404s that make a company inactive
STRIKES_TO_INACTIVE = 2
INACTIVE_RECHECK = timedelta(days=30)
FAIL_BACKOFF = (timedelta(minutes=30), timedelta(hours=1))  # then OK_INTERVAL


def fail_delay(failures: int) -> timedelta:
    """Delay after the `failures`-th consecutive failure (1-based)."""
    return FAIL_BACKOFF[failures - 1] if failures <= len(FAIL_BACKOFF) else OK_INTERVAL


def sync_companies(store: Store, companies: list[Company], now: datetime) -> None:
    """Register companies from the yaml list. Existing rows keep their state (inactive stays).

    Call inside a transaction: it is one INSERT per company.
    """
    for c in companies:
        store.conn.execute(
            "INSERT OR IGNORE INTO ats_companies (ats, slug, name, source, added_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (c.ats, c.slug, c.name, c.source, iso(now)),
        )


def due_companies(
    store: Store, ats: str, slugs: set[str], *, now: datetime, limit: int
) -> list[tuple[str, str]]:
    """(slug, name) of up to `limit` companies of `ats` that are due, never-polled first.

    Only slugs still in the yaml list are returned (removing a line stops polling). Inactive
    companies come back only when their 30-day re-check is due.
    """
    rows = store.conn.execute(
        "SELECT slug, name FROM ats_companies WHERE ats = ? "
        "AND (next_due_at IS NULL OR next_due_at <= ?) "
        "ORDER BY next_due_at IS NOT NULL, next_due_at, slug",
        (ats, iso(now)),
    ).fetchall()
    return [(r["slug"], r["name"]) for r in rows if r["slug"] in slugs][:limit]


def record_check(
    store: Store,
    ats: str,
    slug: str,
    outcome: Outcome,
    *,
    now: datetime,
    status: int | None = None,
    found: int | None = None,
) -> None:
    where = (ats, slug)
    if outcome == "ok":
        due = now + OK_INTERVAL * random.uniform(1 - JITTER, 1 + JITTER)
        store.conn.execute(
            "UPDATE ats_companies SET last_checked_at = ?, last_ok_at = ?, last_status = ?, "
            "last_found = ?, consecutive_failures = 0, gone_strikes = 0, active = 1, "
            "inactive_since = NULL, inactive_reason = NULL, next_due_at = ? "
            "WHERE ats = ? AND slug = ?",
            (iso(now), iso(now), status or 200, found, iso(due), *where),
        )
        return
    row = store.conn.execute(
        "SELECT active, gone_strikes, consecutive_failures FROM ats_companies "
        "WHERE ats = ? AND slug = ?",
        where,
    ).fetchone()
    if row is None:
        return
    if outcome == "gone":
        strikes = row["gone_strikes"] + 1
        if not row["active"]:  # a re-check of an inactive company: stays inactive
            store.conn.execute(
                "UPDATE ats_companies SET last_checked_at = ?, last_status = ?, next_due_at = ? "
                "WHERE ats = ? AND slug = ?",
                (iso(now), status, iso(now + INACTIVE_RECHECK), *where),
            )
        elif strikes >= STRIKES_TO_INACTIVE:
            store.conn.execute(
                "UPDATE ats_companies SET last_checked_at = ?, last_status = ?, active = 0, "
                "gone_strikes = ?, inactive_since = ?, inactive_reason = ?, next_due_at = ? "
                "WHERE ats = ? AND slug = ?",
                (
                    iso(now), status, strikes, iso(now), f"HTTP {status} x{strikes}",
                    iso(now + INACTIVE_RECHECK), *where,
                ),
            )  # fmt: skip
        else:
            store.conn.execute(
                "UPDATE ats_companies SET last_checked_at = ?, last_status = ?, gone_strikes = ?, "
                "next_due_at = ? WHERE ats = ? AND slug = ?",
                (iso(now), status, strikes, iso(now + GONE_RECHECK), *where),
            )
        return
    failures = row["consecutive_failures"] + 1
    delay = fail_delay(failures) if row["active"] else INACTIVE_RECHECK  # inactive: stay on 30 d
    store.conn.execute(
        "UPDATE ats_companies SET last_checked_at = ?, last_status = ?, consecutive_failures = ?, "
        "next_due_at = ? WHERE ats = ? AND slug = ?",
        (iso(now), status, failures, iso(now + delay), *where),
    )


def reactivate(store: Store, ats: str, slug: str) -> bool:
    """Owner command: poll this company again at once. False if the company is unknown."""
    cur = store.conn.execute(
        "UPDATE ats_companies SET active = 1, gone_strikes = 0, consecutive_failures = 0, "
        "inactive_since = NULL, inactive_reason = NULL, next_due_at = NULL "
        "WHERE ats = ? AND slug = ?",
        (ats, slug),
    )
    return cur.rowcount > 0


def counts(store: Store) -> dict[str, dict[str, int]]:
    """{ats: {active, inactive}}."""
    out: dict[str, dict[str, int]] = {}
    for r in store.conn.execute(
        "SELECT ats, active, COUNT(*) AS n FROM ats_companies GROUP BY ats, active"
    ):
        row = out.setdefault(r["ats"], {"active": 0, "inactive": 0})
        row["active" if r["active"] else "inactive"] = r["n"]
    return out
