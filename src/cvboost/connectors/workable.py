"""Workable job widget API: https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true

Public, unauthenticated GET that Workable serves for embedding a company's jobs in its own site
(the careers page itself calls a POST endpoint, `/api/v3/accounts/{slug}/jobs`, which we do not
use). One request returns all published jobs with their HTML descriptions. The widget has no
salary and no seniority fields.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit

from cvboost.connectors._util import http_url_or_none
from cvboost.connectors.ats import ATSConnector
from cvboost.models import EmploymentType, Job, RawPosting, SourceKind
from cvboost.textutil import canonical_url, html_to_text, utc_from_naive_iso

TYPES: dict[str, EmploymentType] = {
    "full-time": "full_time",
    "part-time": "part_time",
    "contract": "contract",
    "temporary": "other",
    "internship": "internship",
    "other": "other",
}


def _place(job: dict[str, Any]) -> str:
    return ", ".join(str(job[k]).strip() for k in ("city", "state", "country") if job.get(k))


class WorkableConnector(ATSConnector):
    name: ClassVar[str] = "workable"
    kind: ClassVar[SourceKind] = "ats"
    base_url: ClassVar[str] = "https://apply.workable.com/api/v1/widget/accounts"
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"apply.workable.com"})
    tos_note: ClassVar[str] = (
        "Public job widget endpoint of Workable (GET, no auth). robots.txt of apply.workable.com: "
        "`User-agent: * / Disallow:` (empty), content signals search=yes. The Workable terms "
        "(workable.com/terms) forbid harvesting candidates, not reading published openings. "
        "1 s between companies, each company at most every 6 h, 404 = company removed."
    )
    attribution: ClassVar[str] = "via company careers page (Workable)"

    def company_url(self, slug: str) -> str:
        return f"{self.base_url}/{slug}?details=true"

    def slug_of(self, url: str) -> str:
        segments = urlsplit(url).path.strip("/").split("/")
        if len(segments) < 5 or segments[3] != "accounts":
            raise ValueError("not a Workable widget URL")
        return segments[4]

    def postings(self, data: Any) -> list[dict[str, Any]]:
        jobs = data["jobs"]
        if not isinstance(jobs, list):
            raise ValueError("jobs is not a list")
        # a job open in several places comes back once per place with the same shortcode
        unique: dict[str, dict[str, Any]] = {}
        for j in jobs:
            if not isinstance(j, dict) or not j.get("shortcode"):
                continue
            first = unique.setdefault(str(j["shortcode"]), {**j, "places": []})
            place = _place(j)
            if place and place not in first["places"]:
                first["places"].append(place)
        return list(unique.values())

    def posting_id(self, item: dict[str, Any]) -> str | None:
        return str(item["shortcode"]) if item.get("shortcode") else None

    def posting_title(self, item: dict[str, Any]) -> object:
        return item.get("title")

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p: dict[str, Any] = raw.payload
        html = p.get("description") or ""
        place = "; ".join(p.get("places") or []) or _place(p)
        etype = str(p.get("employment_type") or "").strip().lower()
        remote = p.get("telecommuting")
        url = p.get("url") or p.get("shortlink") or f"https://apply.workable.com/j/{raw.source_id}"
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(url),
            apply_url=http_url_or_none(p.get("application_url")),
            company=p["_company_name"],
            title=p["title"].strip(),
            description_html=html,
            description_text=html_to_text(html),
            location_text=place or None,
            remote=remote if isinstance(remote, bool) else None,
            employment_type=(TYPES.get(etype, "other") if etype else None),
            tags=[str(p[k]).strip() for k in ("department", "function") if p.get(k)],
            posted_at=(utc_from_naive_iso(p["published_on"]) if p.get("published_on") else None),
            fetched_at=fetched_at,
            raw=p,
        )
