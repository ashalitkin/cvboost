"""Deterministic parser for LinkedIn notification emails (no LLM, no network).

The text is untrusted: names and snippets are cut and cleaned, links must be https on
linkedin.com (tracking query dropped) and are never fetched. Unknown layouts become one
«other» event whose snippet is the subject; a missing headline or company is fine.
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from cvboost.crm.types import SNIPPET_MAX, ParsedEmail, ParsedEvent
from cvboost.llm.prompts import registry
from cvboost.llm.sanitize import normalize_text
from cvboost.textutil import html_to_text

# senders the CRM handler takes (exact addresses; job-alert mail is NOT here)
LINKEDIN_SENDERS = (
    "hit-reply@linkedin.com",
    "inmail-hit-reply@linkedin.com",
    "messaging-digest-noreply@linkedin.com",
    "invitations@linkedin.com",
)
# the owner's own profile links sit in every mail header; never take them for the contact: their
# slugs come from the linkedin.com/in/... link of profile.md (Contacts), see registry.own_slugs()
NAME_MAX = 120
HEADLINE_MAX = 200

_FWD = re.compile(r"^\s*(?:(?:fwd?|re)\s*:\s*)+", re.I)
_INMAIL_SUBJECTS = (
    re.compile(r"^message accepted by\s+(?P<n>.{1,200})$", re.I),
    re.compile(r"^(?P<n>.{1,200}?)\s+accepted your inmail\b", re.I),
)
_INMAIL_LINE = re.compile(r"^\*?(?P<n>[^*\n]{1,120}?)\*?\s+has accepted your request\b", re.I)
_INBOX_ID = re.compile(r"/sales/inbox/([A-Za-z0-9_=-]{4,200})")
_ACCEPTED = re.compile(r"^(?P<n>.{1,200}?)\s+accepted your invitation\b", re.I)
_MSG_SUBJECTS = (
    re.compile(
        r"^(?P<n>.{1,200}?)\s+(?:sent you a message|messaged you|replied to your message)", re.I
    ),
    re.compile(r"^(?:you have a )?new message (?:from|on linkedin from)\s+(?P<n>.{1,200})$", re.I),
)
_DIGEST = re.compile(r"^you have \d+ new messages\b|^\d+ new messages\b", re.I)
_CTA = re.compile(
    r"^(view (?:in |message|profile|conversation|all)|reply|message\b|see all|unsubscribe|"
    r"this email was|you are receiving|linkedin corporation|help center|©|view in browser)",
    re.I,
)
_VIEW_SALES = re.compile(r"^view in sales navigator", re.I)
_URL = re.compile(r"https://[^\s<>\"')\]]+", re.I)
_DIGEST_HEAD = re.compile(r"^you have \d+ new messages?", re.I)


def safe_linkedin_url(url: object) -> str | None:
    """https on linkedin.com (or a subdomain), no credentials/port; query and fragment dropped."""
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        return None
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if p.scheme != "https" or p.username or p.password or port not in (None, 443):
        return None
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    return urlunsplit(("https", host, p.path, "", ""))[:500]


def sales_inbox_url(url: str) -> str | None:
    """«View in Sales Navigator» wraps the thread in a double-encoded redirect; the thread id is
    the only part worth keeping (the rest is tracking and the owner's contract id)."""
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    if p.hostname not in ("www.linkedin.com", "linkedin.com") or p.path != "/sales-web-view":
        return None
    inner = unquote(unquote((parse_qs(p.query).get("url") or [""])[0]))
    m = _INBOX_ID.search(inner)
    return f"https://www.linkedin.com/sales/inbox/{m.group(1)}" if m else None


def _clean(s: str, limit: int) -> str:
    return " ".join(normalize_text(s).split())[:limit]


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in normalize_text(text).split("\n")]


_ANCHOR = re.compile(r"""(?is)<a\s[^>]*?href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""")


def _body_text(p: ParsedEmail) -> str:
    """Plain text with links kept INLINE as `label url`, so every link stays next to its own
    block. #17's text for an HTML-only mail has no URLs: then the HTML anchors are inlined."""
    text = p.text or ""
    if p.html and "https://" not in text:
        quoted = re.compile(r"(?is)<(blockquote|q)\b.*?</\1\s*>")
        plain = quoted.sub(lambda m: re.sub(r"(?is)</?a\b[^>]*>", "", m.group(0)), p.html)
        inlined = _ANCHOR.sub(
            lambda m: f"{m.group(2)} {html.escape(html.unescape(m.group(1)))} ", plain
        )
        return html_to_text(inlined)
    return text if text.strip() else html_to_text(p.html or "")


