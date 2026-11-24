"""Types shared by the dispatcher, the platform parsers and external handlers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

from cvboost.models import EmploymentType


@dataclass
class ParsedJob:
    """One posting found in an e-mail. `url` is always rebuilt by us, never taken from the mail
    as is; `source_id` is the platform's job id (or a stable hash when the mail has none)."""

    platform: str
    source_id: str
    url: str
    title: str
    company: str
    location: str | None = None
    salary_text: str | None = None
    description: str = ""
    posted_at: datetime | None = None
    remote: bool | None = None
    employment_type: EmploymentType | None = None


@dataclass
class HandlerResult:
    """What an external handler (the mini-CRM, #33) reports back: the status stored in
    email_messages (use "crm") and a short note (<= 200 chars, no personal data)."""

    status: str
    note: str = ""


_REMOTE = re.compile(r"\bremot[eo]\b", re.IGNORECASE)
_URL_LINE = re.compile(r"^\s*<?(https?://\S+?)>?\s*$")


def looks_remote(*texts: str | None) -> bool | None:
    return True if any(t and _REMOTE.search(t) for t in texts) else None


def stable_id(*parts: str) -> str:
    """Deterministic id for a posting that has no link with a job id in the mail."""
    norm = "|".join(re.sub(r"\s+", " ", p).strip().lower() for p in parts)
    return "h" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def url_of_line(line: str) -> str | None:
    """The URL if the whole line is one (optionally in <angle brackets>)."""
    m = _URL_LINE.match(line)
    return m.group(1) if m else None
