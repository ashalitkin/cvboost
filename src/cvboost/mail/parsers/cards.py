"""Generic card parser: an anchor with a canonical job link = a posting, anchor text = title.

Used for freelancermap project alerts. SYNTHETIC: the real Projekt-Agent mails of the owner
have not arrived yet, only account mails did. When the first real one comes, replace this with
a platform parser built from it (see docs/sources.md); until then an alert that does not match
falls through to the LLM fallback.
"""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from cvboost.mail.canonical import canonicalise
from cvboost.mail.message import ParsedEmail
from cvboost.mail.parsers.base import ParsedJob, looks_remote, url_of_line
from cvboost.models import UNDISCLOSED_COMPANY

MAX_FIELD = 200


def parse_cards(p: ParsedEmail, platform: str) -> list[ParsedJob]:
    out: dict[str, ParsedJob] = {}
    if p.html:
        tree = HTMLParser(p.html)
        for a in tree.css("a[href]"):
            link = canonicalise((a.attributes.get("href") or "").strip())
            title = re.sub(r"\s+", " ", a.text(strip=True))
            if link is None or link.platform != platform or len(title) < 4 or link.job_id in out:
                continue
            card = a.parent.text(separator=" ", strip=True) if a.parent is not None else ""
            desc = re.sub(r"\s+", " ", card).replace(title, "", 1).strip()[:600]
            out[link.job_id] = ParsedJob(
                platform,
                link.job_id,
                link.url,
                title[:MAX_FIELD],
                UNDISCLOSED_COMPANY,
                description=desc,
                remote=looks_remote(desc, title),
            )
    if not out:
        lines = [ln.strip() for ln in p.text.replace("\r\n", "\n").splitlines()]
        for i, ln in enumerate(lines):
            href = url_of_line(ln)
            link = canonicalise(href) if href else None
            if link is None or link.platform != platform or link.job_id in out:
                continue
            title = next((x for x in reversed(lines[:i]) if x and not url_of_line(x)), "")
            if len(title) >= 4:
                out[link.job_id] = ParsedJob(
                    platform, link.job_id, link.url, title[:MAX_FIELD], UNDISCLOSED_COMPANY
                )
    return list(out.values())
