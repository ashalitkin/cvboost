"""CRM constants and the parsed-event type. The mail types are #17's own (re-exported here)."""

from __future__ import annotations

from dataclasses import dataclass

from cvboost.mail.message import ParsedEmail
from cvboost.mail.parsers.base import HandlerResult

__all__ = ["HandlerResult", "ParsedEmail"]

# the contact kinds the owner can pick, and the columns of /crm
CONTACT_KINDS = ("recruiter", "client", "colleague", "other")
KIND_LABELS = {
    "recruiter": "Recruiter",
    "client": "Client",  # kind_labels() puts profile.yaml crm.client_label here
    "colleague": "Colleague",
    "other": "Other",
}


def kind_labels() -> dict[str, str]:
    """KIND_LABELS with the owner's wording of "client" (config/profile.yaml, crm.client_label)."""
    from cvboost.llm.prompts import registry

    label = registry.client_label()
    return {**KIND_LABELS, "client": label} if label else dict(KIND_LABELS)


COLUMNS = ("inbox", "leads", "talk")
COLUMN_LABELS = {"inbox": "Inbox", "leads": "Leads", "talk": "Talking"}
EVENT_KINDS = ("invitation_accepted", "message", "message_accepted", "other")
EVENT_LABELS = {
    "invitation_accepted": "Accepted the invitation",
    "message": "Message",
    "message_accepted": "InMail accepted (reply)",
    "other": "Other LinkedIn email",
}
NOTE_MAX = 2000
CONTEXT_MAX = 4000
SNIPPET_MAX = 500
STEP_MAX = 300


@dataclass(frozen=True)
class ParsedEvent:
    """One LinkedIn notification reduced to a contact and what happened."""

    kind: str  # invitation_accepted | message | message_accepted | other
    name: str
    headline: str | None = None
    company: str | None = None
    profile_url: str | None = None
    view_url: str | None = None
    snippet: str = ""
    index: int = 0  # position inside a digest (part of the dedupe key)
