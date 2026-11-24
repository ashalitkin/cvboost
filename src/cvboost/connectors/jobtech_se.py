"""JobTech / Arbetsförmedlingen (Sweden): JobSearch JSON API (open data, no key)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import quote

from cvboost.connectors._util import http_url_or_none
from cvboost.connectors.base import Connector
from cvboost.models import EmploymentType, Job, RawPosting, SourceKind
from cvboost.textutil import html_to_text, utc_from_naive_iso

QUERIES = ["data analyst", "data engineer"]
LIMIT = 100


def _label(value: object) -> str:
    return str(value.get("label") or "").lower() if isinstance(value, dict) else ""


def _employment_type(p: dict[str, Any]) -> EmploymentType | None:
    kind, hours = _label(p.get("employment_type")), _label(p.get("working_hours_type"))
    if "frilans" in kind or "egenföretag" in kind:
        return "freelance"
    if "praktik" in kind:
        return "internship"
    if hours == "deltid":
        return "part_time"
    if hours == "heltid":
        return "full_time"
    return None


class JobTechSEConnector(Connector):
    name: ClassVar[str] = "jobtech_se"
    kind: ClassVar[SourceKind] = "api"
    base_url: ClassVar[str] = "https://jobsearch.api.jobtechdev.se/search"
    interval: ClassVar[timedelta] = timedelta(hours=6)
    min_delay_s: ClassVar[float] = 5.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"jobsearch.api.jobtechdev.se"})
    tos_note: ClassVar[str] = (
        "Public open-data API of Arbetsförmedlingen, no key; open data, free for anyone (see "
        "data.arbetsformedlingen.se). No robots.txt (404), no separate terms page found. Link to "
        "the Platsbanken ad and name Arbetsförmedlingen."
    )
    attribution: ClassVar[str] = "via Arbetsförmedlingen (Platsbanken)"

    def requests(self) -> list[str]:
        return [f"{self.base_url}?q={quote(q)}&limit={LIMIT}" for q in QUERIES]

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        out = []
        for hit in json.loads(body).get("hits", []):
            if hit.get("removed") or not hit.get("id") or not hit.get("webpage_url"):
                continue
            out.append(RawPosting(source=self.name, source_id=str(hit["id"]), payload=hit))
        return out

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p = raw.payload
        desc = p.get("description") or {}
        html = desc.get("text_formatted") or ""
        addr = p.get("workplace_address") or {}
        place = [addr.get("municipality"), addr.get("region"), addr.get("country")]
        employer = p.get("employer") or {}
        company = employer.get("name") or employer.get("workplace")
        if not company:
            raise ValueError("no employer name")
        mode = _label(p.get("workplace_model"))
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=p["webpage_url"],
            apply_url=http_url_or_none((p.get("application_details") or {}).get("url")),
            company=company,
            title=p["headline"],
            description_html=html,
            description_text=html_to_text(html) or desc.get("text") or "",
            location_text=", ".join(x for x in place if x) or None,
            remote=True if ("distans" in mode or "remote" in mode) else None,
            employment_type=_employment_type(p),
            tags=[x for x in (_label(p.get("occupation")),) if x],
            salary_text=p.get("salary_description") or None,
            posted_at=utc_from_naive_iso(p.get("publication_date") or ""),
            fetched_at=fetched_at,
            raw=p,
        )
