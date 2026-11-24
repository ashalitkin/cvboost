"""Ashby public job posting API: https://api.ashbyhq.com/posting-api/job-board/{slug}

Docs: https://developers.ashbyhq.com/docs/public-job-posting-api. `includeCompensation=true`
adds the salary components, which become the Job salary fields.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from cvboost.connectors._util import http_url_or_none, salary_number
from cvboost.connectors.ats import ATSConnector
from cvboost.models import EmploymentType, Job, RawPosting, SalaryPeriod, SourceKind
from cvboost.textutil import canonical_url, html_to_text, utc_from_naive_iso

TYPES: dict[str, EmploymentType] = {
    "fulltime": "full_time",
    "parttime": "part_time",
    "contract": "contract",
    "intern": "internship",
    "temporary": "other",
}
# summaryComponents[].interval is "1 YEAR", "1 MONTH", "1 HOUR", ...
PERIODS: dict[str, SalaryPeriod] = {
    "year": "year",
    "month": "month",
    "day": "day",
    "hour": "hour",
}


def _salary(comp: object) -> tuple[float | None, float | None, str | None, SalaryPeriod | None]:
    """(min, max, currency, period) of the Salary component, else (None,) * 4."""
    parts = (comp or {}).get("summaryComponents") if isinstance(comp, dict) else None
    for c in parts or []:
        if not isinstance(c, dict) or c.get("compensationType") != "Salary":
            continue
        lo, hi = salary_number(c.get("minValue")), salary_number(c.get("maxValue"))
        if lo is None and hi is None:
            continue
        unit = str(c.get("interval") or "").lower().split()[-1:]
        return lo, hi, (c.get("currencyCode") or None), PERIODS.get(unit[0] if unit else "")
    return None, None, None, None


class AshbyConnector(ATSConnector):
    name: ClassVar[str] = "ashby"
    kind: ClassVar[SourceKind] = "ats"
    base_url: ClassVar[str] = "https://api.ashbyhq.com/posting-api/job-board"
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"api.ashbyhq.com"})
    tos_note: ClassVar[str] = (
        "Official public job posting API (developers.ashbyhq.com/docs/public-job-posting-api). "
        "1 s between companies, each company at most every 6 h, 404 = company removed from the "
        "polling."
    )
    attribution: ClassVar[str] = "via company careers page (Ashby)"

    def company_url(self, slug: str) -> str:
        return f"{self.base_url}/{slug}?includeCompensation=true"

    def postings(self, data: Any) -> list[dict[str, Any]]:
        jobs = data["jobs"]
        if not isinstance(jobs, list):
            raise ValueError("jobs is not a list")
        return [j for j in jobs if isinstance(j, dict) and j.get("isListed", True)]

    def posting_id(self, item: dict[str, Any]) -> str | None:
        return str(item["id"]) if item.get("id") else None

    def posting_title(self, item: dict[str, Any]) -> object:
        return item.get("title")

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p: dict[str, Any] = raw.payload
        html = p.get("descriptionHtml") or ""
        lo, hi, currency, period = _salary(p.get("compensation"))
        comp_text = ((p.get("compensation") or {}).get("compensationTierSummary") or "").strip()
        workplace = str(p.get("workplaceType") or "").lower()
        remote = p.get("isRemote")
        if not isinstance(remote, bool):
            remote = True if workplace == "remote" else None
        etype = str(p.get("employmentType") or "").strip().lower()
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(p["jobUrl"]),
            apply_url=http_url_or_none(p.get("applyUrl")),
            company=p["_company_name"],
            title=p["title"].strip(),
            description_html=html,
            description_text=html_to_text(html) or (p.get("descriptionPlain") or ""),
            location_text=(p.get("location") or "").strip() or None,
            remote=remote,
            employment_type=(TYPES.get(etype, "other") if etype else None),
            tags=[str(p[k]).strip() for k in ("department", "team") if p.get(k)],
            salary_min=lo,
            salary_max=hi,
            salary_currency=currency if lo or hi else None,
            salary_period=period if lo or hi else None,
            salary_text=comp_text or None,
            posted_at=utc_from_naive_iso(p["publishedAt"]) if p.get("publishedAt") else None,
            fetched_at=fetched_at,
            raw=p,
        )
