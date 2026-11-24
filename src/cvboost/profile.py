"""config/profile.yaml: who the system works for (name, pronouns, CV checks, CRM labels, #58).

Free text about the person lives in config/prompts/*.md; this file holds the small facts that
code and prompt skeletons substitute. A missing profile is not a crash: collect and triage
work, the LLM steps stop with a clear message (see cvboost.llm.prompts.registry).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

PRONOUN_SETS: dict[str, dict[str, str]] = {
    "he": {"he": "he", "his": "his", "him": "him", "himself": "himself", "wants": "wants"},
    "she": {"he": "she", "his": "her", "him": "her", "himself": "herself", "wants": "wants"},
    "they": {"he": "they", "his": "their", "him": "them", "himself": "themselves", "wants": "want"},
}


Language = Literal["en", "ru", "uk", "pt", "es", "de", "fr", "it", "nl", "pl"]
UiLanguage = Literal["en"]  # the interface is English only (#70)
DocumentsLanguage = Literal["en"]  # CV checks are written for English (#70)

# English names of the languages, substituted into prompts. Only these names ever reach a prompt:
# the raw config value never does.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "ru": "Russian",
    "uk": "Ukrainian",
    "pt": "Portuguese",
    "es": "Spanish",
    "de": "German",
    "fr": "French",
    "it": "Italian",
    "nl": "Dutch",
    "pl": "Polish",
}


def _compile(pattern: str) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"bad regex {pattern!r}: {exc}") from None


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaseRule(_M):
    """A sentence naming a case (regex) may only use tools found on that case's profile lines."""

    sentence: str
    profile_lines: str
    section: str | None = None  # heading prefix of a profile section whose whole body also counts

    @field_validator("sentence", "profile_lines")
    @classmethod
    def _regex(cls, v: str) -> str:
        _compile(v)
        return v


class CrmCfg(_M):
    client_label: str = "Client"


class CvCfg(_M):
    cases: list[CaseRule] = []
    no_tool_cases: list[str] = []  # regexes: a sentence naming such a case names no tool
    extra_tools: list[str] = []

    @field_validator("no_tool_cases")
    @classmethod
    def _regex(cls, v: list[str]) -> list[str]:
        for p in v:
            _compile(p)
        return v


class UserProfile(_M):
    name: str
    first_name: str
    pronouns: Literal["he", "she", "they"] = "they"
    photo: str = "photo.jpg"
    timezone: str = "UTC"
    ui_language: UiLanguage = "en"
    explain_language: Language = "en"  # language of the LLM's job summaries and fit notes
    documents_language: DocumentsLanguage = "en"
    crm: CrmCfg = CrmCfg()
    cv: CvCfg = CvCfg()
    resume_skill_sha: str | None = None
    prompts_dir: str = "prompts"  # relative to the directory of profile.yaml

    @field_validator("name", "first_name")
    @classmethod
    def _text(cls, v: str) -> str:
        if not v.strip() or "\n" in v or "$" in v:
            raise ValueError("must be one line without '$'")
        return v.strip()

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError(f"unknown time zone {v!r}") from None
        return v

    @field_validator("prompts_dir")
    @classmethod
    def _dir(cls, v: str) -> str:
        # SEC-58-3: blocks are read from inside the config folder only
        parts = Path(v).parts
        if not v.strip() or Path(v).is_absolute() or ".." in parts or v.startswith(("~", "$")):
            raise ValueError("must be a relative folder without '..'")
        return v

    @field_validator("resume_skill_sha")
    @classmethod
    def _sha(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"[0-9a-f]{64}", v):
            raise ValueError("must be a sha256 hex digest")
        return v

    def keys(self) -> dict[str, str]:
        """The only substitution keys of the prompt skeletons."""
        p = PRONOUN_SETS[self.pronouns]
        return {
            "name": self.name,
            "first_name": self.first_name,
            **p,
            "explain_language": LANGUAGE_NAMES[self.explain_language],
            "He": p["he"].capitalize(),
            "His": p["his"].capitalize(),
        }


def load_user_profile(path: Path | str) -> UserProfile:
    """Raises ValueError with a readable message."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return UserProfile.model_validate(data)
    except FileNotFoundError:
        raise ValueError(f"profile not found: {path}") from None
    except ValidationError as exc:
        # one line per problem: the key name and pydantic's message (for a Literal it lists the
        # allowed values, not the rejected input; custom validators such as timezone may quote it)
        found = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'profile'}: {e['msg']}" for e in exc.errors()
        )
        raise ValueError(f"bad profile {path}: {found}") from exc
    except (yaml.YAMLError, OSError) as exc:
        raise ValueError(f"bad profile {path}: {exc}") from exc


def prompts_dir_of(profile: UserProfile, profile_path: Path | str) -> Path:
    return Path(profile_path).resolve().parent / profile.prompts_dir
