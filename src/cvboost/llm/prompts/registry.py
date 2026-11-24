"""The assembled prompts of the loaded profile (#58): skeleton in code + free text in
config/prompts/*.md + config/profile.yaml. Loaded once at start (cli.main, create_app, worker);
every importer reads `current()` instead of a module constant.

No profile (missing or broken files) is not a crash: `load()` records the reason, collect and
triage keep working, and every LLM step stops with `ProfileNotLoaded` (a ValueError, which the CLI
prints as `error: ...`). Prompt versions are hashes of the assembled text, so an edit of a block
is a new version and a re-score; the owner's goldens (tests/owner) pin today's bytes.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cvboost.llm.prompts import cv_v1, email_v1, reply_v1, score_v1
from cvboost.profile import (
    LANGUAGE_NAMES,
    PRONOUN_SETS,
    UserProfile,
    load_user_profile,
    prompts_dir_of,
)

if TYPE_CHECKING:
    from cvboost.config import Settings

log = logging.getLogger(__name__)

BLOCK_MAX_BYTES = 20_000  # size limit of one prompts/*.md block (SEC-58-3)
BLOCK_NAMES: tuple[str, ...] = (*score_v1.BLOCKS, *cv_v1.BLOCKS, *reply_v1.BLOCKS)
NO_PROFILE_SCORE_VERSION = "score-no-profile"  # matches no stored verdict


class ProfileNotLoaded(ValueError):
    """The LLM steps need config/profile.yaml and config/prompts/*.md."""


@dataclass(frozen=True)
class ScorePrompt:
    system: str
    schema: dict
    task: str
    version: str


@dataclass(frozen=True)
class CvPrompt:
    rules: str
    cover_rules: str
    cover_only_rules: str
    version: str
    cover_version: str
    truth: str = ""  # the TRUTH section alone (reused by plugin reply drafts)

    def build_system_prompt(self, profile_text: str, sections: list[str]) -> str:
        return cv_v1.build_system_prompt(self.rules, self.cover_rules, profile_text, sections)

    def build_cover_system_prompt(self, profile_text: str, sections: list[str]) -> str:
        return cv_v1.build_cover_system_prompt(self.cover_only_rules, profile_text, sections)


@dataclass(frozen=True)
class ReplyPrompt:
    rules: str
    version: str

    def build_system_prompt(self, profile_text: str, links: dict[str, str]) -> str:
        return reply_v1.build_system_prompt(self.rules, profile_text, links)


@dataclass(frozen=True)
class EmailPrompt:
    system: str
    schema: dict
    task: str
    version: str


@dataclass(frozen=True)
class PromptSet:
    profile: UserProfile
    profiles: tuple[str, ...]  # rules.yaml relevance.profiles (without "none")
    score: ScorePrompt
    cv: CvPrompt
    reply: ReplyPrompt
    email: EmailPrompt
    own_slugs: tuple[str, ...] = ()  # the owner's linkedin.com/in/<slug> from profile.md (CRM)

    @property
    def keys(self) -> dict[str, str]:
        return self.profile.keys()

    def versions(self) -> dict[str, str]:
        return {
            "score": self.score.version,
            "cv": self.cv.version,
            "cover": self.cv.cover_version,
            "reply": self.reply.version,
            "email": self.email.version,
        }


_READ_CAP = BLOCK_MAX_BYTES + 4096  # room for the newlines around the text, nothing more is read


class _BlockRefused(Exception):
    """A block file that is not read at all (symlink, not a regular file, too big)."""


def _read_block(path: Path) -> str:
    """Stat before reading: a symlink, a FIFO/device or an oversized file is refused without
    reading it (SEC-59, a block must be a plain file inside the config folder). O_NOFOLLOW makes the
    open fail on a link swapped in after the check."""
    if stat.S_ISLNK(path.lstat().st_mode):
        raise _BlockRefused("is a symlink: refused")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _BlockRefused("is not a regular file: refused")
        if st.st_size > _READ_CAP:  # the exact limit is checked on the stripped text
            raise _BlockRefused(f"is over {BLOCK_MAX_BYTES // 1000} KB")
        data = os.read(fd, _READ_CAP)
    finally:
        os.close(fd)
    return data.decode("utf-8").strip("\n")


def read_blocks(directory: Path) -> tuple[dict[str, str], list[str]]:
    """(blocks, problems). Leading and trailing newlines of a file are not part of the block."""
    blocks: dict[str, str] = {}
    problems: list[str] = []
    for name in BLOCK_NAMES:
        path = directory / f"{name}.md"
        try:
            text = _read_block(path)
        except _BlockRefused as exc:
            problems.append(f"prompt block {path} {exc}")
            continue
        except (OSError, UnicodeDecodeError):
            problems.append(f"missing prompt block {path}")
            continue
        if len(text.encode("utf-8")) > BLOCK_MAX_BYTES:
            problems.append(f"prompt block {path} is over {BLOCK_MAX_BYTES // 1000} KB")
            continue
        if not text.strip():
            problems.append(f"empty prompt block {path}")
            continue
        blocks[name] = text
    return blocks, problems


_DATA_TAGS = (
    "job|contact|owner_context|profile|feedback|approved_links|latest_message_emails|"
    "owner_corrections"
)
_SUSPICIOUS = re.compile(
    rf"<\s*/?\s*(?:{_DATA_TAGS})\b|\bignore\b|\binstruction|\bSECURITY\b", re.I
)


