from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from cvboost.crm.parse import _URL

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "email"
ALL = sorted(FIXTURES.glob("linkedin_crm_*.eml"))


@dataclass
class Parsed:
    """Same fields as #17's ParsedEmail."""

    from_addr: str
    subject: str
    received_at: datetime
    message_id: str | None
    text: str
    html: str | None
    links: list[str] = field(default_factory=list)
    trust: str = "verified"


def load(name: str) -> Parsed:
    msg = BytesParser(policy=policy.default).parsebytes((FIXTURES / name).read_bytes())
    text = msg.get_body(preferencelist=("plain",))
    html = msg.get_body(preferencelist=("html",))
    t = text.get_content() if text else ""
    h = html.get_content() if html else None
    return Parsed(
        from_addr=parseaddr(msg["From"])[1],
        subject=str(msg["Subject"]),
        received_at=parsedate_to_datetime(msg["Date"]),
        message_id=str(msg["Message-ID"]).strip("<>"),
        text=t,
        html=h,
        links=_URL.findall(t + (h or "")),
    )
