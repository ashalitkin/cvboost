"""Recruitee Careers Site API: https://{slug}.recruitee.com/api/offers/

Public, unauthenticated GET of the published offers of one company (the endpoint Recruitee's own
careers sites and widgets use). One request returns all offers with HTML descriptions, remote
flag, location, employment type and, when the company shows it, the salary. The slug is the
company's recruitee.com sub-domain. A company with its own careers domain may redirect
`{slug}.recruitee.com/` there; the API path normally still answers on recruitee.com, and an
off-host redirect is refused by the allow-list (counted as a failure).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit

from cvboost.connectors._util import http_url_or_none, salary_number
from cvboost.connectors.ats import ATSConnector
from cvboost.models import EmploymentType, Job, RawPosting, SalaryPeriod, SourceKind
from cvboost.textutil import canonical_url, html_to_text

# Fields that make a payload big and carry no job content for us (application-form questions,
# per-language copies of the description).
_BULKY = ("open_questions", "translations", "dynamic_fields", "locations")
PERIODS: dict[str, SalaryPeriod] = {
    "year": "year",
    "yearly": "year",
    "month": "month",
    "monthly": "month",
    "day": "day",
    "daily": "day",
    "hour": "hour",
    "hourly": "hour",
}


def employment_type(code: object) -> EmploymentType | None:
    """employment_type_code: fulltime_permanent, parttime_temporary, freelance, internship, ..."""
    c = str(code or "").strip().lower()
    if not c:
        return None
    if "freelance" in c:
        return "freelance"
    if "contract" in c:
        return "contract"
    if "intern" in c or "trainee" in c or "apprentice" in c:
        return "internship"
    if c.startswith("parttime"):
        return "part_time"
    if c.startswith("fulltime"):
        return "full_time"
    return "other"


class RecruiteeConnector(ATSConnector):
    name: ClassVar[str] = "recruitee"
    kind: ClassVar[SourceKind] = "ats"
    base_url: ClassVar[str] = "https://recruitee.com"
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"recruitee.com"})  # link check only
    tos_note: ClassVar[str] = (
        "Public careers-site API (GET, no auth) that the company's own recruitee.com careers "
        "page uses. robots.txt of the tenants: `User-agent: * / Allow: /` (a tenant with its own "
        "domain redirects /robots.txt there). Fetch allow-list is the one tenant host. 1 s "
        "between companies, each company at most every 6 h, 404 = company removed."
    )
    attribution: ClassVar[str] = "via company careers page (Recruitee)"

    def company_url(self, slug: str) -> str:
        return f"https://{slug}.recruitee.com/api/offers/"

    def allowed_hosts_for(self, slug: str) -> frozenset[str]:
        return frozenset({f"{slug}.recruitee.com"})

    def slug_of(self, url: str) -> str:
        host = (urlsplit(url).hostname or "").lower()
        label, _, rest = host.partition(".")
        if rest != "recruitee.com" or not label:
            raise ValueError("not a Recruitee URL")
        return label

    def postings(self, data: Any) -> list[dict[str, Any]]:
        offers = data["offers"]
        if not isinstance(offers, list):
            raise ValueError("offers is not a list")
        out = []
        for o in offers:
            if isinstance(o, dict) and o.get("status", "published") == "published":
                out.append({k: v for k, v in o.items() if k not in _BULKY})
        return out

    def posting_id(self, item: dict[str, Any]) -> str | None:
        return str(item["id"]) if item.get("id") is not None else None

    def posting_title(self, item: dict[str, Any]) -> object:
        return item.get("title")

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p: dict[str, Any] = raw.payload
        slug = p["_company_slug"]
        html = (p.get("description") or "") + (
            "<h3>Requirements</h3>" + p["requirements"] if p.get("requirements") else ""
        )
        url = (
            p.get("careers_url")
            or f"https://{slug}.recruitee.com/o/{p.get('slug') or raw.source_id}"
        )
        sal = p.get("salary") if isinstance(p.get("salary"), dict) else {}
        lo, hi = salary_number(_num(sal.get("min"))), salary_number(_num(sal.get("max")))
        remote = p.get("remote")
        if remote is not True and p.get("on_site") is True:
            remote = False
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(url),
            apply_url=http_url_or_none(p.get("careers_apply_url")),
            company=p.get("company_name") or p["_company_name"],
            title=p["title"].strip(),
            description_html=html,
            description_text=html_to_text(html),
            location_text=(p.get("location") or "").strip() or None,
            remote=remote if isinstance(remote, bool) else None,
            employment_type=employment_type(p.get("employment_type_code")),
            tags=[str(p[k]).strip() for k in ("department",) if p.get(k)],
            salary_min=lo,
            salary_max=hi,
            salary_currency=(sal.get("currency") or None) if lo or hi else None,
            salary_period=PERIODS.get(str(sal.get("period") or "").lower()) if lo or hi else None,
            posted_at=_posted(p),
            fetched_at=fetched_at,
            raw=p,
        )


def _num(value: object) -> object:
    """Salary bounds come as numbers or numeric strings."""
    if isinstance(value, str):
        try:
            return float(value.replace(",", ""))
        except ValueError:
            return None
    return value


def _posted(p: dict[str, Any]) -> datetime | None:
    value = p.get("published_at") or p.get("created_at")
    if not isinstance(value, str):
        return None
    try:  # "2026-10-30 16:32:58 UTC"
        dt = datetime.strptime(value.removesuffix(" UTC"), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC)
