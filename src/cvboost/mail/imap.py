"""Read-only IMAP access to the alerts mailbox (stdlib imaplib, no extra dependency).

Guarantees: the mailbox is opened with EXAMINE (select readonly), bodies are fetched with
BODY.PEEK (nothing is marked read), no STORE / COPY / EXPUNGE / APPEND anywhere, imaplib's
debug output (which would print the LOGIN command with the password) is never switched on, and
every error is re-raised as `MailError` with the secret redacted and no exception chain.
"""

from __future__ import annotations

import imaplib
import re
import ssl
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from cvboost.notify.redact import redact

TIMEOUT_S = 30
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_UID = re.compile(rb"UID (\d+)")
_SIZE = re.compile(rb"RFC822\.SIZE (\d+)")
SIZE_CHUNK = 100


class MailError(Exception):
    """Connection, login or protocol failure. The text is redacted and cut to 200 chars."""


@dataclass(frozen=True)
class ImapCreds:
    user: str
    password: str = field(repr=False)
    host: str = "imap.gmail.com"
    mailbox: str = "INBOX"

    def __repr__(self) -> str:
        return f"ImapCreds(user={self.user!r}, host={self.host!r}, mailbox={self.mailbox!r})"


class Mailbox(Protocol):
    """What the connector needs from a mailbox. Tests use an in-memory fake."""

    def open(self) -> int:
        """Connect, log in, select read-only; returns UIDVALIDITY."""

    def search_uids(self, min_uid: int, since: date) -> list[int]:
        """UIDs >= min_uid of messages since `since`, ascending."""

    def sizes(self, uids: Sequence[int]) -> dict[int, int]:
        """RFC822.SIZE per UID, without downloading bodies."""

    def fetch(self, uid: int) -> bytes:
        """The raw message (BODY.PEEK[])."""

    def close(self) -> None: ...


def imap_date(d: date) -> str:
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"  # locale independent


class ImapMailbox:
    def __init__(self, creds: ImapCreds):
        self._creds = creds
        self._conn: imaplib.IMAP4_SSL | None = None

    def _fail(self, exc: BaseException) -> MailError:
        msg = redact(f"{type(exc).__name__}: {exc}", self._creds.password, self._creds.user)
        return MailError(msg[:200])

    def open(self) -> int:
        c = self._creds
        try:
            conn = imaplib.IMAP4_SSL(
                c.host, 993, ssl_context=ssl.create_default_context(), timeout=TIMEOUT_S
            )
            self._conn = conn
            conn.login(c.user, c.password)
            typ, _ = conn.select(c.mailbox, readonly=True)
            if typ != "OK":
                raise MailError("cannot select the mailbox")
            typ, data = conn.response("UIDVALIDITY")
            vals = [d for d in (data or []) if d]
            if not vals:
                raise MailError("server did not report UIDVALIDITY")
            return int(vals[0])
        except MailError:
            raise
        except (imaplib.IMAP4.error, OSError, ssl.SSLError, ValueError) as exc:
            raise self._fail(exc) from None

    def _uid(self, *args: str) -> list:
        assert self._conn is not None
        try:
            typ, data = self._conn.uid(*args)
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise self._fail(exc) from None
        if typ != "OK":
            raise MailError(f"IMAP {args[0]} failed")
        return data or []

    def search_uids(self, min_uid: int, since: date) -> list[int]:
        data = self._uid("SEARCH", "UID", f"{max(min_uid, 1)}:*", "SINCE", imap_date(since))
        uids = (data[0] or b"").split() if data else []
        return sorted(int(u) for u in uids if int(u) >= min_uid)

    def sizes(self, uids: Sequence[int]) -> dict[int, int]:
        out: dict[int, int] = {}
        for i in range(0, len(uids), SIZE_CHUNK):
            chunk = ",".join(str(u) for u in uids[i : i + SIZE_CHUNK])
            for item in self._uid("FETCH", chunk, "(RFC822.SIZE)"):
                line = item[0] if isinstance(item, tuple) else item
                if not isinstance(line, bytes):
                    continue
                u, s = _UID.search(line), _SIZE.search(line)
                if u and s:
                    out[int(u.group(1))] = int(s.group(1))
        return out

    def fetch(self, uid: int) -> bytes:
        for item in self._uid("FETCH", str(uid), "(BODY.PEEK[])"):
            if isinstance(item, tuple) and isinstance(item[1], bytes):
                return item[1]
        raise MailError(f"message uid:{uid} not returned")

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:  # no CLOSE: nothing to expunge, the session is read-only
            conn.logout()
        except Exception:  # noqa: S110 - best effort
            pass
