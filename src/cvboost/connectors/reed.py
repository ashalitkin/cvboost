"""Reed.co.uk Jobseeker API (UK; key required): https://www.reed.co.uk/developers/jobseeker.

`GET https://www.reed.co.uk/api/1.0/search?keywords=..&resultsToTake=100`, HTTP basic auth with the
key as the username and an empty password (REED_API_KEY). The key therefore travels in the
Authorization header, never in a URL. Reed publishes no rate limit and no terms for the API;
our own call budget keeps usage small (config/api_budget.yaml).
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from cvboost import api_budget
from cvboost.connectors._util import salary_number
from cvboost.connectors.keyed import Call, KeyedAPIConnector, scrub_payload
from cvboost.models import Job, RawPosting, SourceKind
from cvboost.textutil import canonical_url, html_to_text

URL = "https://www.reed.co.uk/api/1.0/search"
QUERIES = (
    "analytics engineer",
    "data architect",
    "dbt",
    "bigquery",
    "looker",
    "marketing analytics",
    "data engineer",
)
CONTRACT_QUERIES = ("analytics engineer", "data engineer", "bigquery")  # also with contract=true
# #65 (tech_lead profile): appended at the END of the plan. The calls per day stay fixed
# (calls_per_run x runs), so these only lengthen the rotation: every query runs a bit less often.
TECH_LEAD_QUERIES = ("cto", "fractional cto", "founding engineer")
YEARLY_FROM = 10000  # below this a figure may be a day or hour rate: period unknown


def _plan() -> list[Call]:
    base = {"resultsToTake": "100"}
    calls = [Call(URL, {**base, "keywords": q}) for q in QUERIES]
    calls += [Call(URL, {**base, "keywords": q, "contract": "true"}) for q in CONTRACT_QUERIES]
    calls += [Call(URL, {**base, "keywords": q}) for q in TECH_LEAD_QUERIES]
    return calls


def _date(value: object) -> datetime | None:
    """Reed dates are `dd/mm/yyyy`."""
    try:
        return datetime.strptime(str(value), "%d/%m/%Y").replace(tzinfo=UTC)
    except ValueError:
        return None


class ReedConnector(KeyedAPIConnector):
    name: ClassVar[str] = "reed"
    kind: ClassVar[SourceKind] = "api"
    base_url: ClassVar[str] = URL
    interval: ClassVar[timedelta] = timedelta(hours=6)
    min_delay_s: ClassVar[float] = 2.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"www.reed.co.uk"})
    tos_note: ClassVar[str] = (
        "Free key (reed.co.uk/developers); no rate limit or terms published for the Jobseeker "
        "API (the Recruiter API default is 2000 requests/hour). Link back to the Reed job page."
    )
    attribution: ClassVar[str] = "via Reed"
    api: ClassVar[str] = "reed"
    label: ClassVar[str] = "Reed"
    env_vars: ClassVar[tuple[str, ...]] = ("REED_API_KEY",)
    default_limit: ClassVar[api_budget.Limit] = api_budget.Limit(per_day=24, per_month=600)
    calls_per_run: ClassVar[int] = 4

    def plan(self) -> list[Call]:
        return _plan()

    def auth(self, creds: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        token = base64.b64encode(f"{creds['REED_API_KEY']}:".encode()).decode()
        return {}, {"Authorization": f"Basic {token}", "Accept": "application/json"}

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        secrets = self.secrets()
        data = json.loads(body)
        items = data.get("results", []) if isinstance(data, dict) else []
        return [
            RawPosting(
                source=self.name,
                source_id=str(item["jobId"]),
                payload=scrub_payload(item, secrets),
            )
            for item in items
            if item.get("jobId") is not None
        ]

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p = raw.payload
        lo, hi = salary_number(p.get("minimumSalary")), salary_number(p.get("maximumSalary"))
        top = hi or lo
        yearly = top is not None and top >= YEARLY_FROM
        cur = str(p.get("currency") or "GBP")
        sym = {"GBP": "£", "USD": "$", "EUR": "€"}.get(cur.upper(), cur + " ")
        desc = p.get("jobDescription") or ""
        url = p.get("jobUrl") or f"https://www.reed.co.uk/jobs/{raw.source_id}"
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(url),
            company=p.get("employerName") or "",
            title=p["jobTitle"],
            description_html=desc,
            description_text=html_to_text(desc),
            location_text=p.get("locationName") or None,
            salary_min=lo if yearly else None,
            salary_max=hi if yearly else None,
            salary_currency=cur.upper() if yearly else None,
            salary_period="year" if yearly else None,
            # a small figure may be a day / hour rate: keep it as text, the rules read it
            salary_text=None if yearly or top is None else f"{sym}{lo or hi:g}-{sym}{top:g}",
            posted_at=_date(p.get("date")),
            fetched_at=fetched_at,
            raw=p,
        )
