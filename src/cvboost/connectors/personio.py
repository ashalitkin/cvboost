"""Personio XML job feed: https://{slug}.jobs.personio.de/xml

Personio's documented XML interface for job boards and career pages (`<workzag-jobs>`), public
GET. Each `<position>` has the office(s), department, name, a list of named description blocks
(CDATA HTML), employment type, schedule, seniority, keywords and `createdAt`. No salary and no
remote flag: remote shows only in an office name ("Madrid (Remote)"). The slug is the company's
jobs.personio.de sub-domain.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit

from defusedxml import ElementTree as ET

from cvboost.connectors._util import xml_text
from cvboost.connectors.ats import ATSConnector
from cvboost.models import EmploymentType, Job, RawPosting, SourceKind
from cvboost.textutil import canonical_url, html_to_text, utc_from_naive_iso

MAX_POSITIONS = 2000
SCHEDULES: dict[str, EmploymentType] = {"full-time": "full_time", "part-time": "part_time"}
KINDS: dict[str, EmploymentType] = {
    "intern": "internship",
    "trainee": "internship",
    "working_student": "internship",
    "freelance": "freelance",
    "freelancer": "freelance",
}
HOST_SUFFIX = ".jobs.personio.de"


class PersonioConnector(ATSConnector):
    name: ClassVar[str] = "personio"
    kind: ClassVar[SourceKind] = "ats"
    base_url: ClassVar[str] = "https://jobs.personio.de"
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"jobs.personio.de"})  # link check only
    tos_note: ClassVar[str] = (
        "Personio's XML job feed (documented interface for job boards), public GET. robots.txt "
        "of the tenants: `User-agent: * / Allow: /`. Fetch allow-list is the one tenant host. "
        "1 s between companies, each company at most every 6 h, 404 = company removed."
    )
    attribution: ClassVar[str] = "via company careers page (Personio)"

    def company_url(self, slug: str) -> str:
        return f"https://{slug}{HOST_SUFFIX}/xml"

    def allowed_hosts_for(self, slug: str) -> frozenset[str]:
        return frozenset({f"{slug}{HOST_SUFFIX}"})

    def slug_of(self, url: str) -> str:
        host = (urlsplit(url).hostname or "").lower()
        if not host.endswith(HOST_SUFFIX) or host == HOST_SUFFIX.lstrip("."):
            raise ValueError("not a Personio URL")
        return host.removesuffix(HOST_SUFFIX)

    def decode(self, body: bytes) -> Any:
        return ET.fromstring(body, forbid_dtd=True)

    def postings(self, data: Any) -> list[dict[str, Any]]:
        if data.tag != "workzag-jobs":
            raise ValueError("not a Personio feed")
        positions = data.findall("./position")
        if len(positions) > MAX_POSITIONS:
            raise ValueError("feed too large")
        out = []
        for pos in positions:
            blocks = [
                {"name": xml_text(b, "name"), "value": xml_text(b, "value")}
                for b in pos.findall("./jobDescriptions/jobDescription")
            ]
            offices = [xml_text(pos, "office")] + [
                (o.text or "").strip() for o in pos.findall("./additionalOffices/office")
            ]
            out.append(
                {
                    "id": xml_text(pos, "id"),
                    "name": xml_text(pos, "name"),
                    "offices": [o for o in offices if o],
                    "department": xml_text(pos, "department"),
                    "recruitingCategory": xml_text(pos, "recruitingCategory"),
                    "descriptions": blocks,
                    "employmentType": xml_text(pos, "employmentType"),
                    "schedule": xml_text(pos, "schedule"),
                    "seniority": xml_text(pos, "seniority"),
                    "keywords": xml_text(pos, "keywords"),
                    "createdAt": xml_text(pos, "createdAt"),
                }
            )
        return out

    def posting_id(self, item: dict[str, Any]) -> str | None:
        return item["id"] or None

    def posting_title(self, item: dict[str, Any]) -> object:
        return item.get("name")

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p: dict[str, Any] = raw.payload
        slug = p["_company_slug"]
        html = "".join(
            (f"<h3>{b['name']}</h3>" if b["name"] else "") + b["value"]
            for b in p.get("descriptions") or []
        )
        offices = p.get("offices") or []
        kind = str(p.get("employmentType") or "").lower()
        etype = KINDS.get(kind) or SCHEDULES.get(str(p.get("schedule") or "").lower())
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(f"https://{slug}{HOST_SUFFIX}/job/{raw.source_id}"),
            company=p["_company_name"],
            title=p["name"].strip(),
            description_html=html,
            description_text=html_to_text(html),
            location_text="; ".join(offices) or None,
            remote=True if any("remote" in o.lower() for o in offices) else None,
            employment_type=etype,
            tags=[str(p[k]).strip() for k in ("department",) if p.get(k)],
            posted_at=utc_from_naive_iso(p["createdAt"]) if p.get("createdAt") else None,
            fetched_at=fetched_at,
            raw=p,
        )
