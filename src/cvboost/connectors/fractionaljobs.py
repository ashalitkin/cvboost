"""Fractional Jobs (fractionaljobs.io): the public "Live Jobs" list on the home page (#69).

HTML kind: the home page is server-rendered (Webflow CMS) with every live job as a
`div.job-item`; there is no feed or API (`/feed`, `/rss.xml` are 404). One request for the list,
plus the detail page of a few relevant jobs (the list has no description). Companies are often
anonymised ("A Boutique Hedge Fund"); many roles are US-only: the location rules decide.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser

from cvboost.connectors.base import Connector, FetchOutcome
from cvboost.http import FetchError, HttpClient
from cvboost.models import Job, RawPosting, SalaryPeriod, SourceKind
from cvboost.textutil import canonical_url, html_to_text

BASE = "https://www.fractionaljobs.io"
# functions whose detail page is worth a request (the rest, finance / sales / legal ..., are
# rejected by the profile rules from the title anyway)
DETAIL_FUNCTIONS = frozenset(
    {"Analytics", "Engineering", "Growth", "Marketing", "Product", "Other"}
)
MAX_DETAILS = 12
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,200}$")
_RATE = re.compile(
    r"\$\s*([\d.,]+)\s*([kK])?\s*(?:-\s*\$?\s*([\d.,]+)\s*([kK])?)?\s*/\s*(hr|hour|day|wk|mo|yr)",
    re.I,
)
_PERIODS: dict[str, SalaryPeriod] = {
    "hr": "hour",
    "hour": "hour",
    "day": "day",
    "mo": "month",
    "yr": "year",
}


def _num(text: str, k: str | None) -> float | None:
    try:
        v = float(text.replace(",", ""))
    except ValueError:
        return None
    return v * 1000 if k else v


def parse_rate(text: str) -> tuple[float | None, float | None, SalaryPeriod | None]:
    """'$150 - $250 / hr' -> (150, 250, 'hour'); weekly rates are not a SalaryPeriod -> None."""
    m = _RATE.search(text or "")
    if not m:
        return None, None, None
    period = _PERIODS.get(m.group(5).lower())
    lo = _num(m.group(1), m.group(2))
    hi = _num(m.group(3), m.group(4)) if m.group(3) else lo
    if period is None or lo is None:
        return None, None, None
    return lo, hi, period


def _text(node: Any) -> str:
    return " ".join((node.text(separator=" ") if node is not None else "").split())


def _visible(node: Any) -> bool:
    return node is not None and "w-condition-invisible" not in (node.attributes.get("class") or "")


class FractionalJobsConnector(Connector):
    name: ClassVar[str] = "fractionaljobs"
    kind: ClassVar[SourceKind] = "html"
    base_url: ClassVar[str] = BASE + "/"
    interval: ClassVar[timedelta] = timedelta(hours=6)
    min_delay_s: ClassVar[float] = 5.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset({"www.fractionaljobs.io"})
    tos_note: ClassVar[str] = (
        "robots.txt has no Disallow; the Terms (2024-01-06) have no clause on automated access. "
        "No feed or API exists, so the public home page is read: 1 list request + at most "
        f"{MAX_DETAILS} detail pages every 6 h, 5 s apart. Link back to the posting."
    )
    attribution: ClassVar[str] = "via Fractional Jobs"

    def requests(self) -> list[str]:
        return [self.base_url]

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        tree = LexborHTMLParser(body.decode("utf-8", errors="replace"))
        out: list[RawPosting] = []
        for item in tree.css("div.job-item"):
            link = item.css_first("a.job-item_link-to-job")
            slug = (
                _text(item.css_first(".job-id"))
                or ((link.attributes.get("href") or "") if link else "").rsplit("/", 1)[-1]
            )
            if not _ID.match(slug):
                continue
            h3 = [_text(h) for h in item.css("div.job-item_name_url h3")]
            parts = [p for p in h3 if p]
            if len(parts) < 3 or parts[1] != "-":
                continue
            info = [
                _text(d)
                for d in item.css(".job-item_more-info > div.text-inline")
                if _text(d) != "|"
            ]
            company_link = item.css_first("a.job-item_company-link")
            out.append(
                RawPosting(
                    source=self.name,
                    source_id=slug,
                    payload={
                        "slug": slug,
                        "company": parts[0],
                        "title": " ".join(parts[2:]),
                        "company_url": (company_link.attributes.get("href") or "")
                        if _visible(company_link)
                        else "",
                        "hours": info[0] if len(info) > 0 else "",
                        "rate": info[1] if len(info) > 1 else "",
                        "location": info[2] if len(info) > 2 else "",
                        "function": _text(item.css_first("[fs-cmsfilter-field=function]")),
                        "syndicated": _visible(item.css_first(".syndicated")),
                        "date": _text(item.css_first(".job-item_more-info .date")),
                        "description_html": "",
                    },
                )
            )
        return out

    @staticmethod
    def parse_detail(body: bytes) -> str:
        """The rich-text description of a detail page ('The Company / The Work / About You')."""
        tree = LexborHTMLParser(body.decode("utf-8", errors="replace"))
        sections = tree.css("section.text-rich-text.w-richtext")
        return "".join(s.html or "" for s in sections)

    def fetch_partial(self, http: HttpClient, limit: int | None = None) -> FetchOutcome:
        outcome = super().fetch_partial(http, limit)
        done = 0
        for raw in outcome.raws:
            if done >= MAX_DETAILS:
                break
            if raw.payload.get("function") not in DETAIL_FUNCTIONS:
                continue
            done += 1
            try:
                resp = http.get(
                    f"{BASE}/jobs/{raw.payload['slug']}",
                    min_delay_s=self.min_delay_s,
                    allowed_hosts=self.allowed_hosts,
                )
            except FetchError:
                continue  # the list entry is still useful without the description
            raw.payload["description_html"] = self.parse_detail(resp.content)
        return outcome

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        p: dict[str, Any] = raw.payload
        lo, hi, period = parse_rate(p.get("rate") or "")
        location = (p.get("location") or "").strip()
        posted = None
        if p.get("date"):
            try:
                posted = datetime.strptime(p["date"], "%B %d, %Y").replace(tzinfo=UTC)
            except ValueError:
                posted = None
        html = p.get("description_html") or ""
        head = "Fractional / part-time engagement."
        if p.get("hours"):
            head += f" Commitment: {p['hours']} per week."
        if p.get("rate") and p["rate"] != "Unknown":
            head += f" Rate: {p['rate']}."
        if location:
            head += f" Location: {location}."
        company_url = p.get("company_url") or ""
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=canonical_url(urljoin(BASE, f"/jobs/{p['slug']}")),
            company=p["company"],
            title=p["title"],
            description_html=html,
            description_text=f"{head}\n\n{html_to_text(html)}".strip(),
            location_text=location or None,
            remote=location.lower().startswith("remote") or None,
            employment_type="part_time",
            tags=["fractional", p["function"]] if p.get("function") else ["fractional"],
            salary_min=lo,
            salary_max=hi,
            salary_currency="USD" if lo else None,
            salary_period=period,
            salary_text=p["rate"] if p.get("rate") and p["rate"] != "Unknown" else None,
            posted_at=posted,
            fetched_at=fetched_at,
            raw={k: v for k, v in p.items() if k != "description_html"}
            | {"company_url": company_url},
        )
