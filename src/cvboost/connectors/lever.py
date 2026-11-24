"""Lever Postings API: https://api.lever.co/v0/postings/{slug}?mode=json

Public GET; Lever documents that postings "may be scraped by third parties"
(https://github.com/lever/postings-api). Companies on Lever's EU instance (api.eu.lever.co) are
not covered.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from cvboost.connectors._util import http_url_or_none, salary_number, utc_from_epoch
from cvboost.connectors.ats import ATSConnector
from cvboost.models import EmploymentType, Job, RawPosting, SalaryPeriod, SourceKind
from cvboost.textutil import canonical_url, html_to_text

COMMITMENT: dict[str, EmploymentType] = {
    "full-time": "full_time",
    "full time": "full_time",
    "part-time": "part_time",
    "part time": "part_time",
    "contract": "contract",
    "contractor": "contract",
    "consultant": "contract",
    "freelance": "freelance",
    "intern": "internship",
    "internship": "internship",
}
# salaryRange.interval is e.g. "per-year-salary", "per-hour-wage"
PERIODS: tuple[tuple[str, SalaryPeriod], ...] = (
    ("year", "year"),
    ("month", "month"),
    ("day", "day"),
    ("hour", "hour"),
)


class LeverConnector(ATSConnector):
    name: ClassVar[str] = "lever"
    kind: ClassVar[SourceKind] = "ats"
    base_url: ClassVar[str] = "https://api.lever.co/v0/postings"
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"api.lever.co"})
    tos_note: ClassVar[str] = (
        "Official public Postings API; Lever's docs say postings may be scraped by third "
        "parties (github.com/lever/postings-api). 1 s between companies, each company at most "
        "every 6 h, 404 = company removed from the polling."
    )
    attribution: ClassVar[str] = "via company careers page (Lever)"

    def company_url(self, slug: str) -> str:
        return f"{self.base_url}/{slug}?mode=json"

    def postings(self, data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, list):
            raise ValueError("postings response is not a list")
        return [j for j in data if isinstance(j, dict)]

    def posting_id(self, item: dict[str, Any]) -> str | None:
        return str(item["id"]) if item.get("id") else None

    def posting_title(self, item: dict[str, Any]) -> object:
        return item.get("text")

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p: dict[str, Any] = raw.payload
        cats = p.get("categories") or {}
        html = p.get("description") or ""
        for part in p.get("lists") or []:
            if isinstance(part, dict):
                html += f"<h3>{part.get('text') or ''}</h3>{part.get('content') or ''}"
        html += p.get("additional") or ""
        workplace = str(p.get("workplaceType") or "").lower()
        rng = p.get("salaryRange") or {}
        lo, hi = salary_number(rng.get("min")), salary_number(rng.get("max"))
        interval = str(rng.get("interval") or "").lower()
        period = next((v for k, v in PERIODS if k in interval), None)
        commitment = str(cats.get("commitment") or "").strip().lower()
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(p["hostedUrl"]),
            apply_url=http_url_or_none(p.get("applyUrl")),
            company=p["_company_name"],
            title=p["text"].strip(),
            description_html=html,
            description_text=html_to_text(html),
            location_text=(cats.get("location") or "").strip() or None,
            remote=True if workplace == "remote" else (False if workplace else None),
            employment_type=(COMMITMENT.get(commitment, "other") if commitment else None),
            tags=[str(cats[k]).strip() for k in ("team", "department") if cats.get(k)],
            salary_min=lo,
            salary_max=hi,
            salary_currency=(rng.get("currency") or None) if lo or hi else None,
            salary_period=period if lo or hi else None,
            posted_at=utc_from_epoch(p["createdAt"] / 1000)
            if isinstance(p.get("createdAt"), int | float)
            else None,
            fetched_at=fetched_at,
            raw=p,
        )