def block_warnings(blocks: Mapping[str, str]) -> list[str]:
    """Blocks that talk about the fixed security rules or the data tags (SEC-58-1). Not an error:
    the code appends its own FIXED RULES last, but a block that argues with them is a smell."""
    out = []
    for name, text in blocks.items():
        m = _SUSPICIOUS.search(text)
        if m:
            out.append(
                f"prompt block {name} mentions {m.group(0)!r}: blocks describe the person, never "
                "the security rules or the data tags such as <job>, <contact>, <profile>, "
                "<feedback> (code owns them)"
            )
    return out


_LANG_IN_BLOCK = re.compile(r"\bin (" + "|".join(LANGUAGE_NAMES.values()) + r")\b")


def language_warnings(profile: UserProfile, blocks: Mapping[str, str]) -> list[str]:
    """The fit-analysis block names a language other than explain_language (#70): the code adds
    its own LANGUAGE line, so a stale "in Russian" in the block contradicts it."""
    want = LANGUAGE_NAMES[profile.explain_language]
    other = sorted(
        {m for m in _LANG_IN_BLOCK.findall(blocks.get("cv_fit_analysis", "")) if m != want}
    )
    if not other:
        return []
    return [
        f"prompt block cv_fit_analysis says 'in {other[0]}' but explain_language is "
        f"{profile.explain_language} ({want}): remove the language from the block, the code "
        "adds it"
    ]


RESCORE_NOTE = (
    "renaming, removing or reordering relevance.profiles changes the scoring prompt version: "
    "every job is scored again"
)


def build(
    profile: UserProfile,
    blocks: Mapping[str, str],
    relevance_profiles: tuple[str, ...],
    own_slugs: tuple[str, ...] = (),
) -> PromptSet:
    keys = profile.keys()
    score_system = score_v1.build_system_prompt(blocks, keys)
    score_schema = score_v1.json_schema(relevance_profiles)
    rules = cv_v1.build_rules(blocks, keys)
    cover = cv_v1.cover_rules(blocks)
    cover_only = cv_v1.build_cover_only_rules(blocks, keys)
    reply_rules = reply_v1.build_rules(blocks, keys)
    return PromptSet(
        profile=profile,
        profiles=relevance_profiles,
        own_slugs=own_slugs,
        score=ScorePrompt(
            score_system,
            score_schema,
            score_v1.TASK,
            score_v1.prompt_version(score_system, score_schema),
        ),
        cv=CvPrompt(
            rules,
            cover,
            cover_only,
            cv_v1.prompt_version(rules, cover),
            cv_v1.cover_prompt_version(cover_only),
            cv_v1.truth_shared(blocks, keys),
        ),
        reply=ReplyPrompt(reply_rules, reply_v1.prompt_version(reply_rules)),
        email=_email_prompt(),
    )


