"""Base of the connectors for APIs that need a key (Adzuna, Reed, ITJobs.pt; issue #19).

Keys come from the environment only. Without them the source is `not_configured`: no request,
no run row. A key must never reach a log, an error message, the DB or a stored payload:
- the key goes to the request through `params` / `headers`, never into the URL string;
- every error that leaves `fetch_partial` is rebuilt from the request path and scrubbed of the key
  (raw, URL-encoded and base64 forms);
- `scrub_payload` removes the key from every string of a stored posting.
Every call is counted in the DB (`api_budget`); at the limit the source pauses.
"""

from __future__ import annotations

import base64
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

from cvboost import api_budget
from cvboost.connectors.base import (
    _PARTIAL_ERRORS,
    Connector,
    FetchOutcome,
    RequestFailure,
)
from cvboost.http import FetchError, HttpClient
from cvboost.models import RawPosting
from cvboost.storage.db import Store

log = logging.getLogger("cvboost.keyed")
REDACTED = "***"


@dataclass(frozen=True)
class Call:
    """One planned request: URL without secrets plus its (non-secret) query parameters."""

    url: str
    params: dict[str, str] = field(default_factory=dict)


def scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if not secret:
            continue
        for form in {secret, quote(secret, safe=""), quote(secret)}:
            text = text.replace(form, REDACTED)
    return text


def scrub_payload(value: Any, secrets: list[str]) -> Any:
    """Copy of a JSON value with every secret removed from every string (keys included)."""
    if isinstance(value, str):
        return scrub(value, secrets)
    if isinstance(value, list):
        return [scrub_payload(v, secrets) for v in value]
    if isinstance(value, dict):
        return {scrub(str(k), secrets): scrub_payload(v, secrets) for k, v in value.items()}
    return value


class KeyedAPIConnector(Connector):
    api: ClassVar[str]  # budget / state key
    label: ClassVar[str]  # name in alerts
    env_vars: ClassVar[tuple[str, ...]]  # all must be set
    default_limit: ClassVar[api_budget.Limit]
    calls_per_run: ClassVar[int] = 4

    def __init__(self) -> None:
        self._store: Store | None = None
        self._now: datetime | None = None
        self._creds: dict[str, str] | None = None

    # --- to implement ---

    def plan(self) -> list[Call]:
        """The whole rotation of requests. Each run takes the next `calls_per_run`."""
        raise NotImplementedError

    def auth(self, creds: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        """(extra query params, headers) that carry the credentials."""
        raise NotImplementedError

    # --- shared ---

    def requests(self) -> list[str]:
        return list(dict.fromkeys(c.url for c in self.plan()))

    def credentials(self) -> dict[str, str] | None:
        vals = {v: os.environ.get(v, "").strip() for v in self.env_vars}
        return vals if all(vals.values()) else None

    def secrets(self) -> list[str]:
        creds = self.credentials() or self._creds or {}
        out = list(creds.values())
        for value in list(out):
            out.append(base64.b64encode(f"{value}:".encode()).decode())
        return [s for s in out if s]

    def _limits(self) -> tuple[api_budget.Limit, float]:
        limit, warn, _ = api_budget.load_limit(self.api, self.default_limit)
        return limit, warn

    def _paused(self) -> bool:
        if self._store is None or self._now is None:
            return False
        limit, _ = self._limits()
        return not api_budget.status(self._store, self.api, limit, self._now).ok

    def before_run(self, store: Store, now: datetime) -> None:
        self._store, self._now = store, now
        self._creds = self.credentials()
        if self._creds is not None:
            _, _, problem = api_budget.load_limit(self.api, self.default_limit)
            if problem:
                log.warning("%s: api_budget config: %s", self.name, problem)
                store.insert_system_alert(
                    "api_budget_config",
                    f"{self.api}:{now.date().isoformat()}",
                    f"API limits config ({self.label}) is broken: {problem}. "
                    "The minimum of the file and the defaults applies.",
                    now,
                )
        if self._creds is not None and self._paused():
            # leave the alert (deduplicated per period) for the next run to find out loudly
            limit, warn = self._limits()
            api_budget.reserve(store, self.api, self.label, limit, warn, now)

    def nothing_to_do(self) -> bool:
        return self._creds is None or self._paused()

    def skip_reason(self) -> str:
        return "not_configured" if self._creds is None else "budget"

    def _cursor_key(self) -> str:
        return f"rotation:{self.api}"

    def _selected(self, store: Store) -> Iterator[tuple[int, Call]]:
        calls = self.plan()
        start = int(store.state_get(self._cursor_key()) or 0) % len(calls)
        for i in range(min(self.calls_per_run, len(calls))):
            yield (start + i + 1) % len(calls), calls[(start + i) % len(calls)]

    def fetch_partial(self, http: HttpClient, limit: int | None = None) -> FetchOutcome:
        store, now, creds = self._store, self._now, self._creds
        if store is None or now is None or creds is None:
            raise RuntimeError(f"{self.name}: before_run was not called or no credentials")
        secrets = self.secrets()
        extra_params, headers = self.auth(creds)
        lim, warn = self._limits()
        seen: set[str] = set()
        out: list[RawPosting] = []
        failures: list[RequestFailure] = []
        first_exc: FetchError | None = None
        total = 0
        for next_cursor, call in self._selected(store):
            if not api_budget.reserve(store, self.api, self.label, lim, warn, now):
                break  # budget reached: stop here, the rest waits
            total += 1
            store.state_set(self._cursor_key(), str(next_cursor), now)
            path = urlsplit(call.url).path
            try:
                resp = http.get(
                    call.url,
                    params={**call.params, **extra_params},
                    headers=headers,
                    min_delay_s=self.min_delay_s,
                    allowed_hosts=self.allowed_hosts,
                    max_attempts=1,  # every attempt is a counted call: a failure waits a cycle
                )
                for raw in self.parse(resp.content, call.url):
                    if raw.source_id not in seen:
                        seen.add(raw.source_id)
                        out.append(raw)
            except _PARTIAL_ERRORS as exc:
                status = exc.status if isinstance(exc, FetchError) else None
                root = exc.__cause__ if isinstance(exc, FetchError) and exc.__cause__ else exc
                kind = type(root).__name__
                failures.append(RequestFailure(path, kind, status))
                if first_exc is None:
                    msg = scrub(str(exc), secrets)
                    first_exc = FetchError(msg, url=path, status=status, attempts=1)
                    first_exc.__cause__ = None
                if status in (401, 403, 429):
                    break  # rate limited or bad key: do not hammer the API
            if limit is not None and len(out) >= limit:
                break
        if failures and len(failures) == total and first_exc is not None:
            raise first_exc
        if limit is not None:
            out = out[:limit]
        return FetchOutcome(out, failures, total)
