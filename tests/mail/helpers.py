from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import httpx

from cvboost.http import HttpClient
from cvboost.mail.imap import ImapCreds, MailError
from tests.conftest import PUBLIC_RESOLVER

EMAILS = Path(__file__).parent.parent / "fixtures" / "email"
PASSWORD = "S3cr3t-imap-pw"
CREDS = ImapCreds("alerts@example.com", PASSWORD)


def eml(name: str) -> bytes:
    return (EMAILS / name).read_bytes()


class FakeMailbox:
    """In-memory mailbox; the journal records every call, so tests see what was fetched."""

    def __init__(self, messages: dict[int, bytes] | None = None, uidvalidity: int = 1):
        self.messages = dict(messages or {})
        self.uidvalidity = uidvalidity
        self.log: list[tuple] = []
        self.fail_fetch: set[int] = set()
        self.size_override: dict[int, int] = {}

    def add(self, uid: int, data: bytes) -> None:
        self.messages[uid] = data

    def factory(self, creds: ImapCreds) -> FakeMailbox:
        self.log.append(("factory", creds.user))
        return self

    def open(self) -> int:
        self.log.append(("open",))
        return self.uidvalidity

    def search_uids(self, min_uid: int, since: date) -> list[int]:
        self.log.append(("search", min_uid))
        # IMAP quirk: "n:*" always returns the last message, even when its UID < n
        uids = sorted(u for u in self.messages if u >= min_uid)
        if not uids and self.messages:
            uids = [max(self.messages)]
        return uids

    def sizes(self, uids: Sequence[int]) -> dict[int, int]:
        self.log.append(("sizes", tuple(uids)))
        return {u: self.size_override.get(u, len(self.messages[u])) for u in uids}

    def fetch(self, uid: int) -> bytes:
        self.log.append(("fetch", uid))
        if uid in self.fail_fetch:
            raise MailError("connection lost")
        return self.messages[uid]

    def close(self) -> None:
        self.log.append(("close",))

    def fetched(self) -> list[int]:
        return [e[1] for e in self.log if e[0] == "fetch"]


class NoHttp:
    """HTTP client that fails the test on any request (links in mails must never be opened)."""

    def __init__(self):
        self.requests: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            self.requests.append(str(req.url))
            raise AssertionError(f"HTTP request from an e-mail run: {req.url}")

        self.client = HttpClient(
            httpx.MockTransport(handler), sleep=lambda s: None, resolver=PUBLIC_RESOLVER
        )


def subject_ref(text: str) -> list[str]:
    return re.findall(r'<email ref="(e\d+)"', text)


def with_auth(raw: bytes, header: str | None) -> bytes:
    """Replace the Authentication-Results header at the BYTES level: the value reaches the parser
    exactly as written (an encoded word stays `=?...?=`, as in a real attack)."""
    head, sep, body = raw.partition(b"\n\n")
    lines = head.split(b"\n")
    kept: list[bytes] = []
    skipping = False
    for ln in lines:
        if ln[:1] in (b" ", b"\t"):
            if not skipping:
                kept.append(ln)
            continue
        skipping = ln.lower().startswith(b"authentication-results:")
        if not skipping:
            kept.append(ln)
    if header:
        kept.insert(0, b"Authentication-Results: " + header.encode("utf-8"))
    return b"\n".join(kept) + sep + body
