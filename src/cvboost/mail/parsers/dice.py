"""Dice job-alert mails (dice@connect.dice.com). Format from a real sample (2026-11-10), text/plain:

<title>\n<https://www.dice.com/job-detail/<uuid>>\n<company>\n<location>\nPosted: MM-DD-YYYY
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from cvboost.mail.canonical import canonicalise
from cvboost.mail.message import ParsedEmail
from cvboost.mail.parsers.base import ParsedJob, looks_remote, url_of_line

_POSTED = re.compile(r"^Posted:\s*(\d{2})-(\d{2})-(\d{4})\b")
MAX_FIELD = 200


def _is_link_line(line: str) -> bool:
    return url_of_line(line) is not None or line.startswith("<http")


def parse(p: ParsedEmail) -> list[ParsedJob]:
    lines = [ln.strip() for ln in p.text.replace("\r\n", "\n").splitlines()]
    out: dict[str, ParsedJob] = {}
    for i, line in enumerate(lines):
        href = url_of_line(line)
        link = canonicalise(href) if href else None
        if link is None or link.platform != "dice" or link.job_id in out:
            continue
        title = next((ln for ln in reversed(lines[:i]) if ln and not _is_link_line(ln)), "")
        after: list[str] = []
        posted = None
        for ln in lines[i + 1 : i + 14]:
            if not ln:
                continue
            if m := _POSTED.match(ln):
                try:
                    posted = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)), tzinfo=UTC)
                except ValueError:
                    posted = None
                break
            if _is_link_line(ln):
                break
            after.append(ln)
        if not title or not after:
            continue
        company, location = after[0], (after[1] if len(after) > 1 else None)
        out[link.job_id] = ParsedJob(
            "dice",
            link.job_id,
            link.url,
            title[:MAX_FIELD],
            company[:MAX_FIELD],
            location[:MAX_FIELD] if location else None,
            posted_at=posted,
            remote=looks_remote(location, title),
        )
    return list(out.values())
