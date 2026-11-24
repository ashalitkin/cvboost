"""Telegram message text and inline keyboards. Pure functions.

Messages are plain text (no parse_mode): nothing in a posting can become markup. Telegram still
turns URLs, bare domains, e-mails and @mentions in plain text into links, and job text and LLM
output are untrusted, so `safe_text` removes URLs with a scheme, e-mails and mentions and breaks
every `label.label` with a one-dot leader. The only links in an alert are the posting URLs taken
from `jobs.url`, and only on the source's own hosts.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from cvboost.textutil import clean_text

MAX_MESSAGE = 3500  # Telegram allows 4096 characters; keep headroom
MAX_LINKS = 3
MAX_URL = 500
LINK = "[link]"

_SCHEME_URL = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]{1,15}://|mailto:|tg:)\S*")
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")  # lookbehind: linear time
_MENTION = re.compile(r"(?<![\w])@\w{5,}")
# Telegram turns `label.label` (any TLD, IDN, xn--, IPv4) into a link. A one-dot leader looks like
# a dot but is not one, so the text stays readable ("ASP․NET", "Monday․com") and is never a link.
_DOTS = (
    ".\u3002\uff0e\uff61"  # ASCII dot, ideographic full stop, fullwidth dot, halfwidth ideographic
)
_IPV4 = re.compile(rf"\d{{1,3}}[{_DOTS}]\d{{1,3}}[{_DOTS}]\d{{1,3}}[{_DOTS}]\d{{1,3}}")
_DOT = re.compile(rf"(?<=\w)[{_DOTS}](?=\w)")
DOT_LEADER = "\u2024"
_WS = re.compile(r"\s+")


def safe_text(value: str | None, max_chars: int) -> str:
    """One line of untrusted text: no control/format characters, no links, no mentions."""
    s = clean_text(value)[: max(max_chars * 4, 2000)]  # bound the regex work on huge input
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")
    s = _SCHEME_URL.sub(LINK, s)
    s = _EMAIL.sub(LINK, s)
    s = _MENTION.sub("", s)
    s = _IPV4.sub(lambda m: re.sub(f"[{_DOTS}]", DOT_LEADER, m.group()), s)  # 4 octets: always
    # other dots between labels, but not inside numbers ("3.5 years", "€80.000", "v2.0")
    s = _DOT.sub(
        lambda m: m.group() if s[m.start() - 1].isdigit() and s[m.end()].isdigit() else DOT_LEADER,
        s,
    )
    s = _WS.sub(" ", s).strip()
    if len(s) > max_chars:
        s = s[: max_chars - 1].rstrip() + "…"
    return s


@dataclass(frozen=True)
class AlertView:
    title: str
    company: str
    rate: str | None  # LLM rate_text, else the rules' display; None = unknown
    location: str | None
    fit: int
    profile: str | None
    employment: str | None
    seniority: str | None
    summary: str
    warnings: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (source, https URL from jobs.url)


# ToS: Adzuna must be named next to its advert; ITJobs.pt likewise by our own choice
ALERT_LABEL = {"adzuna": "Jobs by Adzuna", "itjobs": "via ITJobs.pt"}


def _link_hosts() -> dict[str, frozenset[str]]:
    from cvboost.connectors import REGISTRY

    hosts = {name: cls.allowed_hosts for name, cls in REGISTRY.items()}
    for cls in REGISTRY.values():
        hosts.update(cls.job_source_hosts)
    hosts["hn_hiring"] = frozenset(
        {"news.ycombinator.com"}
    )  # postings live on HN, not the API host
    hosts["hn_jobs"] = frozenset({"news.ycombinator.com"})
    hosts["jobtech_se"] = frozenset({"arbetsformedlingen.se"})
    # the API host is not where the advert lives (Adzuna country sites, ITJobs.pt site)
    hosts["adzuna"] = frozenset(
        {f"adzuna.{t}" for t in ("co.uk", "de", "nl", "es", "pt", "com", "fr", "com.au", "ca")}
    )
    hosts["itjobs"] = frozenset({"itjobs.pt"})
    hosts["reed"] = frozenset({"reed.co.uk"})
    return hosts


def https_link(source: str, url: str) -> str | None:
    """Our own posting URL: https, ASCII, no whitespace, on the source's own host (or one of its
    subdomains). Anything else is not shown."""
    hosts = _link_hosts().get(source)
    if (
        not hosts
        or not url.startswith("https://")
        or len(url) > MAX_URL
        or not url.isascii()
        or re.search(r"[\s\x00-\x1f\x7f]", url)
    ):
        return None
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return None
    if parts.username is not None or port not in (None, 443):
        return None
    host = (parts.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in hosts):
        return url
    return None


def render_alert(v: AlertView) -> str:
    def build(summary_max: int) -> str:
        lines = [
            f"{safe_text(v.title, 200)} — {safe_text(v.company, 200)}",
            f"Rate: {safe_text(v.rate, 80) if v.rate and v.rate != '?' else 'not stated'}",
            f"Location: {safe_text(v.location, 120) or '—'}",
            "Fit "
            + " · ".join(
                x
                for x in (
                    str(v.fit),
                    v.profile,
                    v.employment if v.employment != "unknown" else None,
                    v.seniority if v.seniority != "unknown" else None,
                )
                if x
            ),
            f"Summary: {safe_text(v.summary, summary_max)}",
        ]
        warn = [w for w in (safe_text(w, 80) for w in v.warnings) if w]
        if warn:
            lines.append("Warnings: " + "; ".join(warn))
        links = [(s, u) for s, u in v.links if https_link(s, u)][:MAX_LINKS]
        if links:
            lines.append("Sources:")
            lines += [f"{ALERT_LABEL.get(s, s)}: {u}" for s, u in links]
        return "\n".join(lines)

    text = build(400)
    if len(text) > MAX_MESSAGE:
        text = build(100)
    if len(text) > MAX_MESSAGE:  # cannot happen with the field caps; never exceed Telegram's limit
        text = text[: MAX_MESSAGE - 1] + "…"
    return text


def render_summary(items: list[tuple[int, str, str]], max_lines: int) -> str:
    """One message for the cards beyond the per-cycle cap: `fit · title — company` per line."""
    shown = items[:max_lines]
    lines = [f"{len(items)} more matches (no buttons, see the panel):"]
    lines += [f"{fit} · {safe_text(t, 100)} — {safe_text(c, 60)}" for fit, t, c in shown]
    if len(items) > len(shown):
        lines.append(f"and {len(items) - len(shown)} more in the panel")
    return "\n".join(lines)[:MAX_MESSAGE]


# --- keyboards ---

REASONS = ("stack", "rate", "location", "other")
REASON_LABEL = {"stack": "Stack", "rate": "Rate", "location": "Location", "other": "Other"}
OPS = ("i", "n", "r", "cv", "u", "b", "x")
CALLBACK_RE = re.compile(r"^(i|n|r|cv|u|b|x):(\d{1,10})(?::(stack|rate|location|other))?$")


def cb(op: str, alert_id: int, arg: str | None = None) -> str:
    data = f"{op}:{alert_id}" + (f":{arg}" if arg else "")
    assert len(data.encode()) <= 64, data  # Bot API: callback_data is 1-64 bytes
    return data


def _btn(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


def keyboard(alert_id: int, state: str) -> dict[str, Any]:
    """state: initial | reasons | interested | rejected:<reason> | cv_queued"""
    a = alert_id
    if state == "initial":
        rows = [
            [
                _btn("Interested", cb("i", a)),
                _btn("No", cb("n", a)),
                _btn("Prepare CV", cb("cv", a)),
            ]
        ]
    elif state == "reasons":
        rows = [
            [_btn("Stack", cb("r", a, "stack")), _btn("Rate", cb("r", a, "rate"))],
            [_btn("Location", cb("r", a, "location")), _btn("Other", cb("r", a, "other"))],
            [_btn("Back", cb("b", a))],
        ]
    elif state == "interested":
        rows = [
            [
                _btn("Interested ✓", cb("x", a)),
                _btn("Prepare CV", cb("cv", a)),
                _btn("Undo", cb("u", a)),
            ]
        ]
    elif state.startswith("rejected:"):
        reason = state.split(":", 1)[1]
        rows = [
            [
                _btn(f"No: {REASON_LABEL.get(reason, reason)}", cb("x", a)),
                _btn("Undo", cb("u", a)),
            ]
        ]
    elif state == "cv_queued":
        rows = [[_btn("CV: queued", cb("x", a)), _btn("Undo", cb("u", a))]]
    else:
        raise ValueError(f"unknown keyboard state {state!r}")
    return {"inline_keyboard": rows}
