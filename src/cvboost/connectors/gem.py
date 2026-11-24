"""Gem job board API: https://api.gem.com/job_board/v0/{slug}/job_posts/

Vendor-published, documented Job Board API (api.gem.com/job_board/v0/reference, help.gem.com
"The Job Board API"): reading job posts is public, only submitting an application needs a key.
One request returns a bare array of all open posts with their HTML content. The posts live on
jobs.gem.com/{slug}/{id}.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from cvboost.connectors._util import http_url_or_none
from cvboost.connectors.ats import ATSConnector
from cvboost.models import EmploymentType, Job, RawPosting, SourceKind
from cvboost.textutil import canonical_url, html_to_text, utc_from_naive_iso

TYPES: dict[str, EmploymentType] = {
    "full_time": "full_time",
    "part_time": "part_time",
    "contract": "contract",
    "contractor": "contract",
    "intern": "internship",
    "internship": "internship",
    "temporary": "other",
}


class GemConnector(ATSConnector):
    name: ClassVar[str] = "gem"
    kind: ClassVar[SourceKind] = "ats"
    base_url: ClassVar[str] = "https://api.gem.com/job_board/v0"
    # jobs.gem.com is listed only so that posting links pass the link check of the alerts; the
    # fetch allow-list is the API host alone (allowed_hosts_for)
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"api.gem.com", "jobs.gem.com"})
    tos_note: ClassVar[str] = (
        "Official documented Job Board API (api.gem.com/job_board/v0/reference): reading is "
        'public. api.gem.com answers /robots.txt with 403 `{"message":"Forbidden"}` (an API '
        "gateway page, no rules; RFC 9309: unavailable = no restrictions). The Gem terms of "
        "service do not mention automated reading. 1 s between companies, each company at most "
        "every 6 h, 404 = company removed."
    )
    attribution: ClassVar[str] = "via company careers page (Gem)"

    def company_url(self, slug: str) -> str:
        return f"{self.base_url}/{slug}/job_posts/"

    def allowed_hosts_for(self, slug: str) -> frozenset[str]:
        return frozenset({"api.gem.com"})

    def postings(self, data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, list):
            raise ValueError("job_posts response is not a list")
        return [j for j in data if isinstance(j, dict)]

    def posting_id(self, item: dict[str, Any]) -> str | None:
        return str(item["id"]) if item.get("id") else None

    def posting_title(self, item: dict[str, Any]) -> object:
        return item.get("title")

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p: dict[str, Any] = raw.payload
        html = p.get("content") or ""
        loc = p.get("location")
        place = (loc.get("name") if isinstance(loc, dict) else None) or ""
        loc_type = str(p.get("location_type") or "").lower()
        etype = str(p.get("employment_type") or "").strip().lower()
        posted = p.get("first_published_at") or p.get("created_at")
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(p["absolute_url"]),
            apply_url=http_url_or_none(p["absolute_url"]),
            company=p["_company_name"],
            title=p["title"].strip(),
            description_html=html,
            description_text=html_to_text(html) or (p.get("content_plain") or ""),
            location_text=place.strip() or None,
            remote=(loc_type == "remote") if loc_type else None,
            employment_type=(TYPES.get(etype, "other") if etype else None),
            tags=[
                str(d["name"]).strip()
                for d in p.get("departments") or []
                if isinstance(d, dict) and d.get("name")
            ],
            posted_at=utc_from_naive_iso(posted) if isinstance(posted, str) else None,
            fetched_at=fetched_at,
            raw=p,
        )
