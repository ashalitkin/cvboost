"""The CRM mail handler for LinkedIn notifications, registered with #17's dispatcher.

Importing this module registers `handle_email` for LINKEDIN_SENDERS (the dispatcher's default
"no handler" entry for the same addresses is replaced: the latest registration wins). The
connector module imports it, so every entry point that can run the mailbox has it.

Defence in depth: the registry already skips untrusted mail; the handler refuses anything that
is neither "verified" nor "owner_forwarded" as well. It only ADDS events to the history; it is
idempotent (events are deduplicated by message id, or by kind/time/snippet when the mail has no
id), so a retry after a crash between the handler and the email_messages row changes nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime

from cvboost.crm.parse import LINKEDIN_SENDERS, parse_linkedin
from cvboost.crm.store import record_event
from cvboost.mail.message import ParsedEmail
from cvboost.mail.parsers.base import HandlerResult
from cvboost.mail.parsers.registry import register_handler

log = logging.getLogger("cvboost.crm")

__all__ = ["LINKEDIN_SENDERS", "handle_email", "matches_sender"]

TRUSTED = ("verified", "owner_forwarded")
MAX_EVENTS = 20  # events stored from one mail; the rest is only counted in the note


def matches_sender(from_addr: str) -> bool:
    addr = from_addr.strip().lower()
    if "<" in addr:
        addr = addr.rsplit("<", 1)[1].split(">")[0].strip()
    return addr in LINKEDIN_SENDERS


def handle_email(parsed: ParsedEmail, store, now: datetime) -> HandlerResult:
    """Parse one LinkedIn mail and record its events. `store` is a cvboost Store."""
    if parsed.trust not in TRUSTED:
        return HandlerResult("crm_untrusted", "sender not authenticated")
    events = parse_linkedin(parsed)
    skipped = max(0, len(events) - MAX_EVENTS)
    events = events[:MAX_EVENTS]
    added = 0
    with store.transaction():
        for ev in events:
            added += record_event(store.conn, ev, parsed.received_at, parsed.message_id, now)
    note = f"{added} new / {len(events)} event(s)"
    return HandlerResult("crm", note + (f", {skipped} over the cap skipped" if skipped else ""))


for _addr in LINKEDIN_SENDERS:
    register_handler(_addr, handle_email)
