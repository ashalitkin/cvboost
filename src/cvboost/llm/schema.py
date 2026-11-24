"""Pydantic model for one verdict; model output is untrusted and validated here."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cvboost.llm.sanitize import strip_invisible
from cvboost.mail.mask import strip_unsafe_field

Seniority = Literal["junior", "mid", "senior", "lead", "principal", "unknown"]
Employment = Literal["contract", "freelance", "full_time", "unknown"]
Flag = Annotated[str, Field(max_length=80)]


class JobVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eligible: bool
    eligibility_reason: str = Field(max_length=300)
    fit: int = Field(ge=0, le=100)
    profile: str  # a key of rules.yaml relevance.profiles, or "none" (checked in parse_batch only:
    # verdicts stored under an older rules.yaml must stay readable, #59)
    seniority: Seniority
    employment: Employment
    rate_text: str | None = Field(max_length=80)
    red_flags: list[Flag] = Field(max_length=6)
    summary_ru: str = Field(max_length=400)

    @field_validator("eligibility_reason", "rate_text", "summary_ru", mode="before")
    @classmethod
    def _clean(cls, v: object) -> object:
        return strip_invisible(v) if isinstance(v, str) else v

    @field_validator("red_flags", mode="before")
    @classmethod
    def _clean_list(cls, v: object) -> object:
        if isinstance(v, list):
            return [strip_invisible(x) if isinstance(x, str) else x for x in v]
        return v


def parse_batch(
    structured: dict | None, refs: list[str]
) -> tuple[dict[str, JobVerdict], dict[str, str]]:
    """Validate a model response against the refs we sent.

    Unknown refs are dropped. Returns (valid, invalid) where invalid maps ref -> reason:
    `missing_ref` (absent from the response) or `invalid_output` (duplicated or fails validation).
    """
    items = structured.get("verdicts") if isinstance(structured, dict) else None
    if not isinstance(items, list):
        return {}, dict.fromkeys(refs, "invalid_output")
    from cvboost.llm.prompts import registry  # late: the registry imports the prompt modules

    allowed = registry.profile_names()
    known = set(refs)
    seen: dict[str, list[dict]] = {}
    for it in items:
        ref = it.get("ref") if isinstance(it, dict) else None
        if isinstance(ref, str) and ref in known:
            seen.setdefault(ref, []).append(it)
    valid: dict[str, JobVerdict] = {}
    invalid: dict[str, str] = {}
    for ref in refs:
        got = seen.get(ref, [])
        if not got:
            invalid[ref] = "missing_ref"
            continue
        if len(got) != 1:
            invalid[ref] = "invalid_output"
            continue
        body = {k: v for k, v in got[0].items() if k != "ref"}
        try:
            verdict = JobVerdict.model_validate(body)
        except ValidationError:
            invalid[ref] = "invalid_output"
            continue
        if allowed and verdict.profile != "none" and verdict.profile not in allowed:
            invalid[ref] = "invalid_output"  # not a key of rules.yaml relevance.profiles
            continue
        valid[ref] = verdict
    return valid, invalid


class EmailJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=2, max_length=200)
    company: str | None = Field(max_length=200)
    location: str | None = Field(max_length=200)
    remote: bool | None
    employment_type: (
        Literal["full_time", "part_time", "contract", "freelance", "internship", "other"] | None
    )
    rate_text: str | None = Field(max_length=100)
    link_ref: int | None = Field(ge=1, le=99)  # number of a [job link N] we computed, never a URL

    @field_validator("title", "company", "location", "rate_text", mode="before")
    @classmethod
    def _clean(cls, v: object) -> object:
        # the model must not smuggle URLs, addresses or phone numbers through text fields
        return strip_unsafe_field(strip_invisible(v)) if isinstance(v, str) else v


class EmailVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_job_offer: bool
    not_job_reason: str = Field(max_length=150)
    injection: bool
    jobs: list[EmailJob] = Field(max_length=10)

    @field_validator("not_job_reason", mode="before")
    @classmethod
    def _clean(cls, v: object) -> object:
        return strip_invisible(v) if isinstance(v, str) else v


def parse_email_batch(
    structured: dict | None, refs: list[str]
) -> tuple[dict[str, EmailVerdict], dict[str, str]]:
    """Like `parse_batch` for the e-mail fallback: (valid, invalid) keyed by ref."""
    items = structured.get("emails") if isinstance(structured, dict) else None
    if not isinstance(items, list):
        return {}, dict.fromkeys(refs, "invalid_output")
    known = set(refs)
    seen: dict[str, list[dict]] = {}
    for it in items:
        ref = it.get("ref") if isinstance(it, dict) else None
        if isinstance(ref, str) and ref in known:
            seen.setdefault(ref, []).append(it)
    valid: dict[str, EmailVerdict] = {}
    invalid: dict[str, str] = {}
    for ref in refs:
        got = seen.get(ref, [])
        if not got:
            invalid[ref] = "missing_ref"
        elif len(got) > 1:
            invalid[ref] = "invalid_output"
        else:
            try:
                valid[ref] = EmailVerdict.model_validate(
                    {k: v for k, v in got[0].items() if k != "ref"}
                )
            except ValidationError:
                invalid[ref] = "invalid_output"
    return valid, invalid
