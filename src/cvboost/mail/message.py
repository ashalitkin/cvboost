"""RFC 822 bytes -> `ParsedEmail` (the public contract shared with the mini-CRM, #33).

Everything in a message is untrusted: the sender can be spoofed, the text can carry prompt
injections, links are never opened. This module only decodes and unwraps.

Forwards are unwrapped ONLY when the outer From is one of the configured forwarder addresses
(the owner's own; CVBOOST_MAIL_FORWARDERS), so that routing uses the ORIGINAL sender:
  (a) "Forward as attachment": every message/rfc822 part becomes its own ParsedEmail;
  (b) an inline forward ("---------- Forwarded message ---------" + From/Date/Subject header
      lines): the original From/Date/Subject and the forwarded body are extracted.
For an unwrapped message `from_addr`, `received_at` and `message_id` are the inner ones and
`forwarded` is True; `outer_from` keeps the (equally untrusted) forwarder. A forwarded message
is `trust == "owner_forwarded"` (the owner vouches for it; the original sender is not verified).
"""

from __future__ import annotations

import email
import logging
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from cvboost.textutil import html_to_text

MAX_TEXT_CHARS = 500_000
log = logging.getLogger("cvboost.mail")
MAX_PARTS = 40  # MIME parts looked at per message
MAX_LINKS = 400
MAX_HREF_CHARS = 2048
MAX_INNER_MESSAGES = 50  # attached messages taken from one forward-as-attachment mail

_URL = re.compile(r"https?://[^\s<>\"'\)\]]{1,2048}")
_FWD_MARK = re.compile(
    r"^[ \t>]*-{3,}\s*(?:Forwarded message|Original Message|Forwarded Message)\s*-{3,}[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_HDR_LINE = re.compile(
    r"^[ \t>]*(From|Date|Subject|Sent|To|Cc|Reply-To):[ \t]*(.*)$", re.IGNORECASE
)
TRUSTED_AUTHSERV = "mx.google.com"  # the receiving server whose Authentication-Results we believe
_GMAIL_DATE = re.compile(
    r"(?:[A-Za-z]{3},?\s+)?([A-Za-z]{3,9})\s+(\d{1,2}),\s+(\d{4})(?:\s+at\s+(\d{1,2}):(\d{2})"
    r"(?::(\d{2}))?\s*([AP]M)?)?",
    re.IGNORECASE,
)
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}


@dataclass
class ParsedEmail:
    """One e-mail as the parsers and handlers see it. Public contract: do not rename or reorder
    the first seven fields.

    from_addr    lowercase address of the original sender (inner one for an unwrapped forward)
    subject      decoded subject, single line
    received_at  the message's own Date (UTC); None if missing or unparsable
    message_id   inner Message-ID without angle brackets, None if absent
    text         plain-text body (HTML converted when there is no text/plain), <= 500k chars
    html         HTML body or ""
    links        every http(s) URL found in the HTML hrefs and the text, in order, deduplicated
    """

    from_addr: str
    subject: str
    received_at: datetime | None
    message_id: str | None
    text: str
    html: str
    links: list[str] = field(default_factory=list)
    # extras (default values: constructing a ParsedEmail with the seven contract fields works)
    from_name: str = ""
    forwarded: bool = False
    outer_from: str | None = None  # the forwarder's address (untrusted), for unwrapped messages
    dkim_domains: tuple[str, ...] = ()  # domains with dkim=pass (trusted authserv-id only)
    # "verified": the From domain is authenticated (ONLY exactly one dmarc=pass with related
    # header.from decides, read from
    # the Authentication-Results of OUR mail server); "owner_forwarded": unwrapped from a
    # forward by one of the owner's own authenticated addresses (the owner vouches for it, the
    # original sender is NOT verified); "untrusted": anything else.
    trust: str = "untrusted"

    @property
    def verified(self) -> bool:
        return self.trust == "verified"

    @property
    def from_domain(self) -> str:
        return self.from_addr.rpartition("@")[2]


def _decode_part(part: Message) -> str:
    try:
        content = part.get_content()  # type: ignore[attr-defined]
        if isinstance(content, str):
            return content
    except Exception:  # unknown charset, broken encoding: decode leniently
        pass
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return ""


