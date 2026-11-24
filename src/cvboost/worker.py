"""`cvboost worker`: the 10-minute loop. collect -> triage -> score -> notify -> cv, forever.

One process, one cycle at a time (the loop is synchronous, so cycles never overlap). A second
thread for Telegram buttons is attached through `WorkerDeps.start_bot` (see notify/bot.py).
Every step of a cycle is isolated: a failing step is logged and counted, the next one still runs.
"""

from __future__ import annotations

import logging
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from cvboost import connectors as connector_registry
from cvboost import email_extract
from cvboost.collect import RunResult, collect
from cvboost.config import Settings
from cvboost.connectors.base import Connector
from cvboost.core.rules import RulesConfig, load_config, rule_version
from cvboost.crm.reply import run_reply_step
from cvboost.cv.render import WeasyRenderer
from cvboost.cv.step import CvStats, run_cv_step
from cvboost.http import HttpClient
from cvboost.llm.client import LLMClient, make_client
from cvboost.llm.config import LLMConfig, load_llm_config
from cvboost.llm.prompts import registry
from cvboost.locks import LockBusy, LockHandle, holder_info, pipeline_lock, try_lock
from cvboost.score import ScoreStats, score
from cvboost.storage.db import Store, connect, migrate
from cvboost.triage import triage
from cvboost.worker_config import Backfill, WorkerConfig, load_worker_config

log = logging.getLogger("cvboost.worker")

CV_ALERT_STOPS = {"config", "daily_cap", "daily_cost_cap"}
MIN_PAUSE_S = 30  # a cycle that overran its tick still waits this long
CV_POLL_S = 15  # between cycles: a new "Prepare CV" request starts within this many seconds
CV_BURST_MAX = 5  # fast path: at most this many queued kits back to back
EXIT_LOCKED = 3

Clock = Callable[[], datetime]
NotifyStep = Callable[[Store, datetime, "CycleStats"], None]
BotStarter = Callable[[Settings, WorkerConfig, threading.Event], threading.Thread | None]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class CycleStats:
    status: str = "ok"
    sources_run: int = 0
    sources_deferred: int = 0
    sources_failed: int = 0
    new_jobs: int = 0
    scored: int = 0
    score_calls: int = 0
    score_stopped: str | None = None
    email_calls: int = 0  # Haiku calls of the e-mail fallback this cycle
    email_jobs: int = 0
    email_stopped: str | None = None
    score_code: str | None = None
    cv_done: int = 0  # kits finished this cycle (done, needs_review or failed); log only
    cv_stopped: str | None = None
    alerts_sent: int = 0
    alerts_summarized: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class WorkerDeps:
    cfg: WorkerConfig
    rules: RulesConfig
    llm_cfg: LLMConfig
    data_dir: Path
    connectors: list[Connector]
    http: HttpClient
    clock: Clock = utc_now
    stop: threading.Event = field(default_factory=threading.Event)
    # the LLM client is built on first use so a cycle without pending groups never needs `claude`
    llm_client: LLMClient | None = None
    score_fn: Callable[..., ScoreStats] = score
    # e-mail fallback (#17b): (store, client, llm_cfg, clock) -> EmailExtractStats
    email_fn: Callable[..., email_extract.EmailExtractStats] = email_extract.run
    notify: NotifyStep | None = None
    # CV drafting (#7): (store, llm client, clock) -> CvStats; None = switched off
    cv_step: Callable[[Store, LLMClient, Clock], CvStats] | None = None
    start_bot: BotStarter | None = None
    on_start: Callable[[Store, datetime], None] | None = None
    closers: list[Callable[[], None]] = field(default_factory=list)
    lock_wait_s: float = 60.0
    lock_sleep: Callable[[float], None] = time.sleep

    def client(self) -> LLMClient:
        if self.llm_client is None:
            self.llm_client = make_client(self.llm_cfg)
        return self.llm_client


def _hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def backfill_night(now: datetime, cfg: WorkerConfig) -> str | None:
    """Key (local date the night started) if `now` is inside the backfill window, else None."""
    bf: Backfill = cfg.backfill
    if not bf.enabled:
        return None
    local = now.astimezone(ZoneInfo(cfg.timezone))
    cur = (local.hour, local.minute)
    start, end = _hhmm(bf.window[0]), _hhmm(bf.window[1])
    if start <= end:
        inside = start <= cur < end
        day = local.date()
    else:  # window wraps midnight, e.g. 23:00-05:00
        inside = cur >= start or cur < end
        day = local.date() if cur >= start else local.date() - timedelta(days=1)
    return day.isoformat() if inside else None


