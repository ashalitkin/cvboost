"""Run connectors and store what they found."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cvboost.connectors.ats import ATS_CYCLE_BUDGET_S, ATSConnector
from cvboost.connectors.base import Connector
from cvboost.http import FetchError, HttpClient
from cvboost.schedule import is_due, next_due_at
from cvboost.storage.db import Store

log = logging.getLogger("cvboost.collect")

Clock = Callable[[], datetime]


def _clock(now: datetime | None, clock: Clock | None) -> Clock:
    """`now` pins the time (tests, CLI one-shots); `clock` reads real time per call."""
    if clock is not None:
        return clock
    if now is None:
        raise TypeError("pass now= or clock=")
    return lambda: now


@dataclass
class RunResult:
    source: str
    status: str  # ok | error | skipped
    found: int = 0
    new: int = 0
    invalid: int = 0
    error_type: str | None = None
    error_message: str | None = None
    http_status: int | None = None
    skip_reason: str | None = None


def _describe(exc: Exception) -> tuple[str, str, int | None]:
    status = exc.status if isinstance(exc, FetchError) else None
    root = exc.__cause__ if isinstance(exc, FetchError) and exc.__cause__ else exc
    return type(root).__name__, str(exc), status


def collect_source(
    store: Store,
    http: HttpClient,
    connector: Connector,
    *,
    limit: int | None,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> RunResult:
    clk = _clock(now, clock)
    now = clk()  # run start; the finish time is read again so duration_ms is real
    try:
        connector.before_run(store, now)
    except Exception as exc:  # e.g. an unreadable companies file: a failed run, not a crash
        etype, msg, status = _describe(exc)
        log.warning("%s: before_run failed: %s: %s", connector.name, etype, msg)
        run_id = store.start_run(connector.name, now)
        store.finish_run(
            run_id,
            status="error",
            found=0,
            new=0,
            invalid=0,
            error_type=etype,
            error_message=msg,
            http_status=status,
            now=clk(),
        )
        return RunResult(connector.name, "error", error_type=etype, error_message=msg)
    if connector.nothing_to_do():
        # no run row: an empty "ok" run would reset the failure streak of a source whose
        # companies are all waiting out a backoff
        return RunResult(connector.name, "skipped", skip_reason=connector.skip_reason())
    run_id = store.start_run(connector.name, now)
    try:
        outcome = connector.fetch_partial(http, limit)
    except Exception as exc:  # one source must never stop the others
        etype, msg, status = _describe(exc)
        log.warning("%s: fetch failed: %s: %s", connector.name, etype, msg)
        store.finish_run(
            run_id,
            status="error",
            found=0,
            new=0,
            invalid=0,
            error_type=etype,
            error_message=msg,
            http_status=status,
            now=clk(),
        )
        return RunResult(
            connector.name, "error", error_type=etype, error_message=msg, http_status=status
        )

    raws = outcome.raws
    found, new, invalid = len(raws), 0, 0
    try:
        with store.transaction():
            for raw in raws:
                try:
                    job = connector.normalise(raw, now)
                except Exception:
                    invalid += 1
                    log.warning("%s: cannot normalise %s", connector.name, raw.source_id)
                    continue
                _, is_new = store.upsert_job(job, run_id, now)
                new += is_new
            if found > 0 and invalid == found:
                status, etype, msg = (
                    "error",
                    "AllInvalid",
                    f"all {found} postings failed to normalise",
                )
            elif outcome.failures:
                # partial success: the feeds that worked are stored and the run counts as ok
                status, etype = "ok", "partial"
                msg = (
                    f"{len(outcome.failures)}/{outcome.requests_total} feeds failed: "
                    + "; ".join(f.describe() for f in outcome.failures)
                )[:500]
            else:
                status, etype, msg = "ok", None, None
            store.finish_run(
                run_id,
                status=status,
                found=found,
                new=new,
                invalid=invalid,
                error_type=etype,
                error_message=msg,
                now=clk(),
            )
    except Exception as exc:
        etype, msg, _ = _describe(exc)
        log.exception("%s: storing failed", connector.name)
        store.finish_run(
            run_id,
            status="error",
            found=found,
            new=0,
            invalid=invalid,
            error_type=etype,
            error_message=msg,
            now=clk(),
        )
        return RunResult(
            connector.name,
            "error",
            found=found,
            invalid=invalid,
            error_type=etype,
            error_message=msg,
        )
    try:
        connector.after_run(store, clk())
    except Exception:  # state bookkeeping must not turn a stored run into an error
        log.exception("%s: after_run failed", connector.name)
    return RunResult(connector.name, status, found, new, invalid, etype, msg)


def collect(
    store: Store,
    http: HttpClient,
    connectors: list[Connector],
    *,
    limit: int | None,
    force: bool,
    now: datetime | None = None,
    clock: Clock | None = None,
    deadline: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
    named: frozenset[str] = frozenset(),
) -> list[RunResult]:
    """Run the due sources, most overdue first. Results come back in `connectors` order.

    With `deadline`, or once `should_stop()` is true, a source not started yet is reported
    `skipped/deferred` and leaves no row in source_runs, so it stays due for the next call.
    """
    clk = _clock(now, clock)
    t0 = clk()
    results: dict[str, RunResult] = {}
    due: list[tuple[datetime, Connector]] = []
    last_start: dict[str, datetime] = {}
    for c in connectors:
        if not c.configured():
            results[c.name] = RunResult(c.name, "skipped", skip_reason="not_configured")
            continue
        state = store.source_state(c.name)
        # a disabled source runs only when explicitly named; --force never overrides this
        if not state.enabled and c.name not in named:
            results[c.name] = RunResult(c.name, "skipped", skip_reason="disabled")
            continue
        interval = (
            timedelta(seconds=state.interval_override_s)
            if state.interval_override_s
            else c.interval
        )
        due_at = next_due_at(interval, state.last_started_at, state.consecutive_failures, t0)
        if not force and not is_due(
            interval, state.last_started_at, state.consecutive_failures, t0
        ):
            results[c.name] = RunResult(c.name, "skipped", skip_reason="not due")
            continue
        due.append((due_at, c))
        if state.last_started_at is not None:
            last_start[c.name] = state.last_started_at
    # ATS boards run last: they are many small requests and must never starve the cheap sources.
    # Among themselves they take turns: the one that started longest ago (never = first) goes
    # first, so a connector the 60 s budget deferred, or a new one, is served before the ones that
    # just ran (sorting by due date alone would put a never-run connector behind all the others).
    never = datetime.min.replace(tzinfo=UTC)
    due.sort(
        key=lambda p: (
            isinstance(p[1], ATSConnector),
            last_start.get(p[1].name, never) if isinstance(p[1], ATSConnector) else p[0],
        )
    )  # stable within a group
    ats_left = ATS_CYCLE_BUDGET_S  # one budget for all ATS connectors of this call
    for _, c in due:
        if (deadline is not None and clk() >= deadline) or (should_stop and should_stop()):
            results[c.name] = RunResult(c.name, "skipped", skip_reason="deferred")
            continue
        if isinstance(c, ATSConnector):
            left = ats_left
            if deadline is not None:
                left = min(left, (deadline - clk()).total_seconds())
            if left <= c.min_delay_s:
                results[c.name] = RunResult(c.name, "skipped", skip_reason="deferred")
                continue
            c.cycle_budget_s = left
            t_start = clk()
            results[c.name] = collect_source(store, http, c, limit=limit, clock=clk)
            ats_left -= (clk() - t_start).total_seconds()
            continue
        results[c.name] = collect_source(store, http, c, limit=limit, clock=clk)
    return [results[c.name] for c in connectors]
