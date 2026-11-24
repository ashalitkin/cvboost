"""Greenhouse Job Board API: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true

Public, unauthenticated GET (https://docs.greenhouse.io/job-board.html). One request returns all
open jobs of a company with their descriptions (HTML-escaped in `content`).
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any, ClassVar

from cvboost.connectors._util import http_url_or_none
from cvboost.connectors.ats import ATSConnector
from cvboost.models import Job, RawPosting, SourceKind
from cvboost.textutil import canonical_url, html_to_text, utc_from_naive_iso


class GreenhouseConnector(ATSConnector):
    name: ClassVar[str] = "greenhouse"
    kind: ClassVar[SourceKind] = "ats"
    base_url: ClassVar[str] = "https://boards-api.greenhouse.io/v1/boards"
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"boards-api.greenhouse.io"})
    tos_note: ClassVar[str] = (
        "Official public Job Board API, GET without authentication (docs.greenhouse.io/"
        "job-board.html). The company list is ours (config/companies.yaml); 1 s between "
        "companies, each company at most every 6 h, 404 = company removed from the polling."
    )
    attribution: ClassVar[str] = "via company careers page (Greenhouse)"

    def company_url(self, slug: str) -> str:
        return f"{self.base_url}/{slug}/jobs?content=true"

    def postings(self, data: Any) -> list[dict[str, Any]]:
        jobs = data["jobs"]
        if not isinstance(jobs, list):
            raise ValueError("jobs is not a list")
        return [j for j in jobs if isinstance(j, dict)]

    def posting_id(self, item: dict[str, Any]) -> str | None:
        return str(item["id"]) if item.get("id") is not None else None

    def posting_title(self, item: dict[str, Any]) -> object:
        return item.get("title")

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p: dict[str, Any] = raw.payload
        content = html.unescape(p.get("content") or "")
        location = ((p.get("location") or {}).get("name") or "").strip() or None
        posted = p.get("first_published") or p.get("updated_at")
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(p["absolute_url"]),
            apply_url=http_url_or_none(p["absolute_url"]),
            company=(p.get("company_name") or p["_company_name"]),
            title=p["title"].strip(),
            description_html=content,
            description_text=html_to_text(content),
            location_text=location,
            remote=True if location and "remote" in location.lower() else None,
            tags=[
                str(d["name"]).strip()
                for d in p.get("departments") or []
                if isinstance(d, dict) and d.get("name")
            ],
            posted_at=utc_from_naive_iso(posted) if posted else None,
            fetched_at=fetched_at,
            raw=p,
        )
