"""Normalised data model shared by all connectors."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from cvboost.textutil import clean_text

SourceKind = Literal["api", "rss", "ats", "email", "html", "browser"]
EmploymentType = Literal["full_time", "part_time", "contract", "freelance", "internship", "other"]
SalaryPeriod = Literal["hour", "day", "month", "year"]


# company of a posting whose employer is not named (e-mail alerts, recruiters). It is not an
# identity: dedupe must never merge two jobs because of it.
UNDISCLOSED_COMPANY = "Undisclosed client"

_HTTP = re.compile(r"^https?://", re.IGNORECASE)
_URL_CONTROL = re.compile(r"[\x00-\x20\x7f-\x9f]")


class RawPosting(BaseModel):
    """One posting as the source returned it, before normalisation."""

    source: str
    source_id: str
    payload: dict[str, Any]


class Job(BaseModel):
    source: str
    source_id: str
    url: str
    apply_url: str | None = None
    company: str
    title: str
    description_html: str = ""
    description_text: str = ""
    location_text: str | None = None
    remote: bool | None = None
    employment_type: EmploymentType | None = None
    tags: list[str] = Field(default_factory=list)
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: SalaryPeriod | None = None
    salary_text: str | None = None
    posted_at: datetime | None = None
    fetched_at: datetime
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title", "company", "location_text", "description_text", mode="before")
    @classmethod
    def _sanitise(cls, v: object) -> object:
        return clean_text(v) if isinstance(v, str) else v

    @field_validator("url")
    @classmethod
    def _http_only(cls, v: str) -> str:
        if not _HTTP.match(v) or _URL_CONTROL.search(v):
            raise ValueError("only http(s) URLs without control characters are allowed")
        return v

    @field_validator("apply_url")
    @classmethod
    def _apply_http_only(cls, v: str | None) -> str | None:
        if v is None or not _HTTP.match(v) or _URL_CONTROL.search(v):
            return None
        return v

    @field_validator("source", "source_id", "company", "title")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v

    @field_validator("posted_at", "fetched_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")
        return v

    @property
    def content_hash(self) -> str:
        parts = [self.title, self.company, self.description_text]
        text = "|".join(re.sub(r"\s+", " ", p).strip() for p in parts)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