def _bodies(msg: Message) -> tuple[str, str]:
    """(text, html) of a message, skipping attachments."""
    text = html = ""
    for n, part in enumerate(msg.walk()):
        if n >= MAX_PARTS:
            break
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and not text:
            text = _decode_part(part)
        elif ctype == "text/html" and not html:
            html = _decode_part(part)
    if not text and html:
        text = html_to_text(html)
    text, html = text.replace("\r\n", "\n").replace("\r", "\n"), html.replace("\r\n", "\n")
    return text[:MAX_TEXT_CHARS], html[:MAX_TEXT_CHARS]


def extract_links(text: str, html: str) -> list[str]:
    out: dict[str, None] = {}
    if html:
        try:
            for a in HTMLParser(html).css("a[href]"):
                href = (a.attributes.get("href") or "").strip()
                if href.lower().startswith(("http://", "https://")):
                    out.setdefault(href[:MAX_HREF_CHARS], None)
        except Exception:  # a hostile HTML document must not stop the run
            pass
    for m in _URL.finditer(text):
        out.setdefault(m.group(0).rstrip(".,;"), None)
    return list(out)[:MAX_LINKS]


def _addr(value: str | None) -> tuple[str, str]:
    name, addr = parseaddr(str(value or ""))
    return name.strip()[:200], addr.strip().lower()[:320]


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        dt = _gmail_date(str(value))
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _gmail_date(value: str) -> datetime | None:
    """'Mon, Nov 9, 2026 at 8:16 PM' (Gmail's inline forward header; the zone is not given: UTC)."""
    m = _GMAIL_DATE.search(value)
    if not m or m.group(1)[:3].lower() not in _MONTHS:
        return None
    hour = int(m.group(4) or 0)
    if m.group(7):
        hour = hour % 12 + (12 if m.group(7).upper() == "PM" else 0)
    try:
        return datetime(
            int(m.group(3)),
            _MONTHS[m.group(1)[:3].lower()],
            int(m.group(2)),
            hour,
            int(m.group(5) or 0),
            int(m.group(6) or 0),
            tzinfo=UTC,
        )
    except ValueError:
        return None


def _message_id(msg: Message) -> str | None:
    mid = str(msg.get("Message-ID") or "").strip().strip("<>").strip()
    return mid[:300] or None


@dataclass(frozen=True)
class AuthResults:
    """Clauses of one Authentication-Results header: (method, result, {ptype.prop: value}).
    Only unquoted property values are kept (a quoted-string is attacker-controlled text, e.g. an
    envelope local part). `invalid` = the header could not be lexed cleanly: it proves nothing."""

    clauses: tuple[tuple[str, str, dict[str, str]], ...]
    invalid: bool = False

    def _dmarc(self) -> list[tuple[str, str, dict[str, str]]]:
        return [c for c in self.clauses if c[0] == "dmarc"]

    def dmarc_from(self) -> str | None:
        """header.from of THE dmarc clause when there is exactly one and it is a pass; else None."""
        dmarc = self._dmarc()
        if self.invalid or len(dmarc) != 1 or dmarc[0][1] != "pass":
            return None
        return (dmarc[0][2].get("header.from") or "").lower() or None

    def dkim_domains(self) -> tuple[str, ...]:
        out = []
        for method, result, props in self.clauses:
            if method == "dkim" and result == "pass":
                d = props.get("header.d") or props.get("header.i", "").rpartition("@")[2]
                if d:
                    out.append(d.lower())
        return tuple(dict.fromkeys(out))

    def authenticates(self, from_domain: str) -> bool:
        """The From domain is authenticated: ONLY dmarc decides. Exactly one dmarc clause, a pass,
        with header.from related to the From domain. No dmarc clause (dkim or spf alone) means
        not authenticated: the domains we rely on publish DMARC."""
        if self.invalid or not from_domain:
            return False
        hf = self.dmarc_from()
        return bool(hf) and _related(hf, from_domain)


_WORD_END = frozenset(' \t\r\n;"(')
_PTYPE_PROP = re.compile(r"[a-z0-9-]{1,20}\.[a-z0-9_-]{1,40}")
_METHOD = re.compile(r"[a-z0-9-]{1,20}")
MAX_AUTH_CHARS = 8000


def _skip_quoted(text: str, i: int) -> tuple[str, int] | None:
    """text[i] is '"': the unescaped content and the index after the closing quote, or None if
    the quoted-string is not closed."""
    n = len(text)
    i += 1
    buf = []
    while i < n:
        c = text[i]
        if c == "\\":
            if i + 1 >= n:
                return None
            buf.append(text[i + 1])
            i += 2
            continue
        if c == '"':
            return "".join(buf), i + 1
        buf.append(c)
        i += 1
    return None


