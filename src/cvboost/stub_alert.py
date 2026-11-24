"""Stub e-mail-alert jobs (#68): a LinkedIn/Indeed alert has no description, so the model guesses
and rejects on doubt about location. Such a verdict becomes «keep + check_on_platform».

Applied only to a verdict that is being written now (a first scoring). It changes neither the
prompt (so the prompt version and every cache key stay) nor stored verdicts: already scored
jobs are never queued again.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from cvboost.llm.schema import JobVerdict

FLAG = "check_on_platform"
PLATFORMS = {
    "linkedin": "LinkedIn",
    "indeed": "Indeed",
    "dice": "Dice",
    "freelancermap": "Freelancermap",
}
_STUB_MARKERS = ("the full description is on the platform", "(from an e-mail)")

_LOCATION = re.compile(r"remote|location|on-?site|city|hybrid|office|relocat|country", re.I)
_UNCERTAIN = re.compile(
    r"not (?:stated|specified|clear|confirmed|mentioned|indicated|explicit)|unclear|unknown|"
    r"unspecified|no (?:description|details|information|mention)|stub|cannot|can't|"
    r"unable|insufficient|lacks?|without|absent|missing|no remote",
    re.I,
)
# explicit blockers and other reasons to reject stay a rejection
_BLOCKERS = re.compile(
    r"\bus\b|u\.s\.|usa|united states|\buk\b|canada|citizen|work authori[sz]|visa|green card|"
    r"security clearance|\bw-?2\b|on-?site only|on-?site\b|in-?office|hybrid|relocat|"
    r"must (?:live|be (?:based|located))|only (?:in|for)|"
    r"rate|salary|below|campaign|media buy|junior|intern|not a fit|mismatch|unrelated|"
    r"wrong (?:role|field|stack)|sales|manager|prompt_injection",
    re.I,
)


def platform_of(sources: Sequence[str]) -> str | None:
    """«LinkedIn» for an e-mail-only group from a known job board; None otherwise."""
    if not sources or not all(s.startswith("email:") for s in sources):
        return None
    return PLATFORMS.get(sources[0].split(":", 1)[1])


def is_stub(sources: Sequence[str], description: str, has_extra: bool) -> bool:
    if has_extra or platform_of(sources) is None:
        return False
    d = description.lower()
    return any(m in d for m in _STUB_MARKERS)


def apply(
    verdict: JobVerdict,
    *,
    sources: Sequence[str],
    description: str,
    has_extra: bool,
    title: str,
    location: str | None,
) -> JobVerdict:
    """`verdict` unchanged unless it is a rejection of a stub for location doubt alone."""
    if verdict.eligible or FLAG in verdict.red_flags:
        return verdict
    if not is_stub(sources, description, has_extra) or verdict.profile == "none":
        return verdict
    reason = verdict.eligibility_reason
    if not (_LOCATION.search(reason) and _UNCERTAIN.search(reason)):
        return verdict
    own = " ".join([title, location or "", reason, " ".join(verdict.red_flags)])
    if _BLOCKERS.search(own):
        return verdict
    return verdict.model_copy(
        update={
            "eligible": True,
            "red_flags": [FLAG, *verdict.red_flags][:6],
            "eligibility_reason": ("Stub e-mail alert, location unverified: " + reason)[:300],
        }
    )
