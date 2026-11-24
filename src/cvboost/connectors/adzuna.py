"""Adzuna job search API (key required): https://developer.adzuna.com/ (issue #19).

`GET https://api.adzuna.com/v1/api/jobs/{country}/search/1?app_id=..&app_key=..&what=..`.
Credentials: ADZUNA_APP_ID and ADZUNA_APP_KEY (query parameters, as Adzuna requires).
Terms (developer.adzuna.com/docs/terms_of_service): every displayed advert is labelled "Adzuna"
(we show "Jobs by Adzuna" as the link to the advert on Adzuna); predicted salaries are Adzuna's
estimates, not the employer's, so they are ignored.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import urlsplit

from cvboost import api_budget
from cvboost.connectors._util import salary_number
from cvboost.connectors.keyed import Call, KeyedAPIConnector, scrub_payload
from cvboost.models import EmploymentType, Job, RawPosting, SourceKind
from cvboost.textutil import canonical_url, html_to_text, utc_from_naive_iso

BASE = "https://api.adzuna.com/v1/api/jobs"
COUNTRIES = ("gb", "de", "nl", "es", "pt", "us")  # fr is French-language: add when wanted
QUERIES = (
    "analytics engineer",
    "data architect",
    "dbt",
    "bigquery",
    "looker",
    "marketing analytics",
    "data engineer",
)
CONTRACT_QUERIES = ("analytics engineer", "data engineer")  # also searched with contract=1
# #65 (tech_lead profile): appended at the END of the plan. The calls per day stay fixed
# (calls_per_run x runs), so these only lengthen the rotation: every query runs a bit less often.
TECH_LEAD_QUERIES = ("cto", "fractional cto", "founding engineer")
CURRENCY = {
    "gb": "GBP",
    "us": "USD",
    "de": "EUR",
    "nl": "EUR",
    "es": "EUR",
    "pt": "EUR",
    "fr": "EUR",
}


def _plan() -> list[Call]:
    calls = []
    for country in COUNTRIES:
        url = f"{BASE}/{country}/search/1"
        base = {"results_per_page": "50", "max_days_old": "14", "sort_by": "date"}
        for q in QUERIES:
            calls.append(Call(url, {**base, "what": q}))
        for q in CONTRACT_QUERIES:
            calls.append(Call(url, {**base, "what": q, "contract": "1"}))
        for q in TECH_LEAD_QUERIES:
            calls.append(Call(url, {**base, "what": q}))
    return calls


def _employment(p: dict[str, Any]) -> EmploymentType | None:
    if p.get("contract_type") == "contract":
        return "contract"
    time = p.get("contract_time")
    if time == "part_time":
        return "part_time"
    if time == "full_time":
        return "full_time"
    return None


class AdzunaConnector(KeyedAPIConnector):
    name: ClassVar[str] = "adzuna"
    kind: ClassVar[SourceKind] = "api"
    base_url: ClassVar[str] = BASE
    interval: ClassVar[timedelta] = timedelta(hours=6)
    min_delay_s: ClassVar[float] = 2.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"api.adzuna.com"})
    tos_note: ClassVar[str] = (
        "Free key (registration); plan limits are shown in the account, not published. "
        "Label each advert with Adzuna and link to it; no resale of the data; commercial use "
        "needs a licence after a 14-day trial. Personal job search is a permitted use."
    )
    attribution: ClassVar[str] = "Jobs by Adzuna"
    api: ClassVar[str] = "adzuna"
    label: ClassVar[str] = "Adzuna"
    env_vars: ClassVar[tuple[str, ...]] = ("ADZUNA_APP_ID", "ADZUNA_APP_KEY")
    default_limit: ClassVar[api_budget.Limit] = api_budget.Limit(per_day=30, per_month=800)
    calls_per_run: ClassVar[int] = 6

    def plan(self) -> list[Call]:
        return _plan()

    def auth(self, creds: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        return (
            {"app_id": creds["ADZUNA_APP_ID"], "app_key": creds["ADZUNA_APP_KEY"]},
            {"Accept": "application/json"},
        )

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        parts = urlsplit(url).path.strip("/").split("/")
        country = parts[parts.index("jobs") + 1] if "jobs" in parts[:-1] else ""
        secrets = self.secrets()
        out = []
        for item in json.loads(body).get("results", []):
            if item.get("id") is None:
                continue
            payload = scrub_payload(item, secrets) | {"_country": country}
            out.append(RawPosting(source=self.name, source_id=str(item["id"]), payload=payload))
        return out

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p = raw.payload
        predicted = str(p.get("salary_is_predicted", "0")) in ("1", "true", "True")
        lo = None if predicted else salary_number(p.get("salary_min"))
        hi = None if predicted else salary_number(p.get("salary_max"))
        country = str(p.get("_country") or "")
        loc = (p.get("location") or {}).get("display_name") or None
        category = (p.get("category") or {}).get("label")
        desc = p.get("description") or ""
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(p["redirect_url"]),
            company=(p.get("company") or {}).get("display_name") or "",
            title=p["title"],
            description_html=desc,
            description_text=html_to_text(desc),
            location_text=loc,
            employment_type=_employment(p),
            tags=[str(category)] if category else [],
            salary_min=lo,
            salary_max=hi,
            salary_currency=CURRENCY.get(country) if (lo or hi) else None,
            salary_period="year" if (lo or hi) else None,
            posted_at=utc_from_naive_iso(p["created"].replace("Z", "+00:00"))
            if p.get("created")
            else None,
            fetched_at=fetched_at,
            raw=p,
        )
