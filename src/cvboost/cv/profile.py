"""config/resume/profile.md: the single fact base. Loading, sections, Known-gaps config."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

RESUME_DIR = Path("config/resume")
PROFILE_NAME = "profile.md"
GAPS_NAME = "gaps.yaml"
PHOTO_NAME = "photo.jpg"
CSS_NAME = "resume.css"

_HEADING = re.compile(r"^(#{2,3}) +(.+?) *$")
_GAPS_TITLE = "Known gaps"
_EXCLUDED_PREFIXES = ("Known gaps", "Applications log")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def norm(text: str) -> str:
    """Lower-case, drop markdown emphasis / code ticks, collapse whitespace (quote matching)."""
    text = text.replace("*", "").replace("`", "").replace("’", "'")
    return re.sub(r"\s+", " ", text).strip().lower()


def numbers_in(text: str) -> set[str]:
    return set(_NUMBER.findall(text))


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TLDS = (
    "com|io|org|net|dev|ai|co|me|app|pt|eu|us|uk|info|biz|xyz|ru|de|fr|es|it|nl|ca|au|in|cc|tv|"
    "ly|to|gg|sh|cloud|site|online|tech|page|link|click|top|work|run"
)
_HOST = re.compile(
    # any URL with a scheme, or a bare host on a common TLD (bare "node.js" is not a host)
    r"[a-z][a-z0-9+.-]*://[^\s)\]>\"']+|"  # any scheme: norm_link rejects all but https
    rf"(?<![\w@.-])(?:[a-z0-9-]+\.)+(?:{_TLDS})\b(?:/[^\s)\]>\"']*)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Contacts:
    """What the Contacts section of the profile allows a resume or message to point at."""

    emails: frozenset[str]
    links: frozenset[str]  # lower-case `host` or `host/path`, no scheme / www / trailing slash;
    # the owner's own site / profiles: the link itself and anything below it
    exact: frozenset[str] = frozenset()  # published articles: this exact URL only


def norm_link(raw: str) -> str:
    """Whole-URL key: host (lower case) + path, a trailing "/" dropped. Anything that is not
    plain https (or a bare host), or carries userinfo, a port, a query or a fragment, gets a
    key that no allowed link can equal."""
    raw = raw.strip().rstrip(",;:)")  # not dots: a trailing ".." is a dot segment
    m = re.match(r"(?i)^(?P<scheme>[a-z][a-z0-9+.-]*)://", raw)
    if m:
        if m.group("scheme").lower() != "https":
            return "!" + raw
        raw = raw[m.end() :]
    host, sep, path = raw.partition("/")
    if re.search(r"[?#@:\s\\]", host) or re.search(r"[?#\\]", path):
        return "!" + raw
    # "." / ".." segments (also %2e in any case): a browser collapses them, so the link would
    # leave the allowed prefix
    if re.search(r"(?i)(^|/)(\.|%2e){1,2}(/|$)", path):
        return "!" + raw
    host = re.sub(r"(?i)^www\.", "", host).lower()
    key = host + sep + path
    return key.rstrip(".").rstrip("/") if not sep else (host + sep + path.rstrip(".")).rstrip("/")


def find_contacts(text: str) -> tuple[set[str], set[str]]:
    """(emails, links) mentioned in a text. Emails are removed before looking for hosts."""
    emails = {e.lower() for e in _EMAIL.findall(text)}
    rest = _EMAIL.sub(" ", text)
    return emails, {norm_link(m.group()) for m in _HOST.finditer(rest)}


def allowed_contacts(contacts_body: str, publications_body: str = "") -> Contacts:
    emails: set[str] = set()
    links: set[str] = set()
    for line in contacts_body.splitlines():
        found = _EMAIL.findall(line)
        if found:
            emails.add(found[0].lower())  # first address per line: the second is "not for jobs"
        links |= find_contacts(line)[1]
    exact = find_contacts(publications_body)[1]  # emails there are never allowed
    return Contacts(frozenset(emails), frozenset(links), frozenset(exact))


# the slug is a whole path segment of linkedin.com (or a subdomain of it): not a prefix of a longer
# word ("evil-linkedin.com/in/x", "linkedin.com/in/x.evil.com" are not the owner's profile)
_LINKEDIN_SLUG = re.compile(
    r"(?<![\w.-])(?:[a-z0-9-]+\.)?linkedin\.com/in/([A-Za-z0-9_%-]+)"
    r"(?=[/?#\s)>\]]|[.,;:](?:\s|$)|$)",
    re.IGNORECASE,
)


def linkedin_slugs(profile_text: str) -> tuple[str, ...]:
    """The owner's own LinkedIn profile slug(s): linkedin.com/in/<slug> in the Contacts section."""
    sections = parse_profile(profile_text).sections if profile_text else {}
    found = _LINKEDIN_SLUG.findall(sections.get("Contacts", ""))
    return tuple(dict.fromkeys(slug.lower() for slug in found))