def _link_pairs(lines: list[str]) -> list[tuple[str, str]]:
    """(label, safe https linkedin url) for the links of THESE lines only. The label is the line's
    own text, or the previous line when the URL stands alone (`<url>` below its text)."""
    out: list[tuple[str, str]] = []
    prev = ""
    for ln in lines:
        for raw in _URL.findall(ln):
            url = raw.rstrip(".,;")
            safe = sales_inbox_url(url) or safe_linkedin_url(url)
            if safe:
                label = ln.replace(raw, "").strip(" <>:-\t") or prev
                out.append((label.casefold(), safe))
        if ln and not _URL.search(ln):
            prev = ln
    return out


def _view(pairs: list[tuple[str, str]]) -> str | None:
    for _, u in pairs:
        if any(x in urlsplit(u).path for x in ("/messaging/", "/inbox/", "/sales/inbox/")):
            return u
    return None


def _profile(pairs: list[tuple[str, str]], name: str) -> str | None:
    """The person's own profile link: a /in/ link labelled with their name or «profile», taken
    from the header/footer lines only (never from the message text), never the owner's own."""
    low = name.casefold()
    for label, u in pairs:
        path = urlsplit(u).path
        segs = [s for s in path.lower().split("/") if s]
        # by path segment: /in/jordan is the owner's, /in/jordan-smith is somebody else
        own = any(
            a == "in" and b in registry.own_slugs() for a, b in zip(segs, segs[1:], strict=False)
        )
        if "/in/" not in path or own:
            continue
        if "profile" in label or (low and low in label):
            return u
    return None


def _company(headline: str | None) -> str | None:
    if not headline:
        return None
    m = re.search(r"\b(?:at|@)\s+(.{2,100})$", headline)
    return _clean(m.group(1), 100) if m else None


def _name_from_subject(subject: str) -> tuple[str, str] | None:
    s = _FWD.sub("", _clean(subject, 300))
    for rx in _INMAIL_SUBJECTS:
        if m := rx.match(s):
            return "message_accepted", m.group("n")
    if m := _ACCEPTED.match(s):
        return "invitation_accepted", m.group("n")
    for rx in _MSG_SUBJECTS:
        if m := rx.match(s):
            return "message", m.group("n")
    return None


def _after_name(lines: list[str], name: str) -> list[str]:
    """Body lines after the first line that is exactly the person's name."""
    low = name.casefold()
    for i, ln in enumerate(lines):
        if ln.casefold() == low:
            return lines[i + 1 :]
    return []


_OPEN_Q = ('"', "“")
_CLOSE_Q = ('"', "”")


def _split(
    rest: list[str], with_snippet: bool
) -> tuple[str | None, list[str], list[str], list[str]]:
    """(headline, snippet lines, leading lines, trailing lines) after the name. Links are read
    from the leading and trailing lines only, never from the sender's own text.

    The sender's message is the quoted text: it starts with an opening quote and ends at the LAST
    line that ends with a closing quote, so a «View profile: ...» line inside the message cannot
    end it early (the real footer comes after the real closing quote). Without that structure the
    whole rest counts as the sender's text and no link is taken (when in doubt: no link)."""
    headline = None
    i = 0
    while i < len(rest) and not rest[i]:
        i += 1
    head: list[str] = rest[:i]
    if i < len(rest) and not _CTA.match(rest[i]) and not rest[i].startswith(_OPEN_Q):
        headline = _clean(rest[i], HEADLINE_MAX) or None
        i += 1
    if not with_snippet:
        return headline, [], head, rest[i:]
    j = i
    while j < len(rest) and not rest[j]:
        j += 1
    if j < len(rest) and rest[j].startswith(_OPEN_Q):
        ends = [c for c in range(j, len(rest)) if rest[c].rstrip().endswith(_CLOSE_Q)]
        close = ends[-1] if ends else len(rest) - 1
        return headline, [x for x in rest[j : close + 1] if x], head, rest[close + 1 :]
    return headline, [x for x in rest[i:] if x], head, []


