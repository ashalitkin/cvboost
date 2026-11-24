"""Connector interface: one small module per source."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlsplit
from xml.etree.ElementTree import ParseError

import httpx

from cvboost.http import FetchError, HttpClient
from cvboost.models import Job, RawPosting, SourceKind

if TYPE_CHECKING:
    from cvboost.storage.db import Store


@dataclass(frozen=True)
class RequestFailure:
    """One request of a run that failed. `path` only: no host, query or credentials."""

    path: str
    error_type: str
    http_status: int | None = None

    def describe(self) -> str:
        what = f"HTTP {self.http_status}" if self.http_status else self.error_type
        return f"{self.path} {what}"


@dataclass
class FetchOutcome:
    raws: list[RawPosting]
    failures: list[RequestFailure] = field(default_factory=list)
    requests_total: int = 0  # requests() entries attempted (feeds); 0 = not tracked


_PARTIAL_ERRORS = (FetchError, httpx.TransportError, ValueError, ParseError)


class Connector(ABC):
    name: ClassVar[str]
    kind: ClassVar[SourceKind]
    base_url: ClassVar[str]
    interval: ClassVar[timedelta]
    min_delay_s: ClassVar[float]
    allowed_hosts: ClassVar[frozenset[str]]
    tos_note: ClassVar[str]
    attribution: ClassVar[str]
    default_enabled: ClassVar[bool] = True
    # extra `jobs.source` names this connector writes (e-mail alerts: "email:linkedin", ...);
    # `Store.sync_sources` inserts them so the jobs.source foreign key holds
    job_sources: ClassVar[tuple[str, ...]] = ()
    # hosts of the URLs stored for each job source (the panel shows links only on these hosts)
    job_source_hosts: ClassVar[dict[str, frozenset[str]]] = {}
    max_pages: int = 1

    @abstractmethod
    def requests(self) -> list[str]:
        """URLs fetched in one run."""

    @abstractmethod
    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        """Pure: turn one response body into raw postings."""

    @abstractmethod
    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        """Pure: turn a raw posting into a Job. May raise on bad data."""

    def before_run(self, store: Store, now: datetime) -> None:  # noqa: B027
        """Hook for connectors that keep state in the DB (ATS boards pick their companies)."""

    def configured(self) -> bool:
        """False when the connector lacks credentials: collect then skips it silently."""
        return True

    def nothing_to_do(self) -> bool:
        """True after before_run when this run has no work: collect then writes no run at all."""
        return False

    def skip_reason(self) -> str:
        """Why `nothing_to_do` is true (shown by `cvboost collect`)."""
        return "nothing due"

    def after_run(self, store: Store, now: datetime) -> None:  # noqa: B027
        """Hook called once the run's postings are stored (not called when the fetch failed)."""

    def next_url(self, body: bytes, url: str, page: int) -> str | None:
        """URL of the page after `page` (1-based) of a paginated request, or None."""
        return None

    def fetch(self, http: HttpClient, limit: int | None = None) -> list[RawPosting]:
        return self.fetch_partial(http, limit).raws

    def fetch_partial(self, http: HttpClient, limit: int | None = None) -> FetchOutcome:
        """Like `fetch`, but one failing request (e.g. one RSS feed) does not lose the others.

        Pages already read from a failing request are kept. If every request fails, the first
        exception is raised so the run counts as an error and backs off.
        """
        seen: set[str] = set()
        out: list[RawPosting] = []
        failures: list[RequestFailure] = []
        first_exc: Exception | None = None
        total = 0
        for first in self.requests():
            total += 1
            url: str | None = first
            page = 1
            try:
                while url is not None:
                    resp = http.get(
                        url, min_delay_s=self.min_delay_s, allowed_hosts=self.allowed_hosts
                    )
                    for raw in self.parse(resp.content, url):
                        if raw.source_id in seen:
                            continue
                        seen.add(raw.source_id)
                        out.append(raw)
                    if limit is not None and len(out) >= limit:
                        break
                    if page >= self.max_pages:
                        break
                    url = self.next_url(resp.content, url, page)
                    page += 1
            except _PARTIAL_ERRORS as exc:
                if first_exc is None:
                    first_exc = exc
                root = exc.__cause__ if isinstance(exc, FetchError) and exc.__cause__ else exc
                failures.append(
                    RequestFailure(
                        urlsplit(first).path,
                        type(root).__name__,
                        exc.status if isinstance(exc, FetchError) else None,
                    )
                )
            if limit is not None and len(out) >= limit:
                break
        if failures and len(failures) == total and first_exc is not None:
            raise first_exc
        if limit is not None:
            out = out[:limit]
        return FetchOutcome(out, failures, total)
