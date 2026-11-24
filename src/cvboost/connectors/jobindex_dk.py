"""Jobindex (Denmark): public RSS search feeds. Danish-language ads; the language rule filters."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import ClassVar

from selectolax.lexbor import LexborHTMLParser

from cvboost.connectors._util import rss_items
from cvboost.connectors.base import Connector
from cvboost.models import Job, RawPosting, SourceKind
from cvboost.textutil import canonical_url, html_to_text, parse_rfc822

# One '+' only: robots.txt has `Disallow: /jobsoegning*?*+*+` (three or more words).
QUERIES = ["data+analyst", "data+engineer"]


class JobindexDKConnector(Connector):
    name: ClassVar[str] = "jobindex_dk"
    kind: ClassVar[SourceKind] = "rss"
    base_url: ClassVar[str] = "https://www.jobindex.dk/jobsoegning.rss"
    interval: ClassVar[timedelta] = timedelta(hours=6)
    min_delay_s: ClassVar[float] = 10.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"www.jobindex.dk"})
    tos_note: ClassVar[str] = (
        "robots.txt (checked 2026-11-08) has no rule for /jobsoegning.rss with only `q=`; it "
        "blocks `page=`, `sort=`, `radius=` etc. and queries with two or more '+', so we send "
        "`q=` with at most one '+'. Public RSS feed (ttl 1440). Privacy/terms page has no "
        "scraping clause. Link back to the ad and name Jobindex."
    )
    attribution: ClassVar[str] = "via Jobindex"

    def requests(self) -> list[str]:
        return [f"{self.base_url}?q={q}" for q in QUERIES]

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        out = []
        for fields in rss_items(body):
            guid = fields.get("guid", "")
            sid = guid.rstrip("/").rsplit("/", 1)[-1]
            if sid.isdigit() and fields.get("link"):
                out.append(RawPosting(source=self.name, source_id=sid, payload=fields))
        return out

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p = raw.payload
        title, sep, company = p["title"].rpartition(", ")
        if not sep or not title.strip() or not company.strip():
            raise ValueError(f"title has no 'Title, Company' form: {p['title']!r}")
        html = p.get("description") or ""
        area = LexborHTMLParser(html).css_first(".jix_robotjob--area") if html else None
        location = area.text().strip() if area is not None else ""
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(p["link"]),
            company=company.strip(),
            title=title.strip(),
            description_html=html,
            description_text=html_to_text(html),
            location_text=location or None,
            tags=[c for c in p.get("category", "").split("\n") if c],
            posted_at=parse_rfc822(p["pubDate"]) if p.get("pubDate") else None,
            fetched_at=fetched_at,
            raw=p,
        )
