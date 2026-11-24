"""Sanitising untrusted text (job postings, model output) before it goes into a prompt."""

from __future__ import annotations

import html
import re
import unicodedata

from cvboost.textutil import clean_text

_NL3 = re.compile(r"\n{3,}")


def strip_invisible(s: str) -> str:
    """Drop control, zero-width, bidi, tag (U+E0000-E007F) and every other Unicode `Cf` char."""
    s = clean_text(s)
    return "".join(ch for ch in s if unicodedata.category(ch) != "Cf")


def normalize_text(s: str | None) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = strip_invisible(s.replace("\r\n", "\n").replace("\r", "\n"))
    return _NL3.sub("\n\n", s).strip()


def truncate(text: str, max_chars: int = 6000, head: int = 4500, tail: int = 1500) -> str:
    """Keep head + tail: work-authorization / contractor conditions often sit at the end."""
    if len(text) <= max_chars:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n[... {omitted} characters omitted ...]\n{text[-tail:]}"


def sanitize_field(s: str | None, max_chars: int) -> str:
    """Normalise, cut to max_chars and escape `& < >` (so `</job>` becomes `&lt;/job&gt;`)."""
    return html.escape(normalize_text(s)[:max_chars], quote=False)


def sanitize_description(s: str | None, max_chars: int, head: int, tail: int) -> str:
    return html.escape(truncate(normalize_text(s), max_chars, head, tail), quote=False)
