"""Dispatcher: which parser or handler gets an e-mail. Routing uses the ORIGINAL sender
(`ParsedEmail.from_addr`, already unwrapped from forwards) and is only a hint: parsers validate
the content, and job links are rebuilt from recognised ids, so a spoofed sender can at worst add
a real platform job.

External handlers (contract with the mini-CRM, #33)
---------------------------------------------------
    register_handler(sender_match, handler)

    sender_match  an exact lowercase address ("hit-reply@linkedin.com"), a domain suffix written
                  "@linkedin.com" (the domain and its subdomains), a compiled regex (searched in
                  the address) or a callable (addr: str) -> bool
    handler       handler(parsed: ParsedEmail, store: Store, now: datetime) -> HandlerResult

A message whose sender matches is NOT parsed for jobs. Trust comes first (`parsed.trust`):
  "verified"        the From domain is authenticated by our own mail server (dmarc=pass or an
                    aligned dkim=pass in the topmost Authentication-Results of the trusted server);
  "owner_forwarded" unwrapped from a forward by one of the owner's own authenticated addresses
                    (the owner vouches for the content, the original sender is NOT verified);
  "untrusted"       anything else (spoofable From).
For "untrusted" mail the handler is NOT called: the row is recorded as status "crm_untrusted"
and nothing else happens (no side effects). Otherwise the handler is called with `parsed` (read
`parsed.trust` to decide what it may do, e.g. never auto-change CRM state from "owner_forwarded"
without care); its `HandlerResult.status` must be "crm", "ignored" or "crm_untrusted", any other
value is recorded as "crm" with the note "bad status". Handlers must be idempotent: a message is
handed over once per recorded row, but a crash between the handler and the row means a retry.
The most recently registered matching handler wins. The built-in default for the LinkedIn
messaging senders
(DEFAULT_CRM_SENDERS) only records status "crm" with the note "no handler"; the mini-CRM replaces
it by registering its own handler for the same addresses at import time (import its module from
`cvboost/mail/parsers/__init__.py` or before `EmailAlertsConnector` runs). A handler that raises
is recorded as status "crm" with the note "handler error: <type>"; it never stops the run.
`ParsedEmail` lives in `cvboost.mail.message` and is also importable from here.

Statuses produced here: parsed | needs_llm | ignored | dkim_fail | crm | crm_untrusted.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from cvboost.llm.sanitize import normalize_text, truncate
from cvboost.mail import canonical
from cvboost.mail.mask import mask_contacts
from cvboost.mail.message import ParsedEmail
from cvboost.mail.parsers import cards, dice, indeed, linkedin
from cvboost.mail.parsers.base import HandlerResult, ParsedJob

if TYPE_CHECKING:
    from cvboost.storage.db import Store

log = logging.getLogger("cvboost.mail")

SenderMatch = str | re.Pattern[str] | Callable[[str], bool]
Handler = Callable[[ParsedEmail, "Store", datetime], HandlerResult]

REQUIRE_DKIM = True  # platform mails must be `verified` (or owner_forwarded) to be parsed
HANDLER_STATUSES = frozenset({"crm", "ignored", "crm_untrusted"})
LLM_INPUT_MAX = 6000
PARSER_VERSION = "1"

DEFAULT_CRM_SENDERS = (
    "hit-reply@linkedin.com",
    "inmail-hit-reply@linkedin.com",
    "messaging-digest-noreply@linkedin.com",
    "invitations@linkedin.com",
)

# mails that are never jobs when they come from an unknown sender (account / security / billing)
IGNORE_SUBJECT = re.compile(
    r"password|\bverif|security|sign[- ]?in|log[- ]?in|one-time|\bcode\b|c[oó]digo|invoice|"
    r"receipt|payment|welcome|activate|passkey|chave de acesso|inicie sess|\bupgrade\b|"
    r"project alert limit|unsubscribe|confirm your",
    re.IGNORECASE,
)

_PREFIX_SENDERS = {"jobalerts-noreply", "jobs-noreply", "jobalert-noreply", "jobs-listings"}


@dataclass(frozen=True)
class Platform:
    name: str
    domains: tuple[str, ...]
    is_alert_sender: Callable[[str], bool]
    parse: Callable[[ParsedEmail], list[ParsedJob]]


def _domain_is(addr: str, *domains: str) -> bool:
    host = addr.rpartition("@")[2]
    return any(host == d or host.endswith("." + d) for d in domains)


PLATFORMS: tuple[Platform, ...] = (
    Platform(
        "linkedin",
        ("linkedin.com",),
        lambda a: a.partition("@")[0] in _PREFIX_SENDERS and _domain_is(a, "linkedin.com"),
        linkedin.parse,
    ),
    Platform("dice", ("dice.com",), lambda a: _domain_is(a, "connect.dice.com"), dice.parse),
    Platform(
        "indeed",
        ("indeed.com",),
        lambda a: _domain_is(a, "jobalert.indeed.com") or a == "alert@indeed.com",
        indeed.parse,
    ),
    # freelancermap sends account and alert mails from the same domain: the subject filter
    # (IGNORE_SUBJECT) separates them, so every sender is allowed here
    Platform(
        "freelancermap",
        ("freelancermap.com", "freelancermap.de"),
        lambda a: _domain_is(a, "freelancermap.com", "freelancermap.de"),
        lambda p: cards.parse_cards(p, "freelancermap"),
    ),
)


# --- external handlers -------------------------------------------------------------------------

_handlers: list[tuple[SenderMatch, Handler]] = []


def _matches(match: SenderMatch, addr: str) -> bool:
    if isinstance(match, str):
        m = match.lower()
        return _domain_is(addr, m[1:]) if m.startswith("@") else addr == m
    if isinstance(match, re.Pattern):
        return match.search(addr) is not None
    return bool(match(addr))


def register_handler(sender_match: SenderMatch, handler: Handler) -> None:
    """Route mails from matching senders to `handler` instead of the job parsers (module doc)."""
    _handlers.insert(0, (sender_match, handler))  # the latest registration wins


def unregister_handler(handler: Handler) -> None:
    _handlers[:] = [(m, h) for m, h in _handlers if h is not handler]


def _default_crm(parsed: ParsedEmail, store: Store, now: datetime) -> HandlerResult:
    return HandlerResult("crm", "no handler")


for _addr in DEFAULT_CRM_SENDERS:
    _handlers.append((_addr, _default_crm))  # registered last-in-list: any later one wins


# --- classification ----------------------------------------------------------------------------


@dataclass
class Classification:
    status: str
    platform: str | None = None
    jobs: list[ParsedJob] = field(default_factory=list)
    note: str = ""
    llm_input: str | None = None
    job_links: list[str] = field(default_factory=list)  # canonical links, for the LLM's link_ref


def platform_of(addr: str) -> Platform | None:
    return next((p for p in PLATFORMS if _domain_is(addr, *p.domains)), None)


def prepare_llm_input(p: ParsedEmail, links: list[str], max_chars: int = LLM_INPUT_MAX) -> str:
    """Text for the LLM fallback: links replaced by [job link N] / [link], invisible characters
    removed, head+tail truncated. Stored as plain text; escaped when the prompt is built."""
    numbered = {u: i for i, u in enumerate(links, 1)}

    def repl(m: re.Match[str]) -> str:
        link = canonical.canonicalise(m.group(0))
        return f"[job link {numbered[link.url]}]" if link and link.url in numbered else "[link]"

    # normalise FIRST (NFKC folds fullwidth forms, Cf/zero-width characters are removed), so an
    # obfuscated address or phone number cannot slip past the masks
    body = re.sub(r"https?://[^\s<>\"')\]]{1,2048}", repl, normalize_text(p.text))
    text = mask_contacts(normalize_text(f"Subject: {p.subject}\n\n{body}"))
    return truncate(text, max_chars, max_chars * 3 // 4, max_chars // 4)[:8000]


def classify(
    p: ParsedEmail, store: Store, now: datetime, *, require_dkim: bool | None = None
) -> Classification:
    """Decide what a message is. Never fetches anything."""
    addr = p.from_addr
    if not addr:
        return Classification("ignored", note="no sender")
    for match, handler in _handlers:
        if _matches(match, addr):
            if p.trust == "untrusted":
                return Classification("crm_untrusted", note="sender not authenticated")
            try:
                res = handler(p, store, now)
            except Exception as exc:  # a broken handler must not stop the mailbox run
                log.warning("mail handler failed: %s", type(exc).__name__)
                return Classification("crm", note=f"handler error: {type(exc).__name__}")
            if res.status not in HANDLER_STATUSES:
                return Classification("crm", note="bad status")
            return Classification(res.status, note=res.note[:200])
    platform = platform_of(addr)
    if platform is not None:
        if not platform.is_alert_sender(addr):
            return Classification("ignored", platform.name, note="not an alert sender")
        if IGNORE_SUBJECT.search(p.subject) and platform.name == "freelancermap":
            return Classification("ignored", platform.name, note="account mail")
        dkim_needed = REQUIRE_DKIM if require_dkim is None else require_dkim
        if dkim_needed and p.trust == "untrusted":
            log.warning("spoofed or unauthenticated %s sender: mail ignored", platform.name)
            return Classification("dkim_fail", platform.name, note="sender not authenticated")
        jobs = platform.parse(p)
        if jobs:
            return Classification(
                "parsed", platform.name, jobs, note=_count_note(p, platform.name, len(jobs))
            )
        return _needs_llm(p, platform.name)
    if IGNORE_SUBJECT.search(p.subject):
        return Classification("ignored", note="account-like subject")
    return _needs_llm(p, "recruiter")


def _count_note(p: ParsedEmail, platform: str, n: int) -> str:
    """'N jobs / M links' (M = distinct job links of this platform in the mail); warns if N < M."""
    m = len(canonical.canonical_links(p.links, platform))
    if not m:  # platform without canonical links (Indeed): only the parsed count is known
        if n == 0:
            log.warning("%s mail: no jobs parsed", platform)
        return f"{n} jobs"
    if n < m:
        log.warning("%s mail: %d jobs parsed but %d job links found", platform, n, m)
    return f"{n} jobs / {m} links"


def _needs_llm(p: ParsedEmail, platform: str) -> Classification:
    links = [lk.url for lk in canonical.canonical_links(p.links)][:20]
    return Classification(
        "needs_llm", platform, llm_input=prepare_llm_input(p, links), job_links=links
    )


__all__ = [
    "Classification",
    "HandlerResult",
    "ParsedEmail",
    "ParsedJob",
    "classify",
    "register_handler",
    "unregister_handler",
]