def calls_for_cycle(store: Store, cfg: WorkerConfig, now: datetime) -> tuple[int, str | None]:
    """(LLM call budget for this cycle, backfill night key if the budget is the night one)."""
    night = backfill_night(now, cfg)
    if night is None:
        return cfg.score_max_calls_per_cycle, None
    used = int(store.state_get(f"backfill:{night}") or 0)
    left = cfg.backfill.max_calls_per_night - used
    if left <= 0:
        return cfg.score_max_calls_per_cycle, None
    return max(cfg.score_max_calls_per_cycle, min(cfg.backfill.calls_per_cycle, left)), night


def _step(stats: CycleStats, name: str, fn: Callable[[], None]) -> None:
    """Run one cycle step; an exception is logged and counted, never propagated."""
    try:
        fn()
    except Exception as exc:
        log.exception("cycle step %s failed", name)
        stats.errors.append(f"{name}: {type(exc).__name__}")


def _run_pipeline(store: Store, deps: WorkerDeps, t0: datetime, stats: CycleStats) -> None:
    cfg = deps.cfg

    def do_collect() -> None:
        store.mark_stale_runs(timedelta(hours=1), t0)
        results: list[RunResult] = collect(
            store,
            deps.http,
            deps.connectors,
            limit=None,
            force=False,
            clock=deps.clock,
            deadline=t0 + timedelta(seconds=cfg.collect_budget_s),
            should_stop=deps.stop.is_set,
        )
        for r in results:
            if r.skip_reason == "deferred":
                stats.sources_deferred += 1
            elif r.status != "skipped":
                stats.sources_run += 1
                stats.new_jobs += r.new
                if r.status == "error":
                    stats.sources_failed += 1

    def do_email() -> None:
        if deps.stop.is_set() or not any(
            c.name == "email_alerts" and c.configured() for c in deps.connectors
        ):
            return
        now = deps.clock()
        # one Haiku call can take llm timeout_s; leave time for scoring after it
        if now + timedelta(seconds=2 * deps.llm_cfg.timeout_s) > t0 + timedelta(
            seconds=cfg.cycle_deadline_s
        ):
            stats.email_stopped = "no time left in this cycle"
            return
        st = deps.email_fn(store, deps.client(), deps.llm_cfg, deps.clock)
        stats.new_jobs += st.new_jobs
        stats.email_calls, stats.email_jobs, stats.email_stopped = st.calls, st.new_jobs, st.stopped

    def do_triage() -> None:
        triage(store, deps.rules, deps.clock())

    def do_score() -> None:
        if deps.stop.is_set():
            return
        now = deps.clock()
        # keep room for one LLM call (it can take llm timeout_s) inside the cycle deadline
        latest_start = t0 + timedelta(seconds=cfg.cycle_deadline_s - deps.llm_cfg.timeout_s)
        if now >= latest_start:
            stats.score_stopped = "no time left in this cycle"
            return
        calls, night = calls_for_cycle(store, cfg, now)
        st = deps.score_fn(
            store,
            deps.client(),
            deps.llm_cfg,
            rule_version(deps.rules),
            deps.rules.relevance.threshold,
            deps.clock,  # score reads the clock before every gate and call
            max_calls=calls,
        )
        stats.scored, stats.score_calls, stats.score_stopped = st.scored, st.calls, st.stopped
        stats.score_code = getattr(st, "stop_code", None)
        if night is not None and st.calls:
            used = int(store.state_get(f"backfill:{night}") or 0) + st.calls
            store.state_set(f"backfill:{night}", str(used), now)

    def do_rescore() -> None:
        # the owner's "Re-score with the new description" (#41): one Haiku call for one group,
        # under the same pause and daily caps; a closed gate leaves the request queued
        if deps.stop.is_set():
            return
        req = store.queued_rescore()
        if req is None:
            return
        st = deps.score_fn(
            store,
            deps.client(),
            deps.llm_cfg,
            rule_version(deps.rules),
            deps.rules.relevance.threshold,
            deps.clock,
            max_calls=1,
            only_canonical=req["canonical_id"],
            rules_cfg=deps.rules,
        )
        if getattr(st, "stopped", None):
            return
        store.finish_rescore(req["id"], "failed" if st.errors else "done", deps.clock())

    def do_cv() -> None:
        if deps.stop.is_set() or deps.cv_step is None:
            return
        st = deps.cv_step(store, deps.client(), deps.clock)
        stats.cv_done, stats.cv_stopped = st.finished, st.stopped
        # "CV ready" goes out in this cycle; job alerts were already sent before the Opus call
        if deps.notify is not None and (st.finished or st.stop_code in CV_ALERT_STOPS):
            deps.notify(store, deps.clock(), stats)

    _step(stats, "collect", do_collect)
    _step(stats, "email_extract", do_email)
    if not deps.stop.is_set():
        _step(stats, "triage", do_triage)
    _step(stats, "score", do_score)
    _step(stats, "rescore", do_rescore)
    if deps.notify is not None:
        notify = deps.notify
        _step(stats, "notify", lambda: notify(store, deps.clock(), stats))
    _step(stats, "cv", do_cv)


