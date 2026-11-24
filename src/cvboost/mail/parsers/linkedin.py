"""LinkedIn job-alert mails (jobalerts-noreply@linkedin.com). Format taken from real samples
(2026-11-10): text/plain with one block per job, blocks separated by a dashed line:

    <title>\n<company>\n<location>\n[This company is actively hiring]\nView job: <tracking link>

The job id is read from the link; the HTML fallback (anchors with /jobs/view/<id>) is a guess
for layouts we have not seen and is covered by a synthetic test only.
"""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from cvboost.mail.canonical import canonicalise
from cvboost.mail.message import ParsedEmail
from cvboost.mail.parsers.base import ParsedJob, looks_remote

_SPLIT = re.compile(r"\n[ \t]*-{10,}[ \t]*\n")
_VIEW = re.compile(r"View job:[ \t]*\n?[ \t]*(\S{1,2048})")
_NOISE = re.compile(
    r"^(This company is actively hiring|Apply with resume & profile|Easy Apply|Promoted|"
    r"Be an early applicant|Actively recruiting|\d+ (?:connections?|school alumni?).*|"
    r"\d+ applicants?|Fast growing|Reposted.*)$",
    re.IGNORECASE,
)
_HEADER = re.compile(
    r"job alert|you.ll receive|notifications when|new jobs? (?:for|match)|jobs? that match|"
    r"see all jobs|manage your",
    re.IGNORECASE,
)
_SALARY = re.compile(r"^[$€£]\s?\d[\d.,KkMm ]*(?:\s*[-–]\s*[$€£]?\s?\d[\d.,KkMm ]*)?(?:/\w+)?.*$")
MAX_FIELD = 200


def _card(before_link: str) -> tuple[str, str, str | None, str | None] | None:
    """The job card = the nearest paragraph above "View job:" that has a title and a company
    (its first two lines, then the location); noise and salary lines are labels, not fields."""
    paragraphs = [para for para in re.split(r"\n[ \t]*\n", before_link) if para.strip()]
    for para in reversed(paragraphs):
        raw = [ln.strip() for ln in para.splitlines() if ln.strip()]
        if any(_HEADER.search(ln) for ln in raw):  # the alert's intro, not a job card
            continue
        salary = next((ln for ln in raw if _SALARY.match(ln)), None)
        lines = [ln for ln in raw if not _NOISE.match(ln) and ln != salary]
        if len(lines) >= 2:
            return lines[0], lines[1], lines[2] if len(lines) > 2 else None, salary
    return None


def _parse_text(p: ParsedEmail) -> list[ParsedJob]:
    out: dict[str, ParsedJob] = {}
    for block in _SPLIT.split("\n" + p.text.replace("\r\n", "\n")):
        m = _VIEW.search(block)
        if not m:
            continue
        link = canonicalise(m.group(1))
        if link is None or link.platform != "linkedin" or link.job_id in out:
            continue
        card = _card(block[: m.start()])
        if card is None:
            continue
        title, company, location, salary = card
        out[link.job_id] = ParsedJob(
            "linkedin",
            link.job_id,
            link.url,
            title[:MAX_FIELD],
            company[:MAX_FIELD],
            location[:MAX_FIELD] if location else None,
            salary_text=salary[:MAX_FIELD] if salary else None,
            remote=looks_remote(location, title),
        )
    return list(out.values())


def _parse_html(p: ParsedEmail) -> list[ParsedJob]:
    if not p.html:
        return []
    out: dict[str, ParsedJob] = {}
    tree = HTMLParser(p.html)
    for a in tree.css("a[href]"):
        link = canonicalise((a.attributes.get("href") or "").strip())
        title = re.sub(r"\s+", " ", a.text(strip=True))
        if link is None or link.platform != "linkedin" or not title or link.job_id in out:
            continue
        node = a
        company = location = ""
        for _ in range(4):  # the card: the nearest ancestor with more text than the title
            node = node.parent
            if node is None:
                break
            lines = [ln.strip() for ln in node.text(separator="\n").splitlines() if ln.strip()]
            rest = [ln for ln in lines if ln != title and not _NOISE.match(ln)]
            if rest:
                first = rest[0]
                company, _, location = first.partition(" · ")
                if not location and len(rest) > 1:
                    location = rest[1]
                break
        if not company:
            continue
        out[link.job_id] = ParsedJob(
            "linkedin",
            link.job_id,
            link.url,
            title[:MAX_FIELD],
            company[:MAX_FIELD],
            location[:MAX_FIELD] or None,
            remote=looks_remote(location, title),
        )
    return list(out.values())


def parse(p: ParsedEmail) -> list[ParsedJob]:
    return _parse_text(p) or _parse_html(p)
