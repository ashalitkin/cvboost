"""Deterministic checks on a CvDraft. No LLM: the model's own claims are never trusted.

Each violation has a code (stored in cv_requests.review_reasons) and a short safe argument.
Class `retry` = worth one more generation with feedback; `review` = the owner must look
(no retry); `injection` = the posting tried to steer the model (no retry).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from markdown_it import MarkdownIt

from cvboost.cv.profile import (
    GapsConfig,
    Profile,
    contact_allowed,
    find_contacts,
    norm,
    norm_link,
    numbers_in,
)
from cvboost.cv.schema import Claim, CvDraft
from cvboost.llm.prompts import registry
from cvboost.profile import UserProfile

Kind = Literal["retry", "review", "injection"]

_EMOJI = re.compile(r"[\U0001f000-\U0001faff\u2600-\u27bf\u2b50\u2b55\ufe0f\u20e3]")
_RAW_TAG = re.compile(r"<\s*/?[a-zA-Z][a-zA-Z0-9]*(\s[^>]*)?/?>")
_MONEY = re.compile(r"[$€£]\s?\d|\d\s?(USD|EUR|GBP)\b|\d\s?(dollars|euros)\b", re.IGNORECASE)
_MD = MarkdownIt("commonmark", {"html": False})
ALLOWED_IMAGE = "photo.jpg"


@dataclass(frozen=True)
class Violation:
    code: str  # gap_term | gap_review | unsupported_number | bad_claim | structure | hygiene | ...
    arg: str  # short and safe (never raw model text beyond ids/digits)
    kind: Kind

    @property
    def reason(self) -> str:
        return f"{self.code}:{self.arg}" if self.arg else self.code


def _walk(tokens):
    for t in tokens:
        yield t
        if t.children:
            yield from _walk(t.children)


def _user(user: UserProfile | None) -> UserProfile:
    """The person the checks are for: config/profile.yaml (name, cases, extra tools)."""
    return user if user is not None else registry.current().profile


def validate(
    draft: CvDraft, profile: Profile, gaps: GapsConfig, user: UserProfile | None = None
) -> list[Violation]:
    user = _user(user)
    out: list[Violation] = []
    md = draft.resume_md

    # 1. Known gaps (resume text and the claims that restate it; the recruiter message may name
    # a gap honestly, so it is not scanned)
    scanned = "\n".join([md, *(c.resume_text for c in draft.claims)])
    for g in gaps.gaps:
        if any(re.search(p, scanned, re.IGNORECASE) for p in g.patterns):
            if g.mode == "forbid":
                out.append(Violation("gap_term", g.id, "retry"))
            else:
                out.append(Violation("gap_review", g.id, "review"))

    # 2. numbers must exist in the profile
    bad_numbers = sorted(numbers_in(md) - profile.numbers)
    for n in bad_numbers[:5]:
        out.append(Violation("unsupported_number", n[:20], "retry"))

    # 3. claims: section exists and the quote is really in it
    for i, c in enumerate(draft.claims, 1):
        body = profile.sections.get(c.section)
        if body is None or not norm(c.profile_quote) or norm(c.profile_quote) not in norm(body):
            out.append(Violation("bad_claim", str(i), "retry"))

    # 4. structure
    if not re.search(rf"^# {re.escape(user.name)}\b", md, re.MULTILINE):
        out.append(Violation("structure", "h1", "retry"))
    for name in ("Experience", "Education"):
        if not re.search(rf"^## {name}\b", md, re.MULTILINE):
            out.append(Violation("structure", name.lower(), "retry"))

    # 5. hygiene
    if _EMOJI.search(md):
        out.append(Violation("hygiene", "emoji", "retry"))
    if _RAW_TAG.search(md):
        out.append(Violation("hygiene", "raw_html", "retry"))
    images, links = set(), set()
    for t in _walk(_MD.parse(md)):
        if t.type == "image":
            images.add(str(t.attrGet("src") or ""))
        elif t.type == "link_open":
            links.add(str(t.attrGet("href") or ""))
    if any(src != ALLOWED_IMAGE for src in images):
        out.append(Violation("hygiene", "foreign_image", "retry"))
    if any(not (h.startswith("https://") or h.startswith("mailto:")) for h in links):
        out.append(Violation("hygiene", "non_https_link", "retry"))
    rm = draft.recruiter_message
    if _MONEY.search(rm.connection_note) or _MONEY.search(rm.inmail):
        out.append(Violation("hygiene", "money_in_message", "retry"))
    if _MONEY.search(md):
        out.append(Violation("money_in_resume", "", "retry"))

    for href in links:
        if href.startswith("mailto:"):
            ok = href[7:].split("?")[0].lower() in profile.contacts.emails
        else:
            ok = contact_allowed(norm_link(href), profile.contacts)
        if not ok:
            out.append(Violation("foreign_contact", "href", "review"))
            break

    # 5b. nothing may point anywhere but the owner's own contacts (a posting could talk the
    # model into adding its own link or address to a document the owner sends)
    for where, text in (
        ("resume", md),
        ("note", rm.connection_note),
        ("inmail", rm.inmail),
    ):
        emails, links = find_contacts(text)
        bad_mail = emails - profile.contacts.emails
        bad_link = {x for x in links if not contact_allowed(x, profile.contacts)}
        if bad_mail or bad_link:
            out.append(Violation("foreign_contact", where, "review"))

    # 5c. the cover letter (#32) goes through the same checks
    if draft.cover_letter_md.strip():
        out += validate_cover(draft.cover_letter_md, draft.cover_claims, profile, gaps, user)

    # 6. the posting tried to steer the model
    if draft.injection_suspected:
        out.append(Violation("injection_suspected", "", "injection"))
    return out


_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
COVER_MAX_WORDS = 200
COVER_MIN_CLAIMS = 2


# generic words and ad platforms of the skills inventory that are not "a tool of one case"
_TOOL_STOP = {
    "Studio", "Enterprise", "Ads", "Code", "Actions", "Mix", "Modeling", "Java", "Marketing",
    "Google", "Power", "API", "APIs", "LLM", "SQL", "Microsoft", "LinkedIn", "Meta", "Claude",
    "TikTok", "Pinterest", "Reddit", "UTM", "CAC", "MRR", "LTV", "ROAS", "Conversions", "GitHub",
}  # fmt: skip
_NOT_CASES = ("Skills inventory", "Headline numbers", "Contacts", "Known gaps", "Applications log")


def _tool_vocab(profile: Profile, extra_tools: list[str]) -> set[str]:
    """Tool names: capitalised words (and dbt) of the skills inventory, minus generic words, plus
    profile.yaml cv.extra_tools."""
    vocab: set[str] = set(extra_tools)
    for name, body in profile.sections.items():
        if not name.startswith("Skills inventory"):
            continue
        for line in body.splitlines():
            for tok in re.findall(r"[A-Za-z][A-Za-z0-9]+", line.split(":", 1)[-1]):
                if (tok[0].isupper() or tok == "dbt") and tok not in _TOOL_STOP and len(tok) > 2:
                    vocab.add(tok)
    return vocab


def _has(tool: str, line: str) -> bool:
    return re.search(rf"\b{re.escape(tool)}\b", line) is not None


def mixed_tools(text: str, profile: Profile, user: UserProfile | None = None) -> list[str]:
    """Tools the letter attaches to the wrong case, by sentence:
    - a sentence naming a case (profile.yaml cv.cases: a client, a project, an employer): every
      tool in it must be on that case's profile lines;
    - a no-tool case (profile.yaml cv.no_tool_cases, e.g. a real-estate example) may not name any
      tool;
    - any other sentence: its tools must sit together on one line of the profile (a case, a job
      or a line of the skills inventory)."""
    cfg = _user(user).cv
    vocab = _tool_vocab(profile, cfg.extra_tools)
    no_tool = [re.compile(p, re.IGNORECASE) for p in cfg.no_tool_cases]
    cases = [(re.compile(c.sentence, re.IGNORECASE), c.profile_lines, c.section) for c in cfg.cases]
    case_lines, all_lines = [], []
    section_lines: dict[str, list[str]] = {}
    for name, body in profile.sections.items():
        if name.startswith(("Known gaps", "Applications log", "Contacts")):
            continue
        for ln in body.splitlines():
            if ln.lstrip().startswith("Stack named on"):
                continue
            all_lines.append(ln)
            if not name.startswith(_NOT_CASES):
                case_lines.append(ln)
                for _, _, sec in cases:
                    if sec and name.startswith(sec):
                        section_lines.setdefault(sec, []).append(ln)
    bad: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        named = {t for t in vocab if _has(t, sentence)}
        if not named:
            continue
        if any(rx.search(sentence) for rx in no_tool):
            bad |= named
            continue
        hit_cases = [(pat, sec) for rx, pat, sec in cases if rx.search(sentence)]
        hits = [pat for pat, _ in hit_cases]
        if hits:
            lines = [ln for ln in case_lines if any(re.search(p, ln, re.IGNORECASE) for p in hits)]
            for _, sec in hit_cases:
                if sec:
                    lines += section_lines.get(sec, [])
            bad |= {t for t in named if not any(_has(t, ln) for ln in lines)}
        elif len(named) > 1 and not any(all(_has(t, ln) for t in named) for ln in all_lines):
            bad |= named
    return sorted(bad)


def validate_cover(
    text: str,
    claims: list[Claim],
    profile: Profile,
    gaps: GapsConfig,
    user: UserProfile | None = None,
) -> list[Violation]:
    """The truth checks of the resume, applied to a cover letter. Codes are `cover_*`.

    A letter may state a real gap honestly, but a tool listed in gaps.yaml must not appear at all
    (same rule as the resume): the posting asks for it, the letter says nothing about it."""
    out: list[Violation] = []
    text = _BR.sub("", text)  # "Best regards,<br>" is the one tag a letter may carry

    scanned = "\n".join([text, *(c.resume_text for c in claims)])
    for g in gaps.gaps:
        mode = g.cover or g.mode
        if mode == "allow":
            continue
        if any(re.search(p, scanned, re.IGNORECASE) for p in g.patterns):
            if mode == "forbid":
                out.append(Violation("cover_gap_term", g.id, "retry"))
            else:
                out.append(Violation("cover_gap_review", g.id, "review"))

    for n in sorted(numbers_in(text) - profile.numbers)[:5]:
        out.append(Violation("cover_unsupported_number", n[:20], "retry"))

    if len(claims) < COVER_MIN_CLAIMS:
        out.append(Violation("cover_bad_claim", "count", "retry"))
    for i, c in enumerate(claims, 1):
        body = profile.sections.get(c.section)
        if body is None or not norm(c.profile_quote) or norm(c.profile_quote) not in norm(body):
            out.append(Violation("cover_bad_claim", str(i), "retry"))

    if len(text.split()) > COVER_MAX_WORDS:
        out.append(Violation("cover_structure", "too_long", "retry"))
    if re.search(r"^\s*(#|!\[)", text, re.MULTILINE):
        out.append(Violation("cover_structure", "headings", "retry"))
    mixed = mixed_tools(text, profile, user)
    if mixed:
        out.append(Violation("cover_mixed_tools", ",".join(mixed)[:60], "review"))
    if _EMOJI.search(text):
        out.append(Violation("cover_hygiene", "emoji", "retry"))
    if _RAW_TAG.search(text):
        out.append(Violation("cover_hygiene", "raw_html", "retry"))
    if _MONEY.search(text):
        out.append(Violation("cover_hygiene", "money", "retry"))
    for t in _walk(_MD.parse(text)):
        if t.type == "image":
            out.append(Violation("cover_hygiene", "image", "retry"))
            break

    emails, links = find_contacts(text)
    bad_mail = emails - profile.contacts.emails
    bad_link = {x for x in links if not contact_allowed(x, profile.contacts)}
    if bad_mail or bad_link:
        out.append(Violation("cover_foreign_contact", "", "review"))
    for t in _walk(_MD.parse(text)):
        if t.type == "link_open":
            href = str(t.attrGet("href") or "")
            if not (href.startswith("https://") or href.startswith("mailto:")):
                out.append(Violation("cover_hygiene", "non_https_link", "retry"))
                break
    return out


def needs_retry(violations: list[Violation]) -> bool:
    """Retry only if nothing needs the owner's eyes at once."""
    return bool(violations) and all(v.kind == "retry" for v in violations)


