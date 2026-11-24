"""Small text and time helpers used by connectors."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser


def html_to_text(html: str) -> str:
    if not html:
        return ""
    # newline after block-level boundaries so inline tags do not split words
    html = re.sub(r"(?i)(<br\s*/?>|</(p|div|li|ul|ol|h[1-6]|tr|table|blockquote)>)", r"\1\n", html)
    tree = HTMLParser(html)
    for node in tree.css("script, style"):
        node.decompose()
    body = tree.body
    text = body.text(separator="") if body is not None else tree.text(separator="")
    lines = (re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_rfc822(value: str) -> datetime | None:
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def utc_from_naive_iso(value: str) -> datetime | None:
    """Parse an ISO timestamp; naive values are taken as UTC."""
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def canonical_url(url: str, *, strip_utm: bool = True) -> str:
    """Lower-case the host, drop the fragment and `utm_*` query parameters.

    Only for `url` and dedupe keys; for `apply_url` pass `strip_utm=False` (it must stay as given).
    """
    parts = urlsplit(url.strip())
    query = parts.query
    if query and strip_utm:
        kept = [p for p in query.split("&") if not p.split("=", 1)[0].lower().startswith("utm_")]
        query = "&".join(kept)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))


_CONTROL = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f"  # control chars except \t and \n
    "\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"  # zero-width, bidi
)


def clean_text(value: str | None) -> str:
    """Drop control, zero-width and bidi-override characters from untrusted text."""
    return _CONTROL.sub("", value or "")
