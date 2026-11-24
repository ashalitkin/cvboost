"""Remotive public API: https://remotive.com/api/remote-jobs (JSON).

Disabled by default: robots.txt has `Disallow: /api/*`, owner decision pending.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, ClassVar

from cvboost.connectors.base import Connector
from cvboost.models import EmploymentType, Job, RawPosting, SourceKind
from cvboost.textutil import canonical_url, html_to_text, utc_from_naive_iso

JOB_TYPES: dict[str, EmploymentType] = {
    "full_time": "full_time",
    "part_time": "part_time",
    "contract": "contract",
    "freelance": "freelance",
    "internship": "internship",
}


class RemotiveConnector(Connector):
    name: ClassVar[str] = "remotive"
    kind: ClassVar[SourceKind] = "api"
    base_url: ClassVar[str] = "https://remotive.com/api/remote-jobs"
    interval: ClassVar[timedelta] = timedelta(hours=6)
    min_delay_s: ClassVar[float] = 30.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"remotive.com"})
    tos_note: ClassVar[str] = (
        "Link to the Remotive URL and name Remotive as the source; no re-posting on third-party "
        "sites; data delayed 24h. robots.txt disallows /api/*, "
        "but the documented API is the one used here."
    )
    attribution: ClassVar[str] = "via Remotive"
    default_enabled: ClassVar[bool] = True

    def requests(self) -> list[str]:
        return [self.base_url]

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        data = json.loads(body)
        return [
            RawPosting(source=self.name, source_id=str(item["id"]), payload=item)
            for item in data.get("jobs", [])
            if item.get("id") is not None
        ]

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p: dict[str, Any] = raw.payload
        html = p.get("description") or ""
        salary = (p.get("salary") or "").strip() or None
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(p["url"]),
            company=p["company_name"],
            title=p["title"],
            description_html=html,
            description_text=html_to_text(html),
            location_text=(p.get("candidate_required_location") or None),
            remote=True,
            employment_type=JOB_TYPES.get(p.get("job_type") or "", "other")
            if p.get("job_type")
            else None,
            tags=[str(t).strip() for t in p.get("tags") or []],
            salary_text=salary,
            posted_at=utc_from_naive_iso(p["publication_date"])
            if p.get("publication_date")
            else None,
            fetched_at=fetched_at,
            raw=p,
        )