def feedback_text(violations: list[Violation], gaps: GapsConfig) -> str:
    """A fixed-template note for the next attempt. Only codes, gap names, digits and claim
    numbers go in: never text copied from the model's answer or the posting."""
    names = {g.id: g.source for g in gaps.gaps}
    lines = ["The previous draft was rejected by automatic checks. Fix exactly these problems:"]
    for v in violations:
        if v.code == "gap_term":
            lines.append(f"- It mentions a forbidden topic ({names.get(v.arg, v.arg)}). Remove it.")
        elif v.code == "cover_gap_term":
            lines.append(
                f"- The cover letter mentions a forbidden topic ({names.get(v.arg, v.arg)}). "
                "Remove it."
            )
        elif v.code == "cover_unsupported_number":
            lines.append(
                f"- Cover letter: the number {v.arg} is not in the profile. Remove it or use a "
                "profile number."
            )
        elif v.code == "cover_bad_claim":
            lines.append(
                f"- Cover letter claim {v.arg}: needs at least 2 claims, each with an exact "
                "section name and a verbatim profile_quote."
            )
        elif v.code == "cover_structure":
            lines.append(
                f"- Cover letter structure: {v.arg} (at most 300 words, plain paragraphs and "
                "bullets, no headings)."
            )
        elif v.code == "cover_mixed_tools":
            lines.append(
                f"- Cover letter: tools mixed across cases ({v.arg}). Name a tool only with the "
                "exact case it belongs to in the profile, or leave tools out."
            )
        elif v.code == "unsupported_number":
            lines.append(
                f"- The number {v.arg} is not in the profile. Remove it or use a profile number."
            )
        elif v.code == "bad_claim":
            lines.append(f"- Claim {v.arg}: the section name or the profile_quote is not exact.")
        elif v.code == "structure":
            lines.append(f"- Missing required structure: {v.arg}.")
        else:
            lines.append(f"- Fix: {v.code} ({v.arg}).")
    return "\n".join(lines)[:1900]
