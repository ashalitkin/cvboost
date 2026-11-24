"""Mask personal contact data (addresses, phone numbers) and URLs in mail-derived text.

Recruiters' e-mails carry names, addresses and phones; none of that is needed to score a role.
Bounded quantifiers only (linear time on hostile input).
"""

from __future__ import annotations

import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,5}")
_PHONE = re.compile(
    r"\+\d[\d\s().-]{7,18}\d"  # +351 912 345 678
    r"|\(\d{2,4}\)[\s.-]?\d[\d\s.-]{5,12}\d"  # (212) 555-0123
    r"|(?<![\d.,-])\d{3}[\s.-]\d{3}[\s.-]\d{3,4}(?![\d-]|\.\d)"  # 212-555-0123
)
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]{1,2048}", re.IGNORECASE)


def mask_contacts(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    return _PHONE.sub("[phone]", text)


def strip_unsafe_field(text: str) -> str:
    """For short model-written fields (title, company, rate): no URLs, addresses or phones."""
    text = _URL.sub("", text)
    return re.sub(
        r"\s+", " ", mask_contacts(text).replace("[email]", "").replace("[phone]", "")
    ).strip()