def run_cv_now(store: Store, deps: WorkerDeps) -> CycleStats:
    """Fast path between cycles: run only the CV step for a fresh request (owner pressed
    "Prepare CV"). Skips quietly if a cycle or the CLI holds the pipeline."""
    stats = CycleStats()
    if deps.cv_step is None or deps.stop.is_set():
        return stats
    try:
        with pipeline_lock(deps.data_dir, wait_s=0):
            # several requests queued at once (e.g. the owner rebuilt a few kits): run them back
            # to back instead of one per 10-min cycle; daily caps still apply inside the step
            for _ in range(CV_BURST_MAX):
                st = deps.cv_step(store, deps.client(), deps.clock)
                stats.cv_done += st.finished
                stats.cv_stopped = st.stopped
                if deps.notify is not None and (st.finished or st.stop_code in CV_ALERT_STOPS):
                    deps.notify(store, deps.clock(), stats)
                if not st.finished or st.stopped or deps.stop.is_set():
                    break
                if not _bookkeeping("cv poll", store.newest_queued_cv_request):
                    break
    except LockBusy:
        log.info("cv fast path: pipeline busy, the cycle will take the request")
    except Exception:  # never kill the loop
        log.exception("cv fast path failed")
        stats.errors.append("cv fast path")
    if stats.cv_done:
        log.info("cv fast path: %d kit(s) finished", stats.cv_done)
    return stats


def _wait_with_cv_poll(store: Store, deps: WorkerDeps, seconds: float) -> None:
    """Sleep until the next cycle, but start a CV kit as soon as a new request is queued.
    Each request id triggers the fast path once; a paused/capped request waits for the cycle."""
    if deps.cv_step is None:
        deps.stop.wait(seconds)
        return
    seen = _bookkeeping("cv poll", store.newest_queued_cv_request) or 0
    deadline = deps.clock() + timedelta(seconds=seconds)
    while not deps.stop.is_set():
        left = (deadline - deps.clock()).total_seconds()
        if left <= 0:
            return
        if deps.stop.wait(min(CV_POLL_S, left)):
            return
        newest = _bookkeeping("cv poll", store.newest_queued_cv_request) or 0
        if newest > seen:
            seen = newest
            run_cv_now(store, deps)


def _bookkeeping[T](what: str, fn: Callable[[], T]) -> T | None:
    """Cycle book-keeping writes must never kill the worker loop (e.g. 'database is locked')."""
    try:
        return fn()
    except sqlite3.Error:
        log.warning("%s: database write failed, continuing", what, exc_info=True)
        return None


def run_cycle(store: Store, deps: WorkerDeps) -> CycleStats:
    t0 = deps.clock()
    stats = CycleStats()
    cycle_id = _bookkeeping("start_cycle", lambda: store.start_cycle(t0))
    try:
        with pipeline_lock(deps.data_dir, wait_s=deps.lock_wait_s, sleep=deps.lock_sleep):
            _run_pipeline(store, deps, t0, stats)
    except LockBusy as exc:
        log.warning("cycle skipped: pipeline busy (%s)", exc.holder)
        stats.errors.append("pipeline busy")
    if stats.errors:
        stats.status = "error"
    elif stats.sources_failed:
        stats.status = "partial"
    now = deps.clock()
    if cycle_id is not None:
        _bookkeeping(
            "finish_cycle",
            lambda: store.finish_cycle(
                cycle_id,
                status=stats.status,
                now=now,
                sources_run=stats.sources_run,
                sources_deferred=stats.sources_deferred,
                new_jobs=stats.new_jobs,
                scored=stats.scored,
                score_stopped=stats.score_stopped,
                alerts_sent=stats.alerts_sent,
                alerts_summarized=stats.alerts_summarized,
                error="; ".join(stats.errors) or None,
            ),
        )
    _bookkeeping("heartbeat", lambda: store.state_set("heartbeat", now.isoformat(), now))
    log.info(
        "cycle %s: sources %d run / %d deferred / %d failed, new %d, scored %d, alerts %d+%d, "
        "cv %d, email calls %d/jobs %d%s",
        stats.status,
        stats.sources_run,
        stats.sources_deferred,
        stats.sources_failed,
        stats.new_jobs,
        stats.scored,
        stats.alerts_sent,
        stats.alerts_summarized,
        stats.cv_done,
        stats.email_calls,
        stats.email_jobs,
        f", errors: {'; '.join(stats.errors)}" if stats.errors else "",
    )
    return stats


