"""Working Nomads public JSON feed: https://www.workingnomads.com/api/exposed_jobs/."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, ClassVar

from cvboost.connectors.base import Connector
from cvboost.models import Job, RawPosting, SourceKind
from cvboost.textutil import canonical_url, html_to_text, utc_from_naive_iso

_ID = re.compile(r"/job/go/(\d+)/")


class WorkingNomadsConnector(Connector):
    name: ClassVar[str] = "workingnomads"
    kind: ClassVar[SourceKind] = "api"
    base_url: ClassVar[str] = "https://www.workingnomads.com/api/exposed_jobs/"
    interval: ClassVar[timedelta] = timedelta(hours=6)
    min_delay_s: ClassVar[float] = 5.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"www.workingnomads.com"})
    tos_note: ClassVar[str] = (
        "Public feed linked from the site footer; ToS do not mention the API and forbid building "
        "a competing product (personal tool). robots.txt allows everything. Name Working Nomads "
        "and link to the job."
    )
    attribution: ClassVar[str] = "via Working Nomads"

    def requests(self) -> list[str]:
        return [self.base_url]

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        data = json.loads(body)
        out = []
        for item in data if isinstance(data, list) else []:
            link = item.get("url")
            if not link:
                continue
            m = _ID.search(link)
            out.append(
                RawPosting(source=self.name, source_id=m.group(1) if m else link, payload=item)
            )
        return out

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p: dict[str, Any] = raw.payload
        html = p.get("description") or ""
        tags = [str(p["category_name"]).strip()] if p.get("category_name") else []
        tags += [t.strip() for t in str(p.get("tags") or "").split(",") if t.strip()]
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(p["url"]),
            company=p["company_name"],
            title=p["title"],
            description_html=html,
            description_text=html_to_text(html),
            location_text=(p.get("location") or None),
            remote=True,
            tags=tags,
            posted_at=utc_from_naive_iso(p["pub_date"]) if p.get("pub_date") else None,
            fetched_at=fetched_at,
            raw=p,
        )