def _email_prompt() -> EmailPrompt:
    """Not personal (a job-alert e-mail is parsed the same for everyone): needs no profile."""
    return EmailPrompt(
        email_v1.SYSTEM_PROMPT, email_v1.JSON_SCHEMA, email_v1.TASK, email_v1.PROMPT_VERSION
    )


def load_from_paths(
    profile_path: Path, rules_path: Path, resume_dir: Path | None = None
) -> PromptSet:
    """Strict: raises ValueError with every problem found. `resume_dir` (default: `resume/` next
    to profile.yaml) only supplies the owner's LinkedIn slug from profile.md."""
    from cvboost.core.rules import load_config
    from cvboost.cv.profile import PROFILE_NAME, linkedin_slugs

    profile = load_user_profile(profile_path)
    blocks, problems = read_blocks(prompts_dir_of(profile, profile_path))
    if problems:
        raise ValueError("; ".join(problems))
    rules = load_config(rules_path)
    if not rules.relevance.profiles:
        raise ValueError(f"{rules_path}: relevance.profiles is empty (name at least one profile)")
    resume = resume_dir or Path(profile_path).resolve().parent / "resume"
    try:
        slugs = linkedin_slugs((resume / PROFILE_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        slugs = ()  # no fact base yet: CV steps say so themselves
    return build(profile, blocks, tuple(rules.relevance.profiles), slugs)


_state: dict = {"set": None, "error": None}


def load(settings: Settings) -> PromptSet | None:
    """Load the profile of `settings`; never raises. The reason of a failure is in `error()`."""
    try:
        ps = load_from_paths(settings.profile_path, settings.rules_path, settings.resume_dir)
    except ValueError as exc:
        _state.update(set=None, error=str(exc))
        log.warning("no profile, LLM steps are off: %s", exc)
        return None
    _state.update(set=ps, error=None)
    return ps


def ensure_loaded(settings: Settings) -> PromptSet | None:
    """Load unless already loaded (create_app, run_worker: the CLI entry point loaded it)."""
    return _state["set"] or load(settings)


def reset() -> None:
    _state.update(set=None, error=None)


def install(ps: PromptSet | None, error: str | None = None) -> None:
    """For tests: set the current prompt set directly."""
    _state.update(set=ps, error=error)


def loaded() -> PromptSet | None:
    return _state["set"]


def is_loaded() -> bool:
    return _state["set"] is not None


def error() -> str | None:
    return _state["error"]


def not_loaded_message() -> str:
    why = _state["error"] or "load() was not called"
    return (
        f"no user profile, LLM steps are off ({why}). Create config/profile.yaml and "
        "config/prompts/*.md (see `cvboost profile check`)."
    )


def current() -> PromptSet:
    ps = _state["set"]
    if ps is None:
        raise ProfileNotLoaded(not_loaded_message())
    return ps


def score_version() -> str:
    """The scoring prompt version, or a placeholder that matches no stored verdict (web, alerts)."""
    ps = _state["set"]
    return ps.score.version if ps else NO_PROFILE_SCORE_VERSION


def profile_names() -> tuple[str, ...]:
    ps = _state["set"]
    return ps.profiles if ps else ()


def keys() -> dict[str, str]:
    """Substitution keys; without a profile only the neutral pronouns (no name)."""
    ps = _state["set"]
    if ps:
        return ps.keys
    p = PRONOUN_SETS["they"]
    return {"name": "", "first_name": "", **p, "He": "They", "His": "Their"}


def email_prompt() -> EmailPrompt:
    """The e-mail extraction prompt: works without a profile (it is not personal)."""
    ps = _state["set"]
    return ps.email if ps else _email_prompt()


def own_slugs() -> tuple[str, ...]:
    ps = _state["set"]
    return ps.own_slugs if ps else ()


def client_label() -> str | None:
    """profile.yaml crm.client_label; None without a profile (the code default is used)."""
    ps = _state["set"]
    return ps.profile.crm.client_label if ps else None
