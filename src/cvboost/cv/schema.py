"""CvDraft: the validated shape of the model's answer. The answer is untrusted text."""

from __future__ import annotations

import unicodedata
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cvboost.llm.sanitize import strip_invisible

Short = Annotated[str, Field(max_length=300)]


def _clean(v: Any) -> Any:
    if isinstance(v, str):
        return strip_invisible(unicodedata.normalize("NFKC", v))
    if isinstance(v, list):
        return [_clean(x) for x in v]
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    return v


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecruiterMessage(_Strict):
    connection_note: str = Field(max_length=300)
    inmail: str = Field(max_length=1500)


class FitNotes(_Strict):
    matches: list[Short] = Field(max_length=12)
    risks: list[Short] = Field(max_length=12)
    gaps: list[Short] = Field(max_length=12)
    recommendation: Literal["apply", "apply_with_caveats", "skip"]
    summary: str = Field(max_length=600)


class Claim(_Strict):
    resume_text: str = Field(max_length=300)
    section: str = Field(max_length=200)  # checked against the profile headings by validate()
    profile_quote: str = Field(max_length=200)


COVER_MAX = 4500


class CoverDraft(_Strict):
    """The answer of a cover-letter-only call (a kit that already exists)."""

    cover_letter_md: str = Field(min_length=1, max_length=COVER_MAX)
    cover_claims: list[Claim] = Field(min_length=2, max_length=30)
    injection_suspected: bool

    @model_validator(mode="before")
    @classmethod
    def _clean_all(cls, data: Any) -> Any:
        return _clean(data)


class CvDraft(_Strict):
    resume_md: str = Field(max_length=12000)
    recruiter_message: RecruiterMessage
    fit_notes_ru: FitNotes
    claims: list[Claim] = Field(min_length=5, max_length=60)
    injection_suspected: bool
    # the cover letter (#32): required in the model's JSON schema, optional here so that an
    # answer without one (old fixtures) still makes a kit
    cover_letter_md: str = Field(default="", max_length=COVER_MAX)
    cover_claims: list[Claim] = Field(default_factory=list, max_length=30)

    @model_validator(mode="before")
    @classmethod
    def _clean_all(cls, data: Any) -> Any:
        """Every string of the answer: NFKC (a fullwidth "Ｐower BI" is "Power BI") and no
        control / zero-width / bidi characters, before any length check or scan sees it."""
        return _clean(data)
