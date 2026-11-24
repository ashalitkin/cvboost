"""RemoteOK public API: https://remoteok.com/api (JSON, full dump).

Terms (in element [0].legal): follow-link to the RemoteOK URL and mention "Remote OK".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from cvboost.connectors.base import Connector
from cvboost.models import Job, RawPosting, SourceKind
from cvboost.textutil import canonical_url, html_to_text


def fix_mojibake(s: str) -> str:
    """Undo UTF-8 text that was decoded as latin-1/cp1252; return `s` unchanged if unsure."""
    # Mojibake always contains non-ASCII lead chars (Ã, â, Â ...); a genuine non-ASCII string
    # practically never survives the round trip as valid UTF-8, so the fallback keeps it as is.
    if s.isascii():
        return s
    for enc in ("latin-1", "cp1252"):
        try:
            fixed = s.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        return fixed
    return s


def _is_http(value: object) -> bool:
    return isinstance(value, str) and value.lower().startswith(("http://", "https://"))


def _positive(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


class RemoteOKConnector(Connector):
    name: ClassVar[str] = "remoteok"
    kind: ClassVar[SourceKind] = "api"
    base_url: ClassVar[str] = "https://remoteok.com/api"
    interval: ClassVar[timedelta] = timedelta(hours=1)
    min_delay_s: ClassVar[float] = 5.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"remoteok.com"})
    tos_note: ClassVar[str] = (
        "Must link back (follow link) to the RemoteOK URL and mention Remote OK; no logo use. "
        "robots Content-Signal: search=yes, ai-train=no, use=reference."
    )
    attribution: ClassVar[str] = "via Remote OK"

    def requests(self) -> list[str]:
        return [self.base_url]

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        data = json.loads(body)
        if not isinstance(data, list):
            raise ValueError("unexpected RemoteOK payload: not a list")
        # element [0] is the legal notice (no id); skip anything without an id
        return [
            RawPosting(source=self.name, source_id=str(item["id"]), payload=item)
            for item in data
            if isinstance(item, dict) and item.get("id")
        ]

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p: dict[str, Any] = raw.payload
        html = fix_mojibake(p.get("description") or "")
        smin, smax = _positive(p.get("salary_min")), _positive(p.get("salary_max"))
        has_salary = smin is not None or smax is not None
        apply_url = p.get("apply_url")
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(p["url"]),
            apply_url=canonical_url(apply_url, strip_utm=False) if _is_http(apply_url) else None,
            company=fix_mojibake(p["company"]),
            title=fix_mojibake(p["position"]),
            description_html=html,
            description_text=html_to_text(html),
            location_text=(fix_mojibake(p.get("location") or "") or None),
            remote=True,
            tags=[fix_mojibake(str(t)) for t in p.get("tags") or []],
            salary_min=smin,
            salary_max=smax,
            salary_currency="USD" if has_salary else None,
            salary_period="year" if has_salary else None,
            posted_at=datetime.fromtimestamp(int(p["epoch"]), UTC) if p.get("epoch") else None,
            fetched_at=fetched_at,
            raw=p,
        )
