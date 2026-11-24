"""Approved public links and the draft validators (#39): URL allow-list, e-mail detection.

The model may use only the links in config/outreach.yaml; any other URL in a draft is rejected.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

# Left boundary with Unicode \w and a right one: an address next to a non-ASCII letter is never
# matched (and so never truncated: "joão@example.com" must not become "o@example.com").
EMAIL_RE = re.compile(
    r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])"
)
_EMAIL_FULL = re.compile(EMAIL_RE.pattern)
_URL_END = r"[^\s<>\"')\]]"
# scheme / www URLs, and any bare word.letters{2,} token (host with any TLD, any script)
_URL_RE = re.compile(
    rf"(?:https?://|www\.){_URL_END}+"
    rf"|(?<![\w@.-])[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{{2,}}(?![\w-])(?:/{_URL_END}*)?",
    re.IGNORECASE,
)
_DOTS = re.compile(r"\s?[\[({]\.[\])}]\s?|[\u3002\uff61\u2024\u2e30]")
_NOT_HOSTS = {"node.js", "next.js", "vue.js", "d3.js", "e.g", "i.e"}


def is_email(s: str) -> bool:
    return bool(_EMAIL_FULL.fullmatch(s)) and len(s) <= 254


def find_emails(text: str) -> list[str]:
    """Addresses exactly as written in the text, in order, without duplicates."""
    return list(dict.fromkeys(EMAIL_RE.findall(text or "")))


def _norm(url: str) -> str:
    return re.sub(r"^https?://", "", url.strip().rstrip(".,;:!?)").lower()).rstrip("/")


class OutreachConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    links: dict[str, str]

    @field_validator("links")
    @classmethod
    def _https(cls, v: dict[str, str]) -> dict[str, str]:
        for k, u in v.items():
            if not u.startswith("https://") or any(c.isspace() or c in "<>\"'" for c in u):
                raise ValueError(f"link {k!r} must be a plain https URL")
        return v

    def allowed(self) -> set[str]:
        return {_norm(u) for u in self.links.values()}

    def foreign_links(self, *texts: str | None) -> list[str]:
        """URLs in the texts that are not an approved link (e-mail addresses are not URLs)."""
        ok, bad = self.allowed(), []
        for t in texts:
            t = _DOTS.sub(".", unicodedata.normalize("NFKC", t or ""))
            t = EMAIL_RE.sub(" ", t)
            for m in _URL_RE.findall(t):
                if m.isascii() and (_norm(m) in ok or m.lower().rstrip(".,;:!?") in _NOT_HOSTS):
                    continue
                bad.append(m)
        return bad


def load_outreach(path: Path) -> OutreachConfig:
    try:
        return OutreachConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (yaml.YAMLError, ValidationError, OSError) as exc:
        raise ValueError(f"bad {path}: {exc}") from None
