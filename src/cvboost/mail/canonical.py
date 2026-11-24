"""Job links from e-mails: canonicalise, never open.

A link is accepted only if it carries a job id we can recognise by pattern. The canonical URL is
rebuilt from the platform's own template, so tracking parameters, per-recipient tokens and
look-alike hosts never reach the database. Everything else (unsubscribe, settings, trackers
without a visible id) is dropped. All regexes are linear: bounded quantifiers, no nested groups.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

MAX_HREF = 2048
_HOST = r"https?://(?:[a-z0-9-]{1,40}\.){0,3}"


@dataclass(frozen=True)
class JobLink:
    platform: str
    job_id: str
    url: str


_LINKEDIN = re.compile(
    _HOST + r"linkedin\.com/(?:comm/)?jobs/view/(?:[a-z0-9%_.-]{0,200}?-)?(\d{6,12})(?!\d)",
    re.IGNORECASE,
)
_LINKEDIN_CURRENT = re.compile(
    _HOST + r"linkedin\.com/[^\s?#]{0,100}\?[^\s#]{0,500}?currentJobId=(\d{6,12})(?!\d)",
    re.IGNORECASE,
)
_DICE = re.compile(
    _HOST + r"dice\.com/job-detail/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_INDEED = re.compile(
    _HOST + r"indeed\.com/[^\s?#]{0,100}\?[^\s#]{0,500}?(?<![a-z])jk=([0-9a-f]{16})(?![0-9a-f])",
    re.IGNORECASE,
)
_FREELANCERMAP = re.compile(
    _HOST + r"freelancermap\.(com|de)/(projekt|project)/([a-z0-9][a-z0-9-]{2,150})(?![a-z0-9-])",
    re.IGNORECASE,
)


def _unquote_twice(href: str) -> str:
    """Trackers put the target URL in a query parameter, sometimes percent-encoded twice."""
    return unquote(unquote(href[:MAX_HREF]))


def canonicalise(href: str) -> JobLink | None:
    """A job link with a recognisable id -> its canonical form, else None."""
    s = _unquote_twice(href)
    if m := (_LINKEDIN.search(s) or _LINKEDIN_CURRENT.search(s)):
        return JobLink("linkedin", m.group(1), f"https://www.linkedin.com/jobs/view/{m.group(1)}/")
    if m := _DICE.search(s):
        job_id = m.group(1).lower()
        return JobLink("dice", job_id, f"https://www.dice.com/job-detail/{job_id}")
    if m := _INDEED.search(s):
        job_id = m.group(1).lower()
        return JobLink("indeed", job_id, f"https://www.indeed.com/viewjob?jk={job_id}")
    if m := _FREELANCERMAP.search(s):
        tld, kind, slug = m.group(1).lower(), m.group(2).lower(), m.group(3).lower()
        return JobLink("freelancermap", slug, f"https://www.freelancermap.{tld}/{kind}/{slug}")
    return None


def canonical_links(links: list[str], platform: str | None = None) -> list[JobLink]:
    """Unique canonical job links of a message, in order of appearance."""
    out: dict[tuple[str, str], JobLink] = {}
    for href in links:
        link = canonicalise(href)
        if link and (platform is None or link.platform == platform):
            out.setdefault((link.platform, link.job_id), link)
    return list(out.values())
