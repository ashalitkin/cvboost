"""Arbeitnow job board API: https://www.arbeitnow.com/api/job-board-api (JSON, page 1 only)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, ClassVar

from cvboost.connectors._util import utc_from_epoch
from cvboost.connectors.base import Connector
from cvboost.models import EmploymentType, Job, RawPosting, SourceKind
from cvboost.textutil import canonical_url, html_to_text

# order matters: the first matching rule wins; `(?<!un)` keeps 'unbefristet' (permanent) out of
# the fixed-term rule
TYPE_RULES: list[tuple[re.Pattern[str], EmploymentType]] = [
    (re.compile(r"freelance|freelancer|selbstständig|indépendant"), "freelance"),
    (re.compile(r"permanent|(?<!\w)unbefristet"), "full_time"),  # before contract
    (re.compile(r"contract|(?<!un)befristet|interim|temporary"), "contract"),
    (re.compile(r"intern|praktikum|stage|werkstudent|apprentice"), "internship"),
    (re.compile(r"part-time|parttime|teilzeit|minijob"), "part_time"),
    (re.compile(r"full|vollzeit|unbefristet|cdi|permanent"), "full_time"),
]


def _employment_type(values: object) -> EmploymentType | None:
    items = [str(v).strip().lower() for v in values or [] if str(v).strip()]  # type: ignore[union-attr]
    if not items:
        return None
    for rx, etype in TYPE_RULES:
        if any(rx.search(i) for i in items):
            return etype
    return "other"


class ArbeitnowConnector(Connector):
    name: ClassVar[str] = "arbeitnow"
    kind: ClassVar[SourceKind] = "api"
    base_url: ClassVar[str] = "https://www.arbeitnow.com/api/job-board-api"
    interval: ClassVar[timedelta] = timedelta(hours=6)
    min_delay_s: ClassVar[float] = 5.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"www.arbeitnow.com"})
    tos_note: ClassVar[str] = (
        "Free API, rate limit 50 (x-ratelimit-limit); 'please do not abuse'; link back to "
        "Arbeitnow appreciated. Page 1 only (about the last 23 h, 2+ MB); links.next is ignored."
    )
    attribution: ClassVar[str] = "via Arbeitnow"
    max_pages = 1

    def requests(self) -> list[str]:
        return [self.base_url]

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        data = json.loads(body)
        return [
            RawPosting(source=self.name, source_id=str(item["slug"]), payload=item)
            for item in data.get("data", [])
            if item.get("slug")
        ]

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p: dict[str, Any] = raw.payload
        html = p.get("description") or ""
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(p["url"]),
            company=p["company_name"],
            title=p["title"],
            description_html=html,
            description_text=html_to_text(html),
            location_text=(p.get("location") or None),
            remote=bool(p["remote"]) if p.get("remote") is not None else None,
            employment_type=_employment_type(p.get("job_types")),
            tags=[str(t).strip() for t in p.get("tags") or []],
            posted_at=utc_from_epoch(p.get("created_at")),
            fetched_at=fetched_at,
            raw=p,
        )
