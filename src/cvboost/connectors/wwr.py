"""We Work Remotely: one RSS feed per category."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import urlsplit

from defusedxml import ElementTree as ET

from cvboost.connectors.base import Connector
from cvboost.models import EmploymentType, Job, RawPosting, SourceKind
from cvboost.textutil import canonical_url, html_to_text, parse_rfc822

DEFAULT_CATEGORIES = [
    "remote-back-end-programming-jobs",
    "remote-full-stack-programming-jobs",
    "remote-devops-sysadmin-jobs",
    "remote-sales-and-marketing-jobs",
    "remote-product-jobs",
    "remote-management-and-finance-jobs",
    "all-other-remote-jobs",
]

TYPES: dict[str, EmploymentType] = {
    "full-time": "full_time",
    "part-time": "part_time",
    "contract": "contract",
    "freelance": "freelance",
    "internship": "internship",
}


def _norm_type(value: str | None) -> EmploymentType | None:
    if not value:
        return None
    return TYPES.get(value.strip().lower().replace(" ", "-"), "other")


class WWRConnector(Connector):
    name: ClassVar[str] = "wwr"
    kind: ClassVar[SourceKind] = "rss"
    base_url: ClassVar[str] = "https://weworkremotely.com/categories"
    interval: ClassVar[timedelta] = timedelta(hours=1)
    min_delay_s: ClassVar[float] = 5.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"weworkremotely.com"})
    tos_note: ClassVar[str] = (
        "robots.txt allows /. ToS page is behind Cloudflare (could not be read); "
        "link back to the posting and name We Work Remotely."
    )
    attribution: ClassVar[str] = "via We Work Remotely"

    def __init__(self, categories: list[str] | None = None):
        self.categories = list(categories) if categories is not None else list(DEFAULT_CATEGORIES)

    def requests(self) -> list[str]:
        return [f"{self.base_url}/{c}.rss" for c in self.categories]

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        root = ET.fromstring(body)
        out = []
        for item in root.iterfind("./channel/item"):
            fields: dict[str, Any] = {}
            skills = []
            for child in item:
                tag = child.tag
                if not isinstance(tag, str):
                    continue
                text = (child.text or "").strip()
                if tag == "skills":
                    skills = [
                        s.strip().removeprefix("and ").strip() for s in text.split(",") if s.strip()
                    ]
                elif tag not in fields:
                    fields[tag] = text
            fields["skills"] = skills
            link = fields.get("link") or fields.get("guid") or ""
            slug = urlsplit(link).path.rstrip("/").rsplit("/", 1)[-1]
            if not slug:
                continue
            out.append(RawPosting(source=self.name, source_id=slug, payload=fields))
        return out

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p = raw.payload
        full_title = p["title"]
        company, sep, title = full_title.partition(": ")
        if not sep:
            raise ValueError(f"title has no 'Company: Title' form: {full_title!r}")
        html = p.get("description") or ""
        region, country = p.get("region") or "", p.get("country") or ""
        location = f"{region} ({country})" if region and country else (region or country or None)
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(p.get("link") or p["guid"]),
            company=company.strip(),
            title=title.strip(),
            description_html=html,
            description_text=html_to_text(html),
            location_text=location,
            remote=True,
            employment_type=_norm_type(p.get("type")),
            tags=p.get("skills", []),
            posted_at=parse_rfc822(p["pubDate"]) if p.get("pubDate") else None,
            fetched_at=fetched_at,
            raw=p,
        )
