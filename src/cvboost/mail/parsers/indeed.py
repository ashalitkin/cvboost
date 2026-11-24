"""Indeed job-alert mails (donotreply@jobalert.indeed.com). Format from a real sample (pt.indeed.com
alert, 2026-11-10), text/plain, after a header line "10 ofertas disponíveis: Remoto":

    <title> / <company> - <location> / [salary] / [apply label] / <snippet> / <age> / <link>
    (one line each)

Every link is an opaque engage.indeed.com redirect without a job id, so the id and the URL are
derived from (title, company): the URL is an Indeed search for that pair, never the tracking link.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus

from cvboost.mail.message import ParsedEmail
from cvboost.mail.parsers.base import ParsedJob, looks_remote, stable_id, url_of_line

_HEADER = re.compile(r"^\d+\s+\S.{0,80}:\s*\S", re.UNICODE)
_SALARY = re.compile(r"\d.*[€$£]|[€$£].*\d")
_AGE = re.compile(
    r"^(há\s.+|agora mesmo|hoje|just posted|today|\d+\+?\s+days?\s+ago|\d+\s+days?\s+ago|"
    r"posted .+|vor\s.+|hace\s.+|il y a\s.+)$",
    re.IGNORECASE,
)
_LABELS = re.compile(
    r"^(candidate-se facilmente|easily apply|responde rapidamente|urgently hiring|"
    r"contratação urgente)$",
    re.IGNORECASE,
)
MAX_FIELD = 200


def parse(p: ParsedEmail) -> list[ParsedJob]:
    lines = [ln.strip() for ln in p.text.replace("\r\n", "\n").splitlines()]
    start = next((i + 1 for i, ln in enumerate(lines) if _HEADER.match(ln)), 0)
    block: list[str] = []
    out: dict[str, ParsedJob] = {}
    for ln in lines[start:]:
        if not ln:
            continue
        href = url_of_line(ln)
        if href is None:
            block.append(ln)
            continue
        job = _job(block)
        block = []
        if job is not None:
            out.setdefault(job.source_id, job)
    return list(out.values())


def _job(block: list[str]) -> ParsedJob | None:
    if len(block) < 3 or " - " not in block[1] or len(block[0]) > MAX_FIELD:
        return None
    title = block[0]
    company, _, location = block[1].rpartition(" - ")
    if not company:
        return None
    rest = [ln for ln in block[2:] if not _LABELS.match(ln)]
    salary = next((ln for ln in rest[:1] if _SALARY.search(ln) and len(ln) < 80), None)
    rest = [ln for ln in rest if ln != salary and not _AGE.match(ln)]
    desc = " ".join(rest)[:1500]
    key = stable_id(title, company)
    return ParsedJob(
        "indeed",
        key,
        f"https://pt.indeed.com/jobs?q={quote_plus(f'{title} {company}'[:200])}",
        title[:MAX_FIELD],
        company[:MAX_FIELD],
        location[:MAX_FIELD] or None,
        salary_text=salary,
        description=desc,
        remote=looks_remote(location, title),
    )
