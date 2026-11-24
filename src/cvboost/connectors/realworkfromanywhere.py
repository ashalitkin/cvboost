"""Real Work From Anywhere: one public RSS feed (latest ~125 remote jobs)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import ClassVar

from cvboost.connectors._util import last_path_segment, rss_items
from cvboost.connectors.base import Connector
from cvboost.models import Job, RawPosting, SourceKind
from cvboost.textutil import canonical_url, html_to_text, parse_rfc822

# The live feed puts raw `&` into the enclosure url (not well-formed XML); the image is unused.
_ENCLOSURE = re.compile(r"<enclosure\b[^>]{0,2000}/>")


class RealWorkFromAnywhereConnector(Connector):
    name: ClassVar[str] = "realworkfromanywhere"
    kind: ClassVar[SourceKind] = "rss"
    base_url: ClassVar[str] = "https://www.realworkfromanywhere.com/rss.xml"
    interval: ClassVar[timedelta] = timedelta(hours=3)
    min_delay_s: ClassVar[float] = 5.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"www.realworkfromanywhere.com"})
    tos_note: ClassVar[str] = (
        "robots.txt: 'Allow: /', 'Disallow: /go/' only; /terms is 404; the feed is advertised "
        "(ttl 60). Link back to the posting and name Real Work From Anywhere."
    )
    attribution: ClassVar[str] = "via Real Work From Anywhere"

    def requests(self) -> list[str]:
        return [self.base_url]

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        out = []
        for fields in rss_items(_ENCLOSURE.sub("", body.decode("utf-8", "replace")).encode()):
            slug = last_path_segment(fields.get("link") or fields.get("guid") or "")
            if slug:
                out.append(RawPosting(source=self.name, source_id=slug, payload=fields))
        return out

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p = raw.payload
        company = (p.get("author") or "").strip()
        title = p["title"].strip()
        if company and title.endswith(f" at {company}"):
            title = title[: -len(f" at {company}")].strip()
        elif not company:
            title, sep, company = title.rpartition(" at ")
            if not sep:
                raise ValueError(f"no company in {p['title']!r}")
        html = p.get("description") or ""
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(p.get("link") or p["guid"]),
            company=company.strip(),
            title=title,
            description_html=html,
            description_text=html_to_text(html),
            location_text="Anywhere (work from anywhere)",
            remote=True,
            posted_at=parse_rfc822(p["pubDate"]) if p.get("pubDate") else None,
            fetched_at=fetched_at,
            raw=p,
        )
