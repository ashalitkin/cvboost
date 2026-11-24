"""Hacker News "Ask HN: Who is hiring?" via the Algolia HN API.

Two steps: find the current monthly thread, then read its comments. Each top-level comment is
one posting; the first line is `Company | Role | Location | ...` by convention. Comments are
untrusted public text: nothing from them becomes an apply link, a line without `|` is invalid.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from cvboost.connectors._util import utc_from_epoch
from cvboost.connectors.base import Connector, FetchOutcome
from cvboost.http import HttpClient
from cvboost.models import EmploymentType, Job, RawPosting, SourceKind
from cvboost.textutil import html_to_text

log = logging.getLogger("cvboost.hn_hiring")

API = "https://hn.algolia.com/api/v1/search_by_date"
THREAD_TITLE = "Ask HN: Who is hiring?"
MAX_THREAD_AGE = timedelta(days=40)
MAX_COMMENT_PAGES = 3
MAX_FIRST_LINE = 300
MAX_COMPANY = 80

_I = re.IGNORECASE
ROLE = re.compile(
    r"engineer|developer|scientist|analyst|manager|designer|architect|\blead\w*|head of|director|"
    r"\b(?:sre|cto|coo|cfo)\b|devops|founder|recruiter|consultant|specialist|researcher",
    _I,
)
TYPE = re.compile(
    r"\bfull[- ]?time\b|\bpart[- ]?time\b|\bcontract(?:or)?\b|\bfreelance\b|\bintern(?:ship|s)?\b",
    _I,
)
SALARY = re.compile(r"[$€£]\s?\d|\b\d{2,3}\s?k\b|salary|equity", _I)
URL = re.compile(r"https?://|www\.|\.[a-z]{2,6}(/|$)", _I)
VISA = re.compile(r"\bvisa\b", _I)
LOCATION = re.compile(
    r"remote|onsite|on-site|hybrid|in[- ]office|worldwide|anywhere|\b(us|usa|eu|uk|emea|europe)\b",
    _I,
)
CITY = re.compile(r"^[A-Z][\w .'-]+,\s*[A-Z]")
NOT_REMOTE = re.compile(r"no remote|not remote", _I)
ONSITE = re.compile(r"onsite|on-site|in[- ]office", _I)


def _employment_type(text: str) -> EmploymentType:
    m = TYPE.search(text)
    word = (m.group(0) if m else "").lower().replace("-", "").replace(" ", "")
    if word.startswith("full"):
        return "full_time"
    if word.startswith("part"):
        return "part_time"
    if word.startswith("contract"):
        return "contract"
    if word.startswith("freelance"):
        return "freelance"
    if word.startswith("intern"):
        return "internship"
    return "other"


def first_line(comment_html: str) -> str:
    head = comment_html.split("<p>", 1)[0]
    text = html_to_text(head).split("\n", 1)[0]
    return text.strip()[:MAX_FIRST_LINE]


def parse_first_line(line: str) -> dict[str, Any]:
    """Heuristic fields from `Company | Role | Location | ...`; ValueError when not that shape."""
    if "|" not in line:
        raise ValueError(f"first line has no '|': {line[:80]!r}")
    parts = [p.strip() for p in line.split("|") if p.strip()]
    if len(parts) < 2:
        raise ValueError(f"first line has fewer than two parts: {line[:80]!r}")
    if len(parts[0]) > MAX_COMPANY:
        raise ValueError("first part is too long to be a company name")
    roles: list[str] = []
    others: list[str] = []
    types: list[str] = []
    salaries: list[str] = []
    locations: list[str] = []
    for part in parts[1:]:
        if ROLE.search(part):
            roles.append(part)
        elif TYPE.search(part):
            types.append(part)
        elif SALARY.search(part):
            salaries.append(part)
        elif URL.search(part) or VISA.search(part):
            pass
        elif LOCATION.search(part) or CITY.search(part):
            locations.append(part)
        else:
            others.append(part)
    low = line.lower()
    if "remote" in low and not NOT_REMOTE.search(low):
        remote: bool | None = True
    elif ONSITE.search(low) and "remote" not in low:
        remote = False
    else:
        remote = None
    return {
        "company": parts[0],
        "title": (roles or others or [line])[0],
        "location_text": " | ".join(locations) or None,
        "remote": remote,
        "employment_type": _employment_type(types[0]) if types else None,
        "salary_text": " | ".join(salaries) or None,
    }


class HNHiringConnector(Connector):
    name: ClassVar[str] = "hn_hiring"
    kind: ClassVar[SourceKind] = "api"
    base_url: ClassVar[str] = f"{API}?tags=story,author_whoishiring&hitsPerPage=10"
    interval: ClassVar[timedelta] = timedelta(hours=3)
    min_delay_s: ClassVar[float] = 2.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"hn.algolia.com"})
    tos_note: ClassVar[str] = (
        "Algolia HN Search API, public, no key; no published limit found (stay far below 10k/h "
        "per IP); no robots.txt. Link to the HN comment. Company/title/location come from the "
        "first line heuristically; apply_url is never taken from comment text."
    )
    attribution: ClassVar[str] = "via Hacker News (Who is hiring)"

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self._now = now

    def requests(self) -> list[str]:
        return [self.base_url]

    def comments_url(self, story_id: str, page: int) -> str:
        return f"{API}?tags=comment,story_{story_id}&hitsPerPage=1000&page={page}"

    def pick_thread(self, search_body: bytes) -> str | None:
        """objectID of the newest 'Who is hiring?' story, or None when missing or too old."""
        for hit in json.loads(search_body).get("hits", []):
            if str(hit.get("title") or "").startswith(THREAD_TITLE):
                created = utc_from_epoch(hit.get("created_at_i"))
                if created is None or self._now() - created > MAX_THREAD_AGE:
                    log.warning("hn_hiring: newest thread %s is too old", hit.get("objectID"))
                    return None
                return str(hit["objectID"])
        log.warning("hn_hiring: no 'Who is hiring?' thread in the search result")
        return None

    def fetch_partial(self, http: HttpClient, limit: int | None = None) -> FetchOutcome:
        resp = http.get(
            self.base_url, min_delay_s=self.min_delay_s, allowed_hosts=self.allowed_hosts
        )
        story_id = self.pick_thread(resp.content)
        if story_id is None:
            return FetchOutcome([])
        seen: set[str] = set()
        out: list[RawPosting] = []
        for page in range(MAX_COMMENT_PAGES):
            url = self.comments_url(story_id, page)
            resp = http.get(url, min_delay_s=self.min_delay_s, allowed_hosts=self.allowed_hosts)
            for raw in self.parse(resp.content, url):
                if raw.source_id not in seen:
                    seen.add(raw.source_id)
                    out.append(raw)
            if limit is not None and len(out) >= limit:
                break
            if page + 1 >= json.loads(resp.content).get("nbPages", 1):
                break
        return FetchOutcome(out[:limit] if limit is not None else out)

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        out = []
        for hit in json.loads(body).get("hits", []):
            story_id, parent_id = hit.get("story_id"), hit.get("parent_id")
            if story_id is None or parent_id != story_id:
                continue  # a reply, not a posting
            if not (hit.get("comment_text") or "").strip() or not hit.get("objectID"):
                continue  # deleted
            out.append(RawPosting(source=self.name, source_id=str(hit["objectID"]), payload=hit))
        return out

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p = raw.payload
        html = p["comment_text"]
        fields = parse_first_line(first_line(html))
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=f"https://news.ycombinator.com/item?id={raw.source_id}",
            apply_url=None,
            description_html=html,
            description_text=html_to_text(html),
            tags=[],
            posted_at=utc_from_epoch(p.get("created_at_i")),
            fetched_at=fetched_at,
            raw=p,
            **fields,
        )
