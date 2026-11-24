"""Hacker News Jobs (YC companies' own job posts) via the Firebase API.

Two steps: the id list `jobstories.json`, then `item/{id}.json` per id (at most MAX_ITEMS).
Titles are free text, usually `Company (YC S17) is hiring <role>`; nothing from the text is
trusted beyond title/company display.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import ClassVar
from urllib.parse import urlsplit

from cvboost.connectors._util import http_url_or_none, utc_from_epoch
from cvboost.connectors.base import Connector, FetchOutcome
from cvboost.http import HttpClient
from cvboost.models import Job, RawPosting, SourceKind
from cvboost.textutil import html_to_text

API = "https://hacker-news.firebaseio.com/v0"
MAX_ITEMS = 40
MAX_TITLE = 300
_HIRING = re.compile(
    r"^(?P<company>.{1,100}?)\s+(?:\((?:YC|Y Combinator)[^)]{0,20}\)\s+)?"
    r"(?:is|are)\s+hiring\b[:\s-]*(?P<title>.*)$",
    re.IGNORECASE,
)
_YC_SLUG = re.compile(r"^/companies/([\w-]{1,80})/")


class HNJobsConnector(Connector):
    name: ClassVar[str] = "hn_jobs"
    kind: ClassVar[SourceKind] = "api"
    base_url: ClassVar[str] = f"{API}/jobstories.json"
    interval: ClassVar[timedelta] = timedelta(hours=6)
    min_delay_s: ClassVar[float] = 1.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"hacker-news.firebaseio.com"})
    tos_note: ClassVar[str] = (
        "Official HN Firebase API, public; README: 'There is currently no rate limit' (we wait "
        "1 s between calls, <= 41 calls per run). robots.txt allows `/*.json`. Link to the HN "
        "item; the company's own URL is kept as apply link."
    )
    attribution: ClassVar[str] = "via Hacker News (Jobs)"

    def requests(self) -> list[str]:
        return [self.base_url]

    def fetch_partial(self, http: HttpClient, limit: int | None = None) -> FetchOutcome:
        kw = {"min_delay_s": self.min_delay_s, "allowed_hosts": self.allowed_hosts}
        ids = json.loads(http.get(self.base_url, **kw).content)
        if not isinstance(ids, list):
            raise ValueError("jobstories is not a list")
        cap = MAX_ITEMS if limit is None else min(limit, MAX_ITEMS)
        out: list[RawPosting] = []
        for item_id in [i for i in ids if isinstance(i, int)][:cap]:
            url = f"{API}/item/{item_id}.json"
            out.extend(self.parse(http.get(url, **kw).content, url))
        return FetchOutcome(out, [], 1)

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        item = json.loads(body)
        if (
            not isinstance(item, dict)
            or item.get("type") != "job"
            or item.get("dead")
            or item.get("deleted")
            or not isinstance(item.get("id"), int)
            or not str(item.get("title") or "").strip()
        ):
            return []
        return [RawPosting(source=self.name, source_id=str(item["id"]), payload=item)]

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p = raw.payload
        full = str(p["title"]).strip()[:MAX_TITLE]
        ext = http_url_or_none(p.get("url"))
        m = _HIRING.match(full)
        if m:
            company, title = m["company"].strip(), m["title"].strip() or full
        else:
            company, title = "", full
            if ext:
                parts = urlsplit(ext)
                slug = _YC_SLUG.match(parts.path)
                company = slug[1] if slug else (parts.hostname or "").removeprefix("www.")
        if not company:
            raise ValueError(f"cannot tell the company from {full!r}")
        html = p.get("text") or ""
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=f"https://news.ycombinator.com/item?id={raw.source_id}",
            apply_url=ext,
            company=company,
            title=title,
            description_html=html,
            description_text=html_to_text(html) or full,
            posted_at=utc_from_epoch(p.get("time")),
            fetched_at=fetched_at,
            raw=p,
        )
