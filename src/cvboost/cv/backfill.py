"""Fill `cv_requests.recommendation` / `fit_summary` for kits written before migration 0007.

The source is the kit's own `fit_notes.md` (written by `step.fit_notes_md`). Nothing is invented:
a file that does not parse leaves the request without a verdict.
"""

from __future__ import annotations

import re
from pathlib import Path

from cvboost.storage.db import Store

# fit_notes.md exists in two generations: the original Russian headings and (since #70) English
# ones. A kit may also mix them (English headings written by the new code around an older Russian
# body), so both prefixes and both vocabularies are accepted here.
_REC_FROM_TEXT = {
    "apply": "apply",
    "apply with caveats": "apply_with_caveats",
    "skip": "skip",
    "откликаться": "apply",
    "с оговорками": "apply_with_caveats",
    "откликаться с оговорками": "apply_with_caveats",
    "пропустить": "skip",
}
_REC_LINE = re.compile(r"^(?:Recommendation|Рекомендация):\s*\*\*(.+?)\*\*\s*$", re.M)
FIT_NOTES_MAX = 100_000


def parse_fit_notes(text: str) -> tuple[str, str] | None:
    """(recommendation, summary) from a fit_notes.md, or None if the shape is not ours."""
    m = _REC_LINE.search(text)
    if not m:
        return None
    rec = _REC_FROM_TEXT.get(m.group(1).strip().lower())
    if rec is None:
        return None
    rest = text[m.end() :]
    summary = re.split(r"^## ", rest, maxsplit=1, flags=re.M)[0].strip()
    if not summary:
        return None
    return rec, summary[:600]


def backfill_notes(store: Store, kits_dir: Path) -> tuple[int, int]:
    """(filled, skipped) over finished requests that have no verdict yet."""
    root = kits_dir.resolve()
    rows = store.conn.execute(
        "SELECT id, canonical_id, kit_dir FROM cv_requests "
        "WHERE kind='cv' AND recommendation IS NULL "
        "AND status IN ('done', 'needs_review') ORDER BY id"
    ).fetchall()
    filled = skipped = 0
    for r in rows:
        folder = (
            str(r["kit_dir"]) if r["kit_dir"] and re.fullmatch(r"\d+", str(r["kit_dir"])) else None
        )
        path = (root / (folder or str(int(r["canonical_id"]))) / "fit_notes.md").resolve()
        parsed = None
        if path.is_relative_to(root) and path.is_file():
            with path.open("rb") as fh:
                parsed = parse_fit_notes(fh.read(FIT_NOTES_MAX).decode("utf-8", errors="replace"))
        if parsed is not None and store.backfill_cv_verdict(r["id"], *parsed):
            filled += 1
        else:
            skipped += 1
    return filled, skipped
