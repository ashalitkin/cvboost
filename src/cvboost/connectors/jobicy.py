"""Jobicy public API: https://jobicy.com/api/jobs (JSON, cursor pagination).

Poll a few times a day at most (not more than once an hour); name Jobicy and link to the job.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import quote

from cvboost.connectors._util import salary_number, salary_period
from cvboost.connectors.base import Connector
from cvboost.models import EmploymentType, Job, RawPosting, SalaryPeriod, SourceKind
from cvboost.textutil import canonical_url, html_to_text, utc_from_naive_iso

TYPES: dict[str, EmploymentType] = {
    "full-time": "full_time",
    "part-time": "part_time",
    "contract": "contract",
    "internship": "internship",
    "freelance": "freelance",
}
PERIODS: dict[str, SalaryPeriod] = {
    "yearly": "year",
    "monthly": "month",
    "daily": "day",
    "hourly": "hour",
}


def _employment_type(value: object) -> EmploymentType | None:
    items = value if isinstance(value, list) else [value] if value else []
    items = [str(i).strip().lower() for i in items if str(i).strip()]
    if not items:
        return None
    for item in items:
        if item in TYPES:
            return TYPES[item]
    return "other"


class JobicyConnector(Connector):
    name: ClassVar[str] = "jobicy"
    kind: ClassVar[SourceKind] = "api"
    base_url: ClassVar[str] = "https://jobicy.com/api/jobs?count=100"
    interval: ClassVar[timedelta] = timedelta(hours=6)
    min_delay_s: ClassVar[float] = 5.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"jobicy.com"})
    tos_note: ClassVar[str] = (
        "Name Jobicy and link to the job; Apply goes to the original URL; storing and AI "
        "processing are allowed; poll at most a few times a day (never more than hourly); "
        "mark closed jobs. robots.txt does not close /api/jobs."
    )
    attribution: ClassVar[str] = "via Jobicy"
    max_pages = 2

    def requests(self) -> list[str]:
        return [self.base_url]

    def next_url(self, body: bytes, url: str, page: int) -> str | None:
        cursor = json.loads(body).get("nextCursor")
        if not cursor:
            return None
        return f"{self.base_url}&cursor={quote(str(cursor), safe='')}"

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        data = json.loads(body)
        return [
            RawPosting(source=self.name, source_id=str(item["id"]), payload=item)
            for item in data.get("jobs", [])
            if item.get("id") is not None
        ]

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p: dict[str, Any] = raw.payload
        html = p.get("jobDescription") or ""
        lo, hi = salary_number(p.get("salaryMin")), salary_number(p.get("salaryMax"))
        industry = p.get("jobIndustry") or []
        if isinstance(industry, str):
            industry = [industry]
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(p["url"]),
            company=p.get("companyName") or "",
            title=p["jobTitle"],
            description_html=html,
            description_text=html_to_text(html),
            location_text=re.sub(r"\s*,\s*", ", ", p.get("jobGeo") or "").strip() or None,
            remote=True,
            employment_type=_employment_type(p.get("jobType")),
            tags=[str(i).strip() for i in industry if str(i).strip()],
            salary_min=lo,
            salary_max=hi,
            salary_currency=(p.get("salaryCurrency") or None) if (lo or hi) else None,
            salary_period=salary_period(p.get("salaryPeriod"), PERIODS),
            posted_at=utc_from_naive_iso(p["pubDate"]) if p.get("pubDate") else None,
            fetched_at=fetched_at,
            raw=p,
        )
