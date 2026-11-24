"""Small helpers shared by the JSON-API connectors."""

from __future__ import annotations

from datetime import UTC, datetime

from cvboost.models import SalaryPeriod


def utc_from_epoch(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def salary_number(value: object) -> float | None:
    """A positive number or None (APIs use 0 / null / '' for 'not stated')."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value > 0 else None


def salary_period(value: object, table: dict[str, SalaryPeriod]) -> SalaryPeriod | None:
    return table.get(str(value).strip().lower()) if value else None


def http_url_or_none(value: object) -> str | None:
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        return value
    return None


def rss_items(body: bytes) -> list[dict[str, str]]:
    """Items of an RSS 2.0 body as {tag: stripped text} (first occurrence wins; `category`
    values are joined with a newline). defusedxml: DTDs and entity tricks are refused."""
    from defusedxml import ElementTree as ET

    out: list[dict[str, str]] = []
    for item in ET.fromstring(body).iterfind("./channel/item"):
        fields: dict[str, str] = {}
        for child in item:
            tag = child.tag
            if not isinstance(tag, str):
                continue
            text = (child.text or "").strip()
            if tag == "category":
                fields[tag] = f"{fields[tag]}\n{text}" if tag in fields else text
            elif tag not in fields:
                fields[tag] = text
        out.append(fields)
    return out


def last_path_segment(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]


def xml_text(element: object, tag: str) -> str:
    """Stripped text of the first direct child `tag` of an XML element ('' if absent)."""
    child = element.find(tag)  # type: ignore[attr-defined]
    return (child.text or "").strip() if child is not None else ""
