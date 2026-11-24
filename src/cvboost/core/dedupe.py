"""Cross-source dedupe: assign one canonical_id to the same posting seen on several boards."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from cvboost.core.normalize import company_norm, has_key, title_norm


@dataclass(frozen=True)
class DedupeStats:
    groups: int
    merged: int
    changed: int


def assign_canonical(conn: sqlite3.Connection, window_days: int) -> DedupeStats:
    """Recompute norms and canonical_id for every job. Idempotent; runs in one transaction."""
    rows = conn.execute(
        "SELECT id, source, company, title, location_text, content_hash, "
        "COALESCE(posted_at, first_seen_at) AS d, company_norm, title_norm, canonical_id "
        "FROM jobs ORDER BY id"
    ).fetchall()
    changed = 0
    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        anchors: dict[tuple[str, str], list[tuple[int, str, str | None, datetime]]] = {}
        by_hash: dict[str, int] = {}
        canon: dict[int, int] = {}
        for r in rows:
            cn, tn = company_norm(r["company"]), title_norm(r["title"])
            if (r["company_norm"], r["title_norm"]) != (cn, tn):
                conn.execute(
                    "UPDATE jobs SET company_norm = ?, title_norm = ? WHERE id = ?",
                    (cn, tn, r["id"]),
                )
            d = datetime.fromisoformat(r["d"])
            target: int | None = None
            if has_key(cn) and has_key(tn):
                for aid, asrc, aloc, ad in anchors.get((cn, tn), []):
                    if abs(d - ad).days > window_days:
                        continue
                    if asrc != r["source"] or aloc == r["location_text"]:
                        target = aid
                        break
            if target is None:
                target = by_hash.get(r["content_hash"])
            if target is None:
                target = r["id"]
                if has_key(cn) and has_key(tn):
                    anchors.setdefault((cn, tn), []).append(
                        (r["id"], r["source"], r["location_text"], d)
                    )
            by_hash.setdefault(r["content_hash"], target)
            canon[r["id"]] = target
            if r["canonical_id"] != target:
                conn.execute("UPDATE jobs SET canonical_id = ? WHERE id = ?", (target, r["id"]))
                changed += 1
        if own_tx:
            conn.execute("COMMIT")
    except BaseException:
        if own_tx:
            conn.execute("ROLLBACK")
        raise
    groups = len(set(canon.values()))
    return DedupeStats(groups=groups, merged=len(canon) - groups, changed=changed)
