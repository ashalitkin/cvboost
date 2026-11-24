"""Mastodon hashtag RSS feeds (#fedihire, #hiring, #dataanalytics).

Posts have no title and no company. Only posts that read like a job offer are kept (job words
in, job-seeker phrases out), before anything is stored. Post text is untrusted public text:
no link from it becomes an apply link.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import ClassVar
from urllib.parse import urlsplit

from cvboost.connectors._util import rss_items
from cvboost.connectors.base import Connector
from cvboost.models import EmploymentType, Job, RawPosting, SourceKind
from cvboost.textutil import canonical_url, html_to_text, parse_rfc822

FEEDS = [
    "https://mastodon.social/tags/fedihire.rss",
    "https://mastodon.social/tags/hiring.rss",
    "https://mastodon.social/tags/dataanalytics.rss",
]
MAX_TITLE = 120
_I = re.IGNORECASE
JOB_WORDS = re.compile(
    r"\b(?:hiring|jobs?|vacanc(?:y|ies)|contract(?:or)?|freelanc\w*|(?:open )?positions?|"
    r"openings?|recruit\w*|we(?:'re| are) looking for|looking to hire)\b",
    _I,
)
SEEKER = re.compile(
    r"opentowork|\b(?:available|open) (?:for|to) (?:hire|work)\b|"
    r"\bi(?:'m|’m| am) (?:currently )?(?:looking for|seeking)\b|"
    r"\blooking for (?:work|a (?:new )?(?:job|role|position|gig))\b",
    _I,
)
_HASHTAG = re.compile(r"#\w+")
_URL = re.compile(r"https?://\S{1,500}")
_CONTRACT = re.compile(r"\bcontract(?:or)?\b", _I)
_FREELANCE = re.compile(r"\bfreelanc\w*", _I)
_REMOTE = re.compile(r"\bremote\b", _I)
_ID = re.compile(r"^/@([\w.-]{1,60})/(\d{1,25})$")


def looks_like_job_post(text: str) -> bool:
    """Job words outside hashtags and links (a tag or /jobs/ URL proves nothing), not a seeker."""
    body = _HASHTAG.sub(" ", _URL.sub(" ", text))
    return bool(JOB_WORDS.search(body)) and not SEEKER.search(text)


class MastodonConnector(Connector):
    name: ClassVar[str] = "mastodon"
    kind: ClassVar[SourceKind] = "rss"
    base_url: ClassVar[str] = "https://mastodon.social/tags"
    interval: ClassVar[timedelta] = timedelta(hours=3)
    min_delay_s: ClassVar[float] = 5.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"mastodon.social", "fosstodon.org"})
    tos_note: ClassVar[str] = (
        "Public hashtag RSS (Mastodon feature). robots.txt of both hosts blocks only "
        "/media_proxy/, /interact/, /api/v1/instance/domain_blocks for generic agents; "
        "mastodon.social forbids LLM training use and blocks AI-training crawlers by name "
        "(ClaudeBot, anthropic-ai ...). We use a generic User-Agent and only classify posts, "
        "never train on them. Posts are kept only if they look like job offers."
    )
    attribution: ClassVar[str] = "via Mastodon"
    # off until the owner confirms "Help improve Claude" is OFF (posts go to Haiku); low value
    default_enabled: ClassVar[bool] = False

    def requests(self) -> list[str]:
        return list(FEEDS)

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        out = []
        for fields in rss_items(body):
            link = fields.get("link", "")
            parts = urlsplit(link)
            m = _ID.match(parts.path)
            if not m or parts.scheme != "https" or not parts.hostname:
                continue
            if not looks_like_job_post(html_to_text(fields.get("description", ""))):
                continue
            out.append(
                RawPosting(source=self.name, source_id=f"{parts.hostname}:{m[2]}", payload=fields)
            )
        return out

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p = raw.payload
        parts = urlsplit(p["link"])
        user = _ID.match(parts.path)[1]  # type: ignore[index]
        html = p.get("description") or ""
        text = html_to_text(html)
        first = next((ln.strip() for ln in text.split("\n") if ln.strip()), "")
        etype: EmploymentType | None = None
        if _FREELANCE.search(text):
            etype = "freelance"
        elif _CONTRACT.search(text):
            etype = "contract"
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(p["link"]),
            company=f"@{user}@{parts.hostname}",
            title=first[:MAX_TITLE] or "Mastodon post",
            description_html=html,
            description_text=text,
            remote=True if _REMOTE.search(text) else None,
            employment_type=etype,
            tags=[c for c in p.get("category", "").split("\n") if c],
            posted_at=parse_rfc822(p["pubDate"]) if p.get("pubDate") else None,
            fetched_at=fetched_at,
            raw=p,
        )
