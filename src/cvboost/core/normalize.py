"""Normalised company / title keys for cross-source dedupe."""

from __future__ import annotations

import re
import unicodedata

from cvboost.models import UNDISCLOSED_COMPANY

_COMPANY_SUFFIXES = frozenset(
    "inc incorporated llc ltd limited gmbh ag sa sas srl sl spa bv ab oy as aps plc corp "
    "corporation co lda pty pte kft".split()
)
_DOMAIN_TAILS = frozenset({"io", "com", "ai", "app"})
_GENDER = re.compile(r"\((?:m|f|w|h|d|x)(?:\s*/\s*(?:m|f|w|h|d|x)){1,2}\)", re.I)
_TICKET = re.compile(r"\(?\b[A-Z]{1,3}-?\d{4,}\)?")
_NOISE_WORDS = re.compile(r"\b(?:remote|contract|contractor|freelance|full time|part time)\b")
_YC_BATCH = re.compile(r"\(\s*yc\s*[a-z]\d{2}\s*\)", re.I)
_NON_WORD = re.compile(r"[^\w]+")


def _base(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return s


def _words(s: str) -> str:
    return re.sub(r"\s+", " ", _NON_WORD.sub(" ", _base(s)).replace("_", " ")).strip()


def company_norm(s: str) -> str:
    s = _YC_BATCH.sub(" ", s)
    tokens = [t for t in _words(s).split() if t not in _COMPANY_SUFFIXES]
    if len(tokens) > 1 and tokens[-1] in _DOMAIN_TAILS:
        tokens.pop()
    return " ".join(tokens)


def title_norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = _TICKET.sub(" ", s)  # case-sensitive, before casefold
    s = _GENDER.sub(" ", s)
    s = _words(s)
    s = re.sub(r"\b(?:sr|snr)\b", "senior", s)
    s = re.sub(r"\bjr\b", "junior", s)
    s = _NOISE_WORDS.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


_NO_KEY = frozenset(
    company_norm(c)
    for c in (UNDISCLOSED_COMPANY, "Confidential", "Undisclosed", "Not disclosed", "Anonymous")
)


def has_key(norm: str) -> bool:
    """A normalised company/title usable as a dedupe key (not too short, not the placeholder)."""
    return len(norm) >= 2 and norm not in _NO_KEY
