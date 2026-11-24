"""Himalayas public API: https://himalayas.app/jobs/api/search (JSON, 20 jobs per page).

Terms (himalayas.app/terms): the Apply link must be `applicationLink` unchanged, show a visible
link to Himalayas, do not pass the data on to other boards, drop expired jobs (`expiryDate`).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from cvboost.connectors._util import (
    http_url_or_none,
    salary_number,
    salary_period,
    utc_from_epoch,
)
from cvboost.connectors.base import Connector
from cvboost.models import EmploymentType, Job, RawPosting, SalaryPeriod, SourceKind
from cvboost.textutil import canonical_url, html_to_text

PAGE_SIZE = 20
QUERIES = [
    "analytics engineer",
    "data engineer",
    "data architect",  # was "data analyst" (roles-fit: noisy; owner 2026-11-10 wants architects)
    "dbt",
    "bigquery",
    "looker",
    "marketing analytics",
    "google analytics",
]

TYPES: dict[str, EmploymentType] = {
    "full time": "full_time",
    "part time": "part_time",
    "contractor": "contract",
    "intern": "internship",
}
PERIODS: dict[str, SalaryPeriod] = {
    "annual": "year",
    "monthly": "month",
    "hourly": "hour",
    "daily": "day",
}


class HimalayasConnector(Connector):
    name: ClassVar[str] = "himalayas"
    kind: ClassVar[SourceKind] = "api"
    base_url: ClassVar[str] = "https://himalayas.app/jobs/api/search"
    interval: ClassVar[timedelta] = timedelta(hours=4)
    min_delay_s: ClassVar[float] = 3.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"himalayas.app"})
    tos_note: ClassVar[str] = (
        "Terms allow personal and commercial use; keep applicationLink unchanged as the Apply "
        "link, show a visible link to Himalayas, do not pass jobs to other boards, drop expired "
        "jobs (expiryDate kept in raw). robots.txt disallows `&page=` on /jobs*, but the "
        "documented API prescribes `page=`. No key: shared public quota."
    )
    attribution: ClassVar[str] = "via Himalayas"
    max_pages = 2

    def requests(self) -> list[str]:
        return [f"{self.base_url}?q={quote(q)}&sort=recent&page=1" for q in QUERIES]

    def next_url(self, body: bytes, url: str, page: int) -> str | None:
        if len(json.loads(body).get("jobs") or []) < PAGE_SIZE:  # last page
            return None
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "page"]
        query.append(("page", str(page + 1)))
        return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        data = json.loads(body)
        return [
            RawPosting(source=self.name, source_id=str(item["guid"]), payload=item)
            for item in data.get("jobs", [])
            if item.get("guid")
        ]

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p: dict[str, Any] = raw.payload
        html = p.get("description") or ""
        locations = [str(x).strip() for x in p.get("locationRestrictions") or [] if str(x).strip()]
        etype = p.get("employmentType")
        lo, hi = salary_number(p.get("minSalary")), salary_number(p.get("maxSalary"))
        has_salary = lo is not None or hi is not None
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(p["guid"]),
            apply_url=http_url_or_none(p.get("applicationLink")),
            company=p["companyName"],
            title=p["title"],
            description_html=html,
            description_text=html_to_text(html),
            location_text=", ".join(locations) or None,
            remote=True,
            employment_type=TYPES.get(str(etype).strip().lower(), "other") if etype else None,
            tags=[str(c).strip() for c in p.get("categories") or []],
            salary_min=lo,
            salary_max=hi,
            salary_currency=(p.get("currency") or None) if has_salary else None,
            salary_period=salary_period(p.get("salaryPeriod"), PERIODS),
            posted_at=utc_from_epoch(p.get("pubDate")),
            fetched_at=fetched_at,
            raw=p,
        )
