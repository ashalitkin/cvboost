"""Keep the Telegram bot token out of text that leaves the process (logs, errors, the DB).

The token sits in the URL path (`/bot<token>/method`), so any URL, exception message or traceback
that mentions a Telegram request can carry it.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import traceback

# no leading \b: "bot123456789:AA..." and "yyy123456789:AA..." must match too
_TOKEN_SHAPE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")
_BOT_PATH = re.compile(r"/bot[^/\s]+/")
MARK = "<redacted>"


API_KEY_VARS = ("ADZUNA_APP_ID", "ADZUNA_APP_KEY", "REED_API_KEY", "ITJOBS_API_KEY")


def _api_secrets() -> list[str]:
    """Values of the keyed-API credentials (raw and as Basic-auth base64), read at call time."""
    out: list[str] = []
    for name in API_KEY_VARS:
        value = os.environ.get(name, "").strip()
        if len(value) >= 4:
            out += [value, base64.b64encode(f"{value}:".encode()).decode()]
    return out


def redact(text: str, *secrets: str | None) -> str:
    """Hide the bot token (by shape and by value), the keyed-API credentials from the environment
    and any other known secret (e.g. the IMAP password) given by value."""
    for secret in (*secrets, *_api_secrets()):
        if secret:
            text = text.replace(secret, MARK)
    text = _BOT_PATH.sub(f"/bot{MARK}/", text)
    return _TOKEN_SHAPE.sub(MARK, text)


class RedactingFilter(logging.Filter):
    """Rewrites message and traceback of every record that passes through a handler."""

    def __init__(self, *secrets: str | None):
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage(), *self._secrets)
            record.args = None
            if record.exc_info and record.exc_info[0] is not None:
                record.exc_text = redact(
                    "".join(traceback.format_exception(*record.exc_info)).rstrip("\n"),
                    *self._secrets,
                )
                record.exc_info = None
        except Exception:  # a logging filter must never break the caller
            record.msg, record.args = "<unprintable log record>", None
        return True
