"""RemoteFirstJobs: public RSS feeds (latest 100 jobs each)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import ClassVar

from cvboost.connectors._util import last_path_segment, rss_items
from cvboost.connectors.base import Connector
from cvboost.models import EmploymentType, Job, RawPosting, SourceKind
from cvboost.textutil import canonical_url, html_to_text, parse_rfc822

FEEDS = ["jobs", "jobs/data", "jobs/contract"]
_LOCATION = re.compile(r"^Location:[ \t]*(.{1,80})$", re.MULTILINE)
_TYPE = re.compile(r"^Type:[ \t]*(.{1,40})$", re.MULTILINE)
TYPES: dict[str, EmploymentType] = {
    "full time": "full_time",
    "part time": "part_time",
    "contract": "contract",
    "freelance": "freelance",
    "internship": "internship",
}


class RemoteFirstJobsConnector(Connector):
    name: ClassVar[str] = "remotefirstjobs"
    kind: ClassVar[SourceKind] = "rss"
    base_url: ClassVar[str] = "https://remotefirstjobs.com/rss"
    interval: ClassVar[timedelta] = timedelta(hours=3)
    min_delay_s: ClassVar[float] = 5.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"remotefirstjobs.com"})
    tos_note: ClassVar[str] = (
        "Their /rss page: 'Our RSS feeds are free and public. If you use them ... please: Credit "
        "us - include a link back to Remote First Jobs as the source. Do not submit jobs from our "
        "feeds to LinkedIn Jobs, Google Jobs, Jooble, or similar platforms.' robots.txt does not "
        "list /rss/. Personal use only; we never re-publish."
    )
    attribution: ClassVar[str] = "via Remote First Jobs"

    def requests(self) -> list[str]:
        return [f"https://remotefirstjobs.com/rss/{f}.rss" for f in FEEDS]

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        out = []
        for fields in rss_items(body):
            sid = fields.get("guid") or last_path_segment(fields.get("link", ""))
            if sid:
                out.append(RawPosting(source=self.name, source_id=sid, payload=fields))
        return out

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p = raw.payload
        title, sep, company = p["title"].rpartition(" at ")
        if not sep or not title.strip() or not company.strip():
            raise ValueError(f"title has no 'Title at Company' form: {p['title']!r}")
        html = p.get("description") or ""
        text = html_to_text(html)
        loc = _LOCATION.search(text)
        typ = _TYPE.search(text)
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(p["link"]),
            company=company.strip(),
            title=title.strip(),
            description_html=html,
            description_text=text,
            location_text=loc.group(1).strip() if loc else None,
            remote=True,
            employment_type=TYPES.get(typ.group(1).strip().lower().replace("-", " "), "other")
            if typ
            else None,
            posted_at=parse_rfc822(p["pubDate"]) if p.get("pubDate") else None,
            fetched_at=fetched_at,
            raw=p,
        )