def worker_healthy(settings: Settings, now: datetime | None = None) -> bool:
    """True if the last cycle finished less than 3 ticks ago. Read-only, takes no locks."""
    cfg = load_worker_config(settings.worker_config_path)
    if not settings.db_path.exists():
        return False
    conn = connect(settings.db_path)
    try:
        beat = Store(conn).state_get_dt("heartbeat")
    except Exception:
        return False
    finally:
        conn.close()
    now = now or utc_now()
    return beat is not None and now - beat <= timedelta(minutes=3 * cfg.tick_minutes)


def make_cv_step(settings: Settings, llm_cfg: LLMConfig, renderer) -> Callable:
    """CV kits first; a CRM reply draft (#33) only in a cycle where the CV step spent no call
    (one Opus call per cycle, same daily cap)."""

    def cv_and_reply(store: Store, client: LLMClient, clock: Clock) -> CvStats:
        st = run_cv_step(store, client, llm_cfg, settings, renderer, clock)
        if st.calls or st.stopped:
            return st
        return run_reply_step(store, client, llm_cfg, settings, clock)

    return cv_and_reply


def build_deps(settings: Settings, stop: threading.Event) -> WorkerDeps:
    from cvboost.notify.wiring import attach_telegram

    deps = WorkerDeps(
        cfg=load_worker_config(settings.worker_config_path),
        rules=load_config(settings.rules_path),
        llm_cfg=load_llm_config(settings.llm_config_path),
        data_dir=settings.data_dir,
        connectors=connector_registry.get(
            None,
            companies_path=settings.companies_path,
            rules_path=settings.rules_path,
            imap=settings.imap_creds(),
            forwarders=settings.mail_forwarders,
        ),
        http=HttpClient(),
        stop=stop,
    )
    if deps.llm_cfg.cv.enabled:
        renderer = WeasyRenderer(settings.resume_dir)

        deps.cv_step = make_cv_step(settings, deps.llm_cfg, renderer)
    attach_telegram(deps, settings)
    return deps


def run_worker(
    settings: Settings,
    *,
    once: bool = False,
    deps_factory: Callable[[Settings, threading.Event], WorkerDeps] = build_deps,
    install_signals: bool = False,
    stop: threading.Event | None = None,
) -> int:
    """Run until SIGTERM/SIGINT (or one cycle with `once`). Returns the process exit code."""
    lock: LockHandle | None = try_lock(settings.data_dir / "worker.lock")
    if lock is None:
        print(
            f"another worker is running ({holder_info(settings.data_dir / 'worker.lock')})",
            file=sys.stderr,
        )
        return EXIT_LOCKED
    stop = stop or threading.Event()
    old_handlers: dict[int, object] = {}
    bot: threading.Thread | None = None
    conn = None
    deps: WorkerDeps | None = None
    try:
        if registry.ensure_loaded(settings) is None:
            log.warning("%s", registry.not_loaded_message())  # collect and triage still run
        deps = deps_factory(settings, stop)  # config errors raise ValueError -> exit 2 in the CLI
        conn = connect(settings.db_path, busy_ms=15000)  # the bot thread writes too
        migrate(conn)
        store = Store(conn)
        store.sync_sources({c.name: type(c) for c in deps.connectors}, deps.clock())
        now = deps.clock()
        store.interrupt_running_cycles(now)
        if store.state_get("alerts_since") is None:
            store.state_set("alerts_since", now.isoformat(), now)  # nothing older is alerted
        if deps.on_start is not None:
            deps.on_start(store, now)
        if install_signals:
            for sig in (signal.SIGTERM, signal.SIGINT):
                old_handlers[sig] = signal.signal(sig, lambda *_: stop.set())
        if not once and deps.start_bot is not None:
            bot = deps.start_bot(settings, deps.cfg, stop)
        tick_s = deps.cfg.tick_minutes * 60
        log.info(
            "worker started: tick %d min, %d sources", deps.cfg.tick_minutes, len(deps.connectors)
        )
        while not stop.is_set():
            t0 = deps.clock()
            stats = run_cycle(store, deps)
            if once:
                return 1 if stats.status == "error" else 0
            elapsed = (deps.clock() - t0).total_seconds()
            _wait_with_cv_poll(store, deps, max(tick_s - elapsed, MIN_PAUSE_S))
        log.info("worker stopping")
        return 0
    finally:
        stop.set()
        if bot is not None:
            bot.join(timeout=35)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)  # type: ignore[arg-type]
        if deps is not None:
            deps.http.close()
            for close in deps.closers:
                close()
        if conn is not None:
            conn.close()
        lock.release()


__all__ = ["CycleStats", "WorkerDeps", "run_cycle", "run_worker", "worker_healthy"]
