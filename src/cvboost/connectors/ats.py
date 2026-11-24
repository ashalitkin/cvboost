"""Shared base of the ATS board connectors (Greenhouse, Lever, Ashby, Workable, Recruitee, ...).

One connector per ATS walks the companies of `config/companies.yaml`. A run polls only a slice of
them (`max_companies`, at most `ATS_CYCLE_BUDGET_S` = 60 s shared by the three connectors), the
ones due (about every 6 h, backoff after failures), so a 10-minute worker cycle stays short and the
list is covered round-robin. A company answering 404/410 twice, 6 h apart, is marked inactive; it
is re-checked every 30 days, and `cvboost companies reactivate` revives it at once. The slug goes
only into the URL of the platform and must match ^[a-z0-9-]{1,64}$: into the path of a fixed API
host, or (Recruitee, Teamtailor, Personio) into the sub-domain label, in which case the fetch
allow-list is that one host (`allowed_hosts_for`).
"""

from __future__ import annotations

import json
import logging
import re
import time
from abc import abstractmethod
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit
from xml.etree.ElementTree import ParseError

import httpx

from cvboost import ats_state
from cvboost.companies import SLUG_RE, load_companies
from cvboost.connectors.base import Connector, FetchOutcome, RequestFailure
from cvboost.core.rules import load_config
from cvboost.http import FetchError, HttpClient
from cvboost.models import RawPosting

if TYPE_CHECKING:
    from cvboost.storage.db import Store

log = logging.getLogger("cvboost.ats")

DEFAULT_COMPANIES_PATH = Path("config/companies.yaml")
DEFAULT_RULES_PATH = Path("config/rules.yaml")
# All ATS connectors of one collect call share this many seconds (they run last).
ATS_CYCLE_BUDGET_S = 60.0

# Title prefilter: only postings whose title names one of the owner's areas are stored. The
# terms are a base set plus every title term of the profiles in config/rules.yaml, so the
# prefilter cannot drop a role the relevance rules would score. Paid-media / campaign-operator
# titles are unwanted (#14) and are not added. Terms of 6+ characters match as a word prefix
# ("marketing technolog"), shorter ones as whole words ("ai" is not "maintenance").
BASE_TERMS = (
    "data", "analytics", "analytic", "analyst", "analysts", "analytical", "bi",
    "business intelligence", "dbt", "marketing", "growth", "ai", "ml", "machine learning",
    "looker", "lookml", "tableau", "tracking", "ga4", "gtm", "martech", "bigquery", "snowflake",
    "sql", "etl", "elt", "dwh", "dataops", "mlops", "attribution", "measurement", "conversion",
    "llm", "agentic", "reporting",
)  # fmt: skip
UNWANTED_TERMS = frozenset(
    ("ppc", "paid media", "paid search", "paid social", "google ads", "sem")
) | {"performance marketing"}


def build_title_re(extra_terms: Iterable[str] = ()) -> re.Pattern[str]:
    """One alternation of literals with look-around word edges: linear time, no backtracking."""
    terms = {t.strip().lower() for t in (*BASE_TERMS, *extra_terms)} - UNWANTED_TERMS
    parts = []
    for t in sorted((t for t in terms if t), key=lambda t: (-len(t), t)):
        body = r"[\s-]+".join(re.escape(w) for w in re.split(r"[\s-]+", t) if w)
        parts.append(body + ("" if len(t) >= 6 else r"(?![a-z0-9])"))
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + ")", re.IGNORECASE)


TITLE_RE = build_title_re()


def profile_title_terms(rules_path: Path | str | None) -> list[str]:
    """Every title term of every profile in rules.yaml ([] if the file cannot be read)."""
    if rules_path is None:
        return []
    try:
        cfg = load_config(rules_path)
    except Exception:  # a broken rules file must not stop collection: the base set stays
        log.warning("cannot read title terms from %s", rules_path, exc_info=True)
        return []
    return [t for p in cfg.relevance.profiles.values() for g in p.title for t in g.terms]


def title_matches(title: object, rx: re.Pattern[str] = TITLE_RE) -> bool:
    return isinstance(title, str) and rx.search(title[:300]) is not None