def contact_allowed(link: str, allowed: Contacts) -> bool:
    if link in allowed.exact or link in allowed.links:
        return True
    # subpaths only below the owner's own profile pages (linkedin.com/in/.., github.com/..)
    return any("/" in a and link.startswith(a + "/") for a in allowed.links)


@dataclass(frozen=True)
class Profile:
    text: str  # as on disk
    prompt_text: str  # without Applications log
    sections: dict[str, str]  # claimable heading text -> body (## sections include their ###)
    known_gaps: list[str]  # the lead-in of each Known gaps bullet (before the first ':')
    sha: str
    numbers: frozenset[str] = field(default_factory=frozenset)
    contacts: Contacts = field(default_factory=lambda: Contacts(frozenset(), frozenset()))

    @property
    def section_names(self) -> list[str]:
        return list(self.sections)


def _split(text: str) -> list[tuple[int, str, str]]:
    """[(level, title, body)] for every ## / ### heading; body runs to the next heading."""
    out: list[tuple[int, str, list[str]]] = []
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            out.append((len(m.group(1)), m.group(2), []))
        elif out:
            out[-1][2].append(line)
    return [(lvl, title, "\n".join(body).strip()) for lvl, title, body in out]


def parse_profile(text: str) -> Profile:
    parts = _split(text)
    sections: dict[str, str] = {}
    gaps: list[str] = []
    excluded = False
    for i, (lvl, title, body) in enumerate(parts):
        if lvl == 2:
            excluded = title.startswith(_EXCLUDED_PREFIXES)
            if title.startswith(_GAPS_TITLE):
                for line in body.splitlines():
                    if line.startswith("- "):
                        gaps.append(line[2:].split(":", 1)[0].strip())
        if excluded:
            continue
        full = body
        if lvl == 2:  # a ## section also covers its ### children
            kids = []
            for lvl2, _t, b2 in parts[i + 1 :]:
                if lvl2 == 2:
                    break
                kids.append(f"{_t}\n{b2}")
            full = "\n".join([body, *kids])
        sections[title] = full
    # prompt text: everything except the Applications log section
    prompt_lines: list[str] = []
    skip = False
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m and m.group(1) == "##":
            skip = m.group(2).startswith("Applications log")
        if not skip:
            prompt_lines.append(line)
    return Profile(
        text=text,
        prompt_text="\n".join(prompt_lines).strip() + "\n",
        sections=sections,
        known_gaps=gaps,
        sha=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        # numbers a resume may use: only the fact sections (not the Applications log with rates
        # of other jobs, not the Known gaps text)
        numbers=frozenset(n for body in sections.values() for n in numbers_in(body)),
        contacts=allowed_contacts(
            sections.get("Contacts", ""), sections.get("Publications and writing", "")
        ),
    )


def load_profile(resume_dir: Path = RESUME_DIR) -> Profile:
    try:
        return parse_profile((resume_dir / PROFILE_NAME).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {resume_dir / PROFILE_NAME}: {exc.strerror}") from None


# --- gaps.yaml ------------------------------------------------------------------------------


class GapRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source: str  # lead-in of the Known gaps bullet it covers, or "policy" (a rule not from there)
    mode: Literal["forbid", "review"]
    # cover letter mode when it differs from the resume, e.g. "allow" for a gap the owner lets a
    # letter name honestly (Power BI, 2026-11-24); None = same as `mode`
    cover: Literal["forbid", "review", "allow"] | None = None
    patterns: list[str]


class GapsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gaps: list[GapRule]


def load_gaps(resume_dir: Path = RESUME_DIR) -> GapsConfig:
    path = resume_dir / GAPS_NAME
    try:
        cfg = GapsConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        for g in cfg.gaps:
            for p in g.patterns:
                re.compile(p)
    except (yaml.YAMLError, ValidationError, OSError, re.error) as exc:
        raise ValueError(f"bad {path}: {exc}") from None
    ids = [g.id for g in cfg.gaps]
    if len(ids) != len(set(ids)):
        raise ValueError(f"bad {path}: duplicate gap ids")
    return cfg


def check_gaps(profile: Profile, gaps: GapsConfig) -> list[str]:
    """Problems that stop the CV step: a Known gaps bullet nobody guards, or a rule with no
    pattern. Empty list = in sync."""
    problems: list[str] = []
    covered = {g.source for g in gaps.gaps}
    for item in profile.known_gaps:
        if item not in covered:
            problems.append(f"Known gaps item has no entry in gaps.yaml: {item!r}")
    for g in gaps.gaps:
        if not g.patterns:
            problems.append(f"gaps.yaml entry {g.id!r} has no patterns")
        if g.source != "policy" and g.source not in profile.known_gaps:
            problems.append(f"gaps.yaml entry {g.id!r}: source {g.source!r} not in Known gaps")
    return problems
