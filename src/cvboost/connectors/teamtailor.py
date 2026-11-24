"""Teamtailor job feed: https://{slug}.teamtailor.com/jobs.rss

Public RSS 2.0 feed of a company's career site (Teamtailor documents the RSS feed for job
boards and aggregators). `<description>` is the HTML text, `<tt:locations>` the places,
`<remoteStatus>` is none / hybrid / fully. The feed has no salary and no employment type.
The slug is the company's teamtailor.com sub-domain (regional `*.na.teamtailor.com` tenants are
not covered).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit

from defusedxml import ElementTree as ET

from cvboost.connectors._util import http_url_or_none, xml_text
from cvboost.connectors.ats import ATSConnector
from cvboost.models import Job, RawPosting, SourceKind
from cvboost.textutil import canonical_url, html_to_text, parse_rfc822

TT = "{https://teamtailor.com/locations}"
MAX_ITEMS = 2000  # a feed larger than this is not a career site


class TeamtailorConnector(ATSConnector):
    name: ClassVar[str] = "teamtailor"
    kind: ClassVar[SourceKind] = "ats"
    base_url: ClassVar[str] = "https://teamtailor.com"
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"teamtailor.com"})  # link check only
    tos_note: ClassVar[str] = (
        "Public RSS feed of the company's career site. robots.txt (career.teamtailor.com): "
        "`User-agent: * / Disallow: /app/, /messages/, /messenger/, /facebook/tab/, "
        "/jobs/internal/` and `Content-Signal: search=yes` - /jobs.rss is allowed. Fetch "
        "allow-list is the one tenant host. 1 s between companies, each company at most every "
        "6 h, 404 = company removed."
    )
    attribution: ClassVar[str] = "via company careers page (Teamtailor)"

    def company_url(self, slug: str) -> str:
        return f"https://{slug}.teamtailor.com/jobs.rss"

    def allowed_hosts_for(self, slug: str) -> frozenset[str]:
        return frozenset({f"{slug}.teamtailor.com"})

    def slug_of(self, url: str) -> str:
        host = (urlsplit(url).hostname or "").lower()
        label, _, rest = host.partition(".")
        if rest != "teamtailor.com" or not label:
            raise ValueError("not a Teamtailor URL")
        return label

    def decode(self, body: bytes) -> Any:
        return ET.fromstring(body, forbid_dtd=True)

    def postings(self, data: Any) -> list[dict[str, Any]]:
        if data.tag != "rss":
            raise ValueError("not an RSS feed")
        items = data.findall("./channel/item")
        if len(items) > MAX_ITEMS:
            raise ValueError("feed too large")
        out = []
        for it in items:
            places = []
            for loc in it.findall(f"./{TT}locations/{TT}location"):
                place = {k: xml_text(loc, f"{TT}{k}") for k in ("name", "city", "country")}
                places.append({k: v for k, v in place.items() if v})
            out.append(
                {
                    "guid": xml_text(it, "guid"),
                    "title": xml_text(it, "title"),
                    "description": xml_text(it, "description"),
                    "pubDate": xml_text(it, "pubDate"),
                    "link": xml_text(it, "link"),
                    "remoteStatus": xml_text(it, "remoteStatus"),
                    "locations": places,
                    "department": xml_text(it, f"{TT}department"),
                    "role": xml_text(it, f"{TT}role"),
                }
            )
        return out

    def posting_id(self, item: dict[str, Any]) -> str | None:
        return item["guid"] or None

    def posting_title(self, item: dict[str, Any]) -> object:
        return item.get("title")

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p: dict[str, Any] = raw.payload
        html = p.get("description") or ""
        places = []
        for loc in p.get("locations") or []:
            text = ", ".join(
                v for v in (loc.get("city") or loc.get("name"), loc.get("country")) if v
            )
            if text and text not in places:
                places.append(text)
        status = str(p.get("remoteStatus") or "").lower()
        remote = True if status == "fully" else (False if status in ("none", "hybrid") else None)
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(p["link"]),
            apply_url=http_url_or_none(p["link"]),
            company=p["_company_name"],
            title=p["title"].strip(),
            description_html=html,
            description_text=html_to_text(html),
            location_text="; ".join(places) or None,
            remote=remote,
            tags=[str(p[k]).strip() for k in ("department", "role") if p.get(k)],
            posted_at=parse_rfc822(p["pubDate"]) if p.get("pubDate") else None,
            fetched_at=fetched_at,
            raw=p,
        )