def _lex_auth(text: str) -> list[tuple[str, str]] | None:
    """RFC 8601 lexer: ('semi', ';'), ('qs', content) and ('word', run). Whitespace and nested
    (comments) are dropped; a ';', '=' or ')' inside a quoted-string (also inside a comment) or a
    comment never splits anything. FAILS CLOSED: an unclosed comment or quote, a stray ')' or a
    header longer than MAX_AUTH_CHARS gives None (no truncation, nothing is guessed)."""
    if len(text) > MAX_AUTH_CHARS:
        return None
    toks: list[tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == ";":
            toks.append(("semi", ";"))
            i += 1
        elif c == ")":
            return None
        elif c == "(":
            depth = 0
            while True:
                if i >= n:
                    return None  # unclosed comment
                ch = text[i]
                if ch == "\\":
                    i += 2
                elif ch == '"':  # quoted strings inside comments are atomic (envelope addresses)
                    q = _skip_quoted(text, i)
                    if q is None:
                        return None
                    i = q[1]
                else:
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            i += 1
                            break
                    i += 1
        elif c == '"':
            q = _skip_quoted(text, i)
            if q is None:
                return None
            toks.append(("qs", q[0]))
            i = q[1]
        else:
            j = i
            while j < n and text[j] not in _WORD_END:
                if text[j] == ")":
                    return None
                j += 1
            toks.append(("word", text[i:j]))
            i = j
    return toks


def _clauses(toks: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    out: list[list[tuple[str, str]]] = [[]]
    for t in toks:
        if t[0] == "semi":
            out.append([])
        else:
            out[-1].append(t)
    return out


def parse_auth_results(msg: Message, authserv: str = TRUSTED_AUTHSERV) -> AuthResults | None:
    """The TOPMOST Authentication-Results whose authserv-id is `authserv` (headers of other
    servers are ignored; None if there is none), lexed per RFC 8601 into method clauses. The RAW
    header value is lexed (not the decoded one): RFC 2047 encoded words (`=?`) could smuggle
    clause text past the lexer after decoding, so any `=?` in the trusted header makes it invalid.
    Quoted-strings and (comments) are atomic: they never end a clause or yield a method or
    property token, so text an attacker controls (the envelope sender Gmail copies into the spf
    comment and smtp.mailfrom) cannot forge a clause. Quoted property values are discarded.
    A header of the trusted server that does not lex cleanly (unclosed quote or comment, stray
    parenthesis, too long) is returned as `invalid` and authenticates nothing."""
    raw_headers = [v for k, v in msg.raw_items() if k.lower() == "authentication-results"]
    for header in raw_headers:
        text = str(header)
        toks = None if "=?" in text else _lex_auth(text)
        if toks is None:
            if text.lstrip().lower().startswith(authserv.lower()):
                return AuthResults((), invalid=True)
            continue
        parts = _clauses(toks)
        ident = parts[0][0][1].lower() if parts[0] and parts[0][0][0] == "word" else ""
        if ident != authserv.lower():
            continue
        clauses = []
        for part in parts[1:]:
            if not part or part[0][0] != "word":
                continue
            method, eq, result = part[0][1].partition("=")
            if not eq or not _METHOD.fullmatch(method.lower()) or not result.isalpha():
                continue
            props: dict[str, str] = {}
            k = 1
            while k < len(part):
                kind, word = part[k]
                k += 1
                if kind != "word":
                    continue
                key, eq2, value = word.partition("=")
                key = key.lower()
                if not eq2 or not _PTYPE_PROP.fullmatch(key):
                    continue
                if not value and k < len(part) and part[k][0] == "qs":
                    k += 1  # a quoted value: skipped, never trusted
                    continue
                if value:
                    props.setdefault(key, value)
            clauses.append((method.lower(), result.lower(), props))
        return AuthResults(tuple(clauses))
    return None


def _related(a: str, b: str) -> bool:
    a, b = a.lower().strip("."), b.lower().strip(".")
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def _direct_trust(auth: AuthResults | None, from_domain: str) -> str:
    return "verified" if auth is not None and auth.authenticates(from_domain) else "untrusted"


def _build(
    msg: Message, *, forwarded: bool, outer_from: str | None, authserv: str = TRUSTED_AUTHSERV
) -> ParsedEmail:
    text, html = _bodies(msg)
    name, addr = _addr(msg.get("From"))
    subject = re.sub(r"\s+", " ", str(msg.get("Subject") or "")).strip()[:500]
    auth = None if forwarded else parse_auth_results(msg, authserv)
    return ParsedEmail(
        from_addr=addr,
        subject=subject,
        received_at=_date(msg.get("Date")),
        message_id=_message_id(msg),
        text=text,
        html=html,
        links=extract_links(text, html),
        from_name=name,
        forwarded=forwarded,
        outer_from=outer_from,
        dkim_domains=auth.dkim_domains() if auth else (),
        trust="owner_forwarded" if forwarded else _direct_trust(auth, addr.rpartition("@")[2]),
    )


def _unwrap_inline(parsed: ParsedEmail) -> ParsedEmail | None:
    """Inline forward -> the original message, or None if the text has no forward marker."""
    mark = _FWD_MARK.search(parsed.text)
    if not mark:
        return None
    lines = parsed.text[mark.end() :].lstrip("\r\n").splitlines()
    headers: dict[str, str] = {}
    body_from = 0
    for i, line in enumerate(lines[:20]):
        m = _HDR_LINE.match(line)
        if m:
            headers.setdefault(m.group(1).lower(), m.group(2).strip())
            body_from = i + 1
        elif line.strip() == "" and headers:
            body_from = i + 1
            break
        elif headers:  # a wrapped header value or the body started without a blank line
            break
    if "from" not in headers:
        return None
    body = "\n".join(
        line.lstrip("> ").rstrip() if line.startswith(">") else line for line in lines[body_from:]
    ).strip()
    name, addr = _addr(re.sub(r"<mailto:[^>]*>", "", headers["from"]))
    if not addr:
        return None
    return ParsedEmail(
        from_addr=addr,
        subject=re.sub(r"\s+", " ", headers.get("subject", "")).strip()[:500],
        received_at=_date(headers.get("date") or headers.get("sent")),
        message_id=None,
        text=body[:MAX_TEXT_CHARS],
        html=parsed.html if parsed.html else "",
        links=extract_links(body, ""),
        from_name=name,
        forwarded=True,
        outer_from=parsed.from_addr or None,
        dkim_domains=(),
        trust="owner_forwarded",
    )


def _attached_messages(msg: Message) -> list[Message]:
    """The DIRECT message/rfc822 parts of the outer message (also inside its multipart
    containers), never messages attached inside an attached message."""
    found: list[Message] = []

    def visit(m: Message, budget: list[int]) -> None:
        for part in m.get_payload() if m.is_multipart() else []:
            budget[0] -= 1
            if budget[0] < 0 or len(found) >= MAX_INNER_MESSAGES:
                return
            if part.get_content_type() == "message/rfc822":
                payload = part.get_payload()
                inner = payload[0] if isinstance(payload, list) and payload else None
                if isinstance(inner, Message):
                    found.append(inner)  # not descended into
            elif part.is_multipart():
                visit(part, budget)

    visit(msg, [MAX_PARTS * 2])
    return found


def parse_messages(
    raw: bytes, *, forwarders: Collection[str] = (), authserv: str = TRUSTED_AUTHSERV
) -> list[ParsedEmail]:
    """Bytes of one IMAP message -> one ParsedEmail, or several for forwards (see module doc).

    Forwards are unwrapped ONLY when the outer From is one of `forwarders` (the owner's own
    addresses, lowercase; default none = never unwrap) AND the Authentication-Results of
    `authserv` (topmost, ours) show dmarc=pass with header.from exactly equal to the forwarder's
    domain. No such header, or any other result: the message is ordinary untrusted mail and is not
    unwrapped. Unwrapped messages get trust "owner_forwarded". Raises on input that cannot be
    read at all; the caller records a per-message failure.
    """
    msg = email.message_from_bytes(raw, policy=policy.default)
    outer = _addr(msg.get("From"))[1]
    allowed = {f.strip().lower() for f in forwarders if f.strip()}
    if outer not in allowed:
        return [_build(msg, forwarded=False, outer_from=None, authserv=authserv)]
    auth = parse_auth_results(msg, authserv)
    if auth is None or auth.dmarc_from() != outer.rpartition("@")[2]:
        log.warning("mail from a forwarder address is not dmarc-authenticated: not unwrapped")
        return [_build(msg, forwarded=False, outer_from=None, authserv=authserv)]
    attached = _attached_messages(msg)
    if attached:
        return [_build(inner, forwarded=True, outer_from=outer) for inner in attached]
    parsed = _build(msg, forwarded=False, outer_from=None, authserv=authserv)
    return [_unwrap_inline(parsed) or parsed]