class ATSConnector(Connector):
    kind: ClassVar = "ats"
    interval: ClassVar[timedelta] = timedelta(minutes=10)  # a slice of the list every cycle
    min_delay_s: ClassVar[float] = 1.0  # between two companies (same host)
    company_interval: ClassVar[timedelta] = timedelta(hours=6)
    max_companies: ClassVar[int] = 25  # per run
    # own cap per run; the 60 s budget shared by all ATS connectors (collect) normally wins
    time_budget_s: ClassVar[float] = 90.0
    default_enabled: ClassVar[bool] = True

    def __init__(
        self,
        companies_path: Path | str | None = None,
        rules_path: Path | str | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.companies_path = Path(companies_path) if companies_path else DEFAULT_COMPANIES_PATH
        self.rules_path = Path(rules_path) if rules_path else DEFAULT_RULES_PATH
        self._title_re = build_title_re(profile_title_terms(self.rules_path))
        # set by collect(): seconds left of the ATS budget shared by the three connectors
        self.cycle_budget_s: float | None = None
        self._monotonic = monotonic
        self._batch: list[str] = []
        self._names: dict[str, str] = {}
        self._pending_ok: list[tuple[str, int]] = []  # (slug, postings found), stored by after_run
        self._store: Store | None = None
        self._now: datetime | None = None

    # -- per ATS ---------------------------------------------------------------------------
    @abstractmethod
    def company_url(self, slug: str) -> str:
        """API URL of one company. Called only with a validated slug."""

    @abstractmethod
    def postings(self, data: Any) -> list[dict[str, Any]]:
        """The posting objects of a decoded response (raise ValueError on a wrong shape)."""

    @abstractmethod
    def posting_id(self, item: dict[str, Any]) -> str | None: ...

    @abstractmethod
    def posting_title(self, item: dict[str, Any]) -> object: ...

    def decode(self, body: bytes) -> Any:
        """The response body as Python data (JSON by default; XML platforms override)."""
        return json.loads(body)

    def allowed_hosts_for(self, slug: str) -> frozenset[str]:
        """Hosts the HttpClient may contact for this company (also for redirects).

        `allowed_hosts` is the platform's own host set. Platforms with one sub-domain per company
        override this with exactly that sub-domain, so one tenant can never reach another host.
        """
        return self.allowed_hosts

    # -- the Connector interface -------------------------------------------------------------
    def url_for(self, slug: str) -> str:
        if not SLUG_RE.fullmatch(slug):
            raise ValueError(f"invalid {self.name} company slug")
        return self.company_url(slug)

    def slug_of(self, url: str) -> str:
        """Company slug of one of our own URLs: the third path segment by default."""
        segments = urlsplit(url).path.strip("/").split("/")
        if len(segments) < 3:
            raise ValueError("not a board URL")
        return segments[2]

    def requests(self) -> list[str]:
        return [self.url_for(s) for s in self._batch]

    def before_run(self, store: Store, now: datetime) -> None:
        companies, rejected = load_companies(self.companies_path)
        if rejected:
            log.warning(
                "%s: %d invalid entries in %s", self.name, len(rejected), self.companies_path
            )
        mine = [c for c in companies if c.ats == self.name]
        with store.transaction():
            ats_state.sync_companies(store, mine, now)
        due = ats_state.due_companies(
            store, self.name, {c.slug for c in mine}, now=now, limit=self.max_companies
        )
        self._batch = [slug for slug, _ in due]
        self._names = {slug: name for slug, name in due}
        self._pending_ok = []
        self._store = store
        self._now = now

    def nothing_to_do(self) -> bool:
        return not self._batch

    def after_run(self, store: Store, now: datetime) -> None:
        with store.transaction():
            for slug, found in self._pending_ok:
                ats_state.record_check(store, self.name, slug, "ok", now=now, found=found)
        self._pending_ok = []

    def parse_all(self, body: bytes, url: str) -> list[RawPosting]:
        """Every posting of one company, before the title prefilter."""
        slug = self.slug_of(url)
        data = self.decode(body)
        out = []
        for item in self.postings(data):
            pid = self.posting_id(item)
            if pid is None:
                continue
            payload = dict(item)
            payload["_company_slug"] = slug
            payload["_company_name"] = self._names.get(slug) or slug
            out.append(RawPosting(source=self.name, source_id=pid, payload=payload))
        return out

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        return [
            r
            for r in self.parse_all(body, url)
            if title_matches(self.posting_title(r.payload), self._title_re)
        ]

    def fetch_partial(self, http: HttpClient, limit: int | None = None) -> FetchOutcome:
        out: list[RawPosting] = []
        failures: list[RequestFailure] = []
        first_exc: Exception | None = None
        polled = 0  # companies that were not gone: the run fails only if all of them failed
        budget = self.time_budget_s
        if self.cycle_budget_s is not None:
            budget = min(budget, self.cycle_budget_s)
        started = self._monotonic()
        done = 0
        for slug in self._batch:
            if self._monotonic() - started >= budget:
                log.info(
                    "%s: time budget used, %d of %d companies left",
                    self.name,
                    len(self._batch) - done,
                    len(self._batch),
                )
                break
            if limit is not None and len(out) >= limit:
                break
            done += 1
            try:
                url = self.url_for(slug)
            except ValueError:  # never reached from the yaml (validated there); defence in depth
                log.warning("%s: refusing company slug %r", self.name, slug[:40])
                continue
            try:
                resp = http.get(
                    url, min_delay_s=self.min_delay_s, allowed_hosts=self.allowed_hosts_for(slug)
                )
                raws = self.parse(resp.content, url)
            except FetchError as exc:
                if exc.status in (404, 410):
                    ats_state.record_check(
                        self._store, self.name, slug, "gone", now=self._now, status=exc.status
                    )
                    log.info("%s: %s answered HTTP %s", self.name, slug, exc.status)
                    continue
                polled += 1
                first_exc = first_exc or exc
                failures.append(RequestFailure(urlsplit(url).path, type(exc).__name__, exc.status))
                ats_state.record_check(
                    self._store, self.name, slug, "failed", now=self._now, status=exc.status
                )
                continue
            except (
                ValueError,
                TypeError,
                KeyError,
                RecursionError,
                ParseError,
                httpx.TransportError,
            ) as exc:
                # bad JSON, an unexpected shape, nesting too deep, timeout, DNS: counted per
                # company (backoff), never an error of the whole run
                polled += 1
                first_exc = first_exc or exc
                failures.append(RequestFailure(urlsplit(url).path, type(exc).__name__))
                ats_state.record_check(self._store, self.name, slug, "failed", now=self._now)
                continue
            polled += 1
            if limit is not None and len(out) + len(raws) > limit:
                # --limit cut this company's postings: it is not "checked", it stays due
                out.extend(raws[: limit - len(out)])
                break
            self._pending_ok.append((slug, len(raws)))
            out.extend(raws)
        if failures and len(failures) == polled and first_exc is not None:
            raise first_exc
        return FetchOutcome(out, failures, polled)
