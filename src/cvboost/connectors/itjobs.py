"""ITJobs.pt API (Portugal IT jobs; key required): https://www.itjobs.pt/api/docs (issue #19).

`GET https://api.itjobs.pt/job/search.json?api_key=..&q=..&limit=..` (ITJOBS_API_KEY, a
read-only key issued on request by e-mail). No rate limit or terms are published.
Salaries are annual gross EUR. `workModel`: 0 on-site, 1 remote, 2 hybrid.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, ClassVar

from cvboost import api_budget
from cvboost.connectors._util import salary_number
from cvboost.connectors.keyed import Call, KeyedAPIConnector, scrub_payload
from cvboost.models import EmploymentType, Job, RawPosting, SourceKind
from cvboost.textutil import canonical_url, html_to_text, utc_from_naive_iso

URL = "https://api.itjobs.pt/job/search.json"
QUERIES = (
    "analytics engineer",
    "data architect",
    "dbt",
    "bigquery",
    "looker",
    "marketing analytics",
    "data engineer",
    # Portugal market overview (owner, 2026-11-10): broad queries, mostly stored for analysis;
    # the rules still reject what does not fit, so no extra Haiku calls.
    "dados",
    "analista de dados",
    "engenheiro de dados",
    "business intelligence",
    "power bi",
    "data analyst",
    "data scientist",
)


# #65 (tech_lead profile): appended at the END of the plan. The calls per day stay fixed
# (calls_per_run x runs), so these only lengthen the rotation: every query runs a bit less often.
TECH_LEAD_QUERIES = ("cto", "fractional cto", "founding engineer")


def _plan() -> list[Call]:
    base = {"limit": "50"}
    calls = [Call(URL, {**base, "q": q}) for q in QUERIES]
    calls.append(Call(URL, {**base, "q": "data", "type": "2"}))  # part-time
    calls += [Call(URL, {**base, "q": q}) for q in TECH_LEAD_QUERIES]
    return calls


def _names(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    return [str(i["name"]) for i in items if isinstance(i, dict) and i.get("name")]


def _employment(p: dict[str, Any]) -> EmploymentType | None:
    types = " ".join(_names(p.get("types"))).lower()
    contracts = " ".join(_names(p.get("contracts"))).lower()
    if "freelanc" in contracts or "prestação" in contracts or "prestacao" in contracts:
        return "freelance"
    if "part" in types:
        return "part_time"
    if "full" in types:
        return "full_time"
    return None


class ITJobsConnector(KeyedAPIConnector):
    name: ClassVar[str] = "itjobs"
    kind: ClassVar[SourceKind] = "api"
    base_url: ClassVar[str] = URL
    interval: ClassVar[timedelta] = timedelta(hours=6)
    min_delay_s: ClassVar[float] = 2.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"api.itjobs.pt"})
    tos_note: ClassVar[str] = (
        "Documented public API with a free read-only key (itjobs.pt/api); no rate limit, terms "
        "or attribution rule published (questions: itjobs@itjobs.pt). Link back to ITJobs.pt."
    )
    attribution: ClassVar[str] = "via ITJobs.pt"
    api: ClassVar[str] = "itjobs"
    label: ClassVar[str] = "ITJobs.pt"
    env_vars: ClassVar[tuple[str, ...]] = ("ITJOBS_API_KEY",)
    default_limit: ClassVar[api_budget.Limit] = api_budget.Limit(per_day=24, per_month=600)
    calls_per_run: ClassVar[int] = 4

    def plan(self) -> list[Call]:
        return _plan()

    def auth(self, creds: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        return {"api_key": creds["ITJOBS_API_KEY"]}, {"Accept": "application/json"}

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        secrets = self.secrets()
        data = json.loads(body)
        items = data.get("results", []) if isinstance(data, dict) else []
        return [
            RawPosting(
                source=self.name, source_id=str(item["id"]), payload=scrub_payload(item, secrets)
            )
            for item in items
            if item.get("id") is not None
        ]

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p = raw.payload
        lo, hi = salary_number(p.get("salaryMin")), salary_number(p.get("salaryMax"))
        work_model = p.get("workModel")
        slug = p.get("slug") or ""
        html = p.get("body") or ""
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(f"https://www.itjobs.pt/oferta/{raw.source_id}/{slug}".rstrip("/")),
            company=(p.get("company") or {}).get("name") or "",
            title=p["title"],
            description_html=html,
            description_text=html_to_text(html),
            location_text=", ".join(_names(p.get("locations"))) or None,
            remote=True if work_model in (1, "1") else None,
            employment_type=_employment(p),
            salary_min=lo,
            salary_max=hi,
            salary_currency="EUR" if (lo or hi) else None,
            salary_period="year" if (lo or hi) else None,
            posted_at=utc_from_naive_iso(p["publishedAt"]) if p.get("publishedAt") else None,
            fetched_at=fetched_at,
            raw=p,
        )