def _snip(lines: list[str]) -> str:
    return _clean(" ".join(lines).strip('"“” '), SNIPPET_MAX)


def _single(p: ParsedEmail, kind: str, name: str, body: str) -> ParsedEvent:
    name = _clean(name, NAME_MAX)
    lines = _lines(body)
    after = _after_name(lines, name)
    before = lines[: len(lines) - len(after)] if after else []  # no name line: no links
    headline, snippet, head, tail = _split(after, kind == "message")
    pairs = _link_pairs(before + head + tail)
    return ParsedEvent(
        kind=kind,
        name=name,
        headline=headline,
        company=_company(headline),
        profile_url=_profile(pairs, name),
        view_url=_view(pairs) if kind == "message" else None,
        snippet=_snip(snippet) or (_clean(p.subject, SNIPPET_MAX) if kind == "message" else ""),
    )


def _inmail_accepted(p: ParsedEmail, name: str, body: str) -> ParsedEvent:
    """Sales Navigator «InMail accepted»: `*Name* has accepted your request.`, then the person's
    own reply text, then «View in Sales Navigator». No headline in this layout."""
    lines = _lines(body)
    start = next((i for i, ln in enumerate(lines) if _INMAIL_LINE.match(ln)), None)
    snippet_lines: list[str] = []
    link_lines = lines[:0]
    if start is not None:
        # the real «View in Sales Navigator» is the LAST one: the person's text can only add
        # look-alikes before it
        ctas = [k for k in range(start + 1, len(lines)) if _VIEW_SALES.match(lines[k])]
        k = ctas[-1] if ctas else len(lines)
        for ln in lines[start + 1 : k]:
            if ln and not ln.startswith(("<http", "[image", "http")):
                snippet_lines.append(ln)
        link_lines = lines[: start + 1] + lines[k:] if ctas else lines[: start + 1]
    return ParsedEvent(
        kind="message_accepted",
        name=_clean(name, NAME_MAX),
        view_url=_view(_link_pairs(link_lines)),
        snippet=_snip(snippet_lines),
    )


MAX_BLOCKS = 100  # blocks looked at in one digest (the handler stores at most 20 events)


def _digest(p: ParsedEmail, body: str) -> list[ParsedEvent]:
    """Blocks separated by a blank line: name, optional headline, quoted snippet, «View message».
    A block counts only with its OWN «View message» link (links of other blocks and the person's
    text are not used); a block without one is skipped, so stray text makes no contact."""
    out: list[ParsedEvent] = []
    for block in re.split(r"\n\s*\n", normalize_text(body))[:MAX_BLOCKS]:
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        lines = [ln for ln in lines if not _DIGEST_HEAD.match(ln)]
        if len(lines) < 2 or _CTA.match(lines[0]) or len(lines[0]) > 80 or _URL.search(lines[0]):
            continue
        name = _clean(lines[0], NAME_MAX)
        headline, snippet, head, tail = _split(lines[1:], True)
        pairs = _link_pairs(head + tail)
        view = next(
            (
                u
                for lab, u in pairs
                if lab.startswith(("view message", "view in", "reply"))
                and any(x in urlsplit(u).path for x in ("/messaging/", "/inbox/", "/sales/inbox/"))
            ),
            None,
        )
        if view is None:
            continue
        out.append(
            ParsedEvent(
                kind="message",
                name=name,
                headline=headline,
                company=_company(headline),
                profile_url=_profile(pairs, name),
                view_url=view,
                snippet=_snip(snippet),
                index=len(out),
            )
        )
    return out


def parse_linkedin(p: ParsedEmail) -> list[ParsedEvent]:
    """One or more events for one email; never raises on odd layouts."""
    body = _body_text(p)
    subject = _FWD.sub("", _clean(p.subject or "", 300))
    if _DIGEST.search(subject):
        events = _digest(p, body)
        if events:
            return events
    found = _name_from_subject(subject)
    if found and found[1].strip():
        kind, name = found
        if kind == "message_accepted":
            return [_inmail_accepted(p, name, body)]
        if _clean(name, NAME_MAX):
            return [_single(p, kind, name, body)]
    return [ParsedEvent(kind="other", name="", snippet=subject[:SNIPPET_MAX])]
