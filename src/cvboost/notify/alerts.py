"""Alert outbox: pick groups worth an alert, send them once, keep the books in `alerts`.

Delivery is at-least-once: a row is `pending` until Telegram confirms, so a crash between the send
and the `sent` update can repeat one message. Losing an alert is worse than a duplicate.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from cvboost import plugins, stub_alert
from cvboost.core.freshness import closed_reason
from cvboost.crm.remind import queue_crm_reminders
from cvboost.llm.prompts import registry
from cvboost.llm.schema import JobVerdict
from cvboost.notify import format as F
from cvboost.notify.redact import redact
from cvboost.notify.telegram import TelegramClient, TelegramError
from cvboost.storage.db import Store
from cvboost.textutil import clean_text
from cvboost.worker_config import AlertsCfg, WorkerConfig

log = logging.getLogger("cvboost.alerts")

AUTH_STOP = {401, 403, 404}  # token revoked / bot blocked / chat unknown: stop sending this cycle


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def in_quiet_hours(cfg: WorkerConfig, now: datetime) -> bool:
    q = cfg.alerts.quiet_hours
    if q is None:
        return False
    local = now.astimezone(ZoneInfo(cfg.timezone)).strftime("%H:%M")
    if q.start <= q.end:
        return q.start <= local < q.end
    return local >= q.start or local < q.end  # wraps midnight, e.g. 23:00-08:00


@dataclass
class SelectStats:
    pending: int = 0
    skipped_closed: int = 0
    skipped_backlog: int = 0


def _closed(m, now: datetime, max_age_days: int) -> str | None:
    try:
        raw = json.loads(m["raw_json"] or "{}")
    except ValueError:
        raw = {}
    return closed_reason(
        m["source"], _dt(m["posted_at"]), _dt(m["first_seen_at"]), raw, now, max_age_days
    )


def alive_members(store: Store, canonical_id: int, now: datetime, max_age_days: int):
    return [m for m in store.group_members(canonical_id) if _closed(m, now, max_age_days) is None]


def select_candidates(
    store: Store,
    cfg: WorkerConfig,
    now: datetime,
    *,
    kept_hashes: dict[int, str] | None = None,
    prompt_version: str | None = None,
) -> SelectStats:
    """Record an `alerts` row for every scored group that is new, eligible and above the threshold.

    A group gets exactly one row, ever (`canonical_id` is unique). `kept_hashes` maps the groups the
    rules still keep to the input hash `score` would use for them now: a verdict whose hash differs
    is stale (text or representative changed) and waits for re-scoring; a rule change that rejected
    the group drops it. A verdict that flags `prompt_injection` is never alerted.
    """
    a: AlertsCfg = cfg.alerts
    stats = SelectStats()
    prompt_version = prompt_version or registry.score_version()  # no profile: matches nothing
    since = store.state_get_dt("alerts_since")
    lag_cutoff = now - timedelta(hours=a.max_alert_lag_h)
    done_canon, done_jobs = store.alerted_ids()
    for sg in store.llm_verdicts(prompt_version):
        v = sg.verdict
        if not v.eligible or v.fit < a.fit_threshold:
            continue
        if kept_hashes is not None and kept_hashes.get(sg.canonical_id) != sg.input_hash:
            continue
        if has_injection(v):
            continue
        if sg.canonical_id in done_canon:
            continue
        members = store.group_members(sg.canonical_id)
        if any(m["id"] in done_jobs for m in members):
            continue  # groups merged after one member was already alerted
        if all(_closed(m, now, a.max_age_days) is not None for m in members):
            status = "skipped_closed"
        else:
            first_seen = min(_dt(m["first_seen_at"]) for m in members)
            if (since is not None and first_seen < since) or first_seen < lag_cutoff:
                status = "skipped_backlog"
            else:
                status = "pending"
        store.insert_alert(
            canonical_id=sg.canonical_id,
            job_id=sg.job_id,
            llm_verdict_id=store.llm_verdict_row_id(sg.job_id, prompt_version, sg.input_hash),
            fit=v.fit,
            status=status,
            now=now,
        )
        setattr(stats, status, getattr(stats, status) + 1)
    return stats


def has_injection(v: JobVerdict) -> bool:
    return any("prompt_injection" in f.lower() for f in v.red_flags)


def build_view(
    store: Store, alert, verdict: JobVerdict, now: datetime, cfg: WorkerConfig
) -> F.AlertView | None:
    """Card content from the verdict the alert was created for; None if every member has closed."""
    members = alive_members(store, alert["canonical_id"], now, cfg.alerts.max_age_days)
    if not members:
        return None
    rep = next((m for m in members if m["id"] == alert["job_id"]), members[0])
    v = verdict
    rule_flags = _rule_flags(store, [m["id"] for m in members])
    plat = stub_alert.platform_of([m["source"] for m in members])
    warnings = [
        f"check the description on {plat or 'the platform'}" if f == stub_alert.FLAG else f
        for f in [*rule_flags, *v.red_flags]
    ][:5]
    rate = v.rate_text or next((m["salary_text"] for m in members if m["salary_text"]), None)
    return F.AlertView(
        title=rep["title"],
        company=rep["company"],
        rate=rate,
        location=rep["location_text"],
        fit=v.fit,
        profile=None if v.profile == "none" else v.profile,
        employment=v.employment,
        seniority=v.seniority,
        summary=v.summary_ru,
        warnings=warnings,
        links=[(m["source"], m["url"]) for m in members],
    )


def _rule_flags(store: Store, job_ids: list[int]) -> list[str]:
    flags: list[str] = []
    for v in store.latest_verdicts(None, job_ids).values():
        for f in json.loads(v["flags_json"]):
            if f not in flags:
                flags.append(f)
    return flags


@dataclass
class SendStats:
    sent: int = 0
    summarized: int = 0
    system: int = 0
    stopped: str | None = None
    systemic: bool = False


def _qualifies(v: JobVerdict, threshold: int) -> bool:
    return v.eligible and v.fit >= threshold and not has_injection(v)


def _classify(exc: TelegramError) -> str:
    """auth | rate | down | reject.

    auth/rate/down say nothing about the message itself: stop sending this cycle and do not count
    an attempt (the same alert is retried next cycle). Only a 4xx for this very message is a
    `reject` that counts."""
    if exc.status in AUTH_STOP:
        return "auth"
    if exc.status == 400 and "chat not found" in exc.description.lower():
        return "auth"  # the chat id is wrong or the owner never pressed /start: not the message
    if exc.status == 429:
        return "rate"
    if exc.status is None or exc.status >= 500 or 300 <= exc.status < 400:
        return "down"
    return "reject"


def send_pending(
    store: Store,
    tg: TelegramClient,
    cfg: WorkerConfig,
    chat_id: int,
    now: datetime,
    *,
    sleep: Callable[[float], None] = time.sleep,
    token: str | None = None,
    kept_hashes: dict[int, str] | None = None,
) -> SendStats:
    """Send system alerts, then up to `max_per_cycle` cards, then one summary for the rest.

    Just before sending, every pending alert is re-checked against its own verdict. If the group's
    input hash has changed since (`kept_hashes`), the verdict for the current hash is used: it
    replaces the old one if it still qualifies (the alert is rebound and sent), the alert waits if
    there is none yet, and becomes `skipped_stale` if it no longer qualifies.

    A 4xx rejection counts as an attempt only if it is about that one message: when two different
    messages of one cycle are rejected with the same description the cause is systemic, the cycle
    stops and nothing is counted."""
    deferred: list[tuple[str, Callable[[], None]]] = []
    out = _send_all(store, tg, cfg, chat_id, now, sleep, token, kept_hashes, deferred)
    if not out.systemic:
        for _, count in deferred:
            count()
    return out


def _send_all(
    store: Store,
    tg: TelegramClient,
    cfg: WorkerConfig,
    chat_id: int,
    now: datetime,
    sleep: Callable[[float], None],
    token: str | None,
    kept_hashes: dict[int, str] | None,
    deferred: list[tuple[str, Callable[[], None]]],
) -> SendStats:
    out = SendStats()
    if in_quiet_hours(cfg, now):
        return out  # everything stays pending until the quiet hours end
    a = cfg.alerts

    def stop_on(exc: TelegramError) -> bool:
        kind = _classify(exc)
        if kind == "auth":
            out.stopped = f"telegram refused ({exc.status}); bot token or chat id is wrong"
            log.error("%s", redact(str(exc), token))
        elif kind == "rate":
            wait = min(exc.retry_after or 5, 60)
            out.stopped = f"rate limited, retry_after {exc.retry_after}"
            log.warning("telegram rate limit: pausing %ss", wait)
            sleep(wait)
        elif kind == "down":
            out.stopped = "telegram unreachable or failing; will retry next cycle"
            log.warning("%s", redact(str(exc), token))
        return kind != "reject"

    def systemic(description: str) -> bool:
        """The same 4xx for a second, different message: not about the message."""
        if any(d == description.lower() for d, _ in deferred):
            out.systemic = True
            out.stopped = "same 4xx for different messages; treated as systemic, nothing counted"
            log.error("%s: %s", out.stopped, description)
            return True
        return False

    for sa in store.pending_system_alerts():
        try:
            text = (
                clean_text(sa["text"])[:1000]
                if sa["kind"] in CV_KINDS or sa["kind"] in plugins.trusted_alert_kinds()
                else F.safe_text(sa["text"], 1000)
            )
            tg.send_message(chat_id, text)
        except TelegramError as exc:
            err = redact(str(exc), token)
            log.warning("system alert %s not sent: %s", sa["kind"], err)
            if stop_on(exc):
                return out
            if systemic(exc.description):
                return out
            deferred.append(
                (
                    exc.description.lower(),
                    lambda sa=sa, err=err: store.system_alert_attempt_failed(
                        sa["id"], err, a.max_attempts
                    ),
                )
            )
            continue
        store.set_system_alert_status(sa["id"], "sent", now)
        out.system += 1

    lag_cutoff = now - timedelta(hours=a.max_alert_lag_h)
    cards: list[tuple[object, F.AlertView]] = []
    rest: list[tuple[object, F.AlertView]] = []
    for alert in store.pending_alerts():
        if datetime.fromisoformat(alert["created_at"]) < lag_cutoff:
            store.set_alert_status([alert["id"]], "skipped_backlog", now)  # waited too long
            continue
        stored = store.verdict_by_id(alert["llm_verdict_id"])
        current = kept_hashes.get(alert["canonical_id"]) if kept_hashes is not None else None
        if kept_hashes is not None and (stored is None or stored[1] != current):
            # the text (or representative) changed after the alert was queued: judge the verdict
            # for the current text, not the old one
            found = (
                store.llm_verdict_for_hash(alert["canonical_id"], registry.score_version(), current)
                if current is not None
                else None
            )
            if found is None:
                if current is None:  # the rules no longer keep this group
                    store.set_alert_status([alert["id"]], "skipped_stale", now)
                continue  # not scored yet: stays pending (the lag cutoff still applies)
            vid, verdict, vjob = found
            if not _qualifies(verdict, a.fit_threshold):
                store.set_alert_status([alert["id"]], "skipped_stale", now)
                continue
            store.rebind_alert(alert["id"], vid, vjob, verdict.fit)
            alert = store.alert(alert["id"])
        elif stored is None or not _qualifies(stored[0], a.fit_threshold):
            store.set_alert_status([alert["id"]], "skipped_stale", now)
            continue
        else:
            verdict = stored[0]
        view = build_view(store, alert, verdict, now, cfg)
        if view is None:
            store.set_alert_status([alert["id"]], "skipped_closed", now)
            continue
        (cards if len(cards) < a.max_per_cycle else rest).append((alert, view))

    for alert, view in cards:
        text = F.render_alert(view)
        try:
            msg = tg.send_message(chat_id, text, F.keyboard(alert["id"], "initial"))
        except TelegramError as exc:
            err = redact(str(exc), token)
            if stop_on(exc):
                log.warning("alert %s not sent: %s", alert["id"], err)
                return out
            log.warning("alert %s rejected: %s", alert["id"], err)
            if systemic(exc.description):
                return out
            deferred.append(
                (
                    exc.description.lower(),
                    lambda alert=alert, err=err: store.mark_alert_attempt_failed(
                        alert["id"], err, a.max_attempts
                    ),
                )
            )
            continue
        store.mark_alert_sent(
            alert["id"],
            chat_id=chat_id,
            message_id=int(msg["message_id"]),
            text=text,
            now=now,
        )
        out.sent += 1

    if rest:
        items = [(v.fit, v.title, v.company) for _, v in rest]
        try:
            tg.send_message(chat_id, F.render_summary(items, a.summary_max_lines))
        except TelegramError as exc:
            log.warning("summary not sent: %s", redact(str(exc), token))
            stop_on(exc)
        else:
            store.set_alert_status([al["id"] for al, _ in rest], "summarized", now)
            out.summarized += len(rest)
    return out


# --- system alerts (the owner must hear about these; the web panel is not enough) ---


def _day(now: datetime, cfg: WorkerConfig) -> str:
    return now.astimezone(ZoneInfo(cfg.timezone)).date().isoformat()


def _parse_utc(value: str | None) -> datetime | None:
    """ISO timestamp (a trailing Z is fine); naive means UTC; garbage means None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def queue_system_alerts(
    store: Store,
    cfg: WorkerConfig,
    now: datetime,
    *,
    score_code: str | None = None,
    score_text: str | None = None,
) -> int:
    """Insert (deduplicated) system alerts for LLM trouble and failing sources."""
    day = _day(now, cfg)
    n = 0

    def add(kind: str, key: str, text: str) -> None:
        nonlocal n
        n += store.insert_system_alert(kind, key, text, now)

    pause = store.last_pause_call()
    pause_until = _parse_utc(pause["pause_until"]) if pause is not None else None
    if pause is not None and pause_until is not None and pause_until > now:
        until = pause_until.astimezone(ZoneInfo(cfg.timezone))
        when = until.strftime("%H:%M")
        if pause["status"] == "usage_limit":
            add(
                "llm_usage_limit",
                day,
                "Claude subscription limit reached. LLM scoring is paused until "
                f"{when} ({cfg.timezone}). Collection continues.",
            )
        else:
            add(
                "llm_auth",
                day,
                "LLM stopped: Claude authorisation error "
                f"({pause['status']}). Collection continues. Update CLAUDE_CODE_OAUTH_TOKEN "
                "(claude setup-token). Next attempt no earlier than "
                f"{when} ({cfg.timezone}).",
            )
    detail = F.safe_text(score_text, 200)
    if score_code == "daily_cap":
        add(
            "llm_daily_cap",
            day,
            f"Daily LLM call limit reached ({detail}). New jobs wait for scoring until tomorrow.",
        )
    elif score_code == "daily_cost_cap":
        add(
            "llm_cost_cap",
            day,
            f"Daily LLM cost limit reached ({detail}). New jobs wait for scoring until tomorrow.",
        )
    _, expires = store.last_auth_info()
    exp = _parse_utc(expires)
    if exp is not None and exp < now + timedelta(days=cfg.alerts.credential_warn_days):
        state = "has expired" if exp < now else f"expires {exp.date().isoformat()}"
        add(
            "credential_expiring",
            day,
            f"The Claude token {state}. Update CLAUDE_CODE_OAUTH_TOKEN (claude setup-token).",
        )
    for h in store.source_health():
        if h.enabled and h.consecutive_failures >= cfg.alerts.source_fail_alert_after:
            episode = h.last_success_at.isoformat() if h.last_success_at else "never"
            add(
                "source_failing",
                f"{h.name}:{episode}",
                f"Source {h.name} has failed {h.consecutive_failures} runs in a row: "
                f"{F.safe_text(h.last_error, 200)}",
            )
    n += queue_crm_reminders(store, cfg.timezone, now)
    return n


# --- CV kit alerts (#7). Kinds cv_*: the text is built here from safe_text pieces plus the owner's
# own CVBOOST_PANEL_URL, so it is sent as is (safe_text would turn the link into "[link]").

CV_KINDS = ("cv_ready", "cv_review", "cv_failed", "cv_config", "cv_stopped")
_RECOMMENDATION_TEXT = {
    "apply": "apply",
    "apply_with_caveats": "apply with caveats",
    "skip": "skip",
}


def _cv_job(title: str, company: str) -> str:
    return f"{F.safe_text(title, 80)} — {F.safe_text(company, 60)}"


def _cv_link(canonical_id: int, panel_url: str | None) -> str:
    if panel_url:
        return f"{panel_url.rstrip('/')}/jobs/{canonical_id}"
    return f"Panel → job #{canonical_id}"


def cv_ready_text(
    title: str, company: str, recommendation: str | None, canonical_id: int, panel_url: str | None
) -> str:
    rec = _RECOMMENDATION_TEXT.get(recommendation or "", "see the analysis in the kit")
    return (
        f"CV ready: {_cv_job(title, company)}. Recommendation: {rec}. "
        f"{_cv_link(canonical_id, panel_url)}"
    )


def cv_review_text(
    title: str, company: str, reasons: list[str], canonical_id: int, panel_url: str | None
) -> str:
    why = ", ".join(F.safe_text(r, 40) for r in reasons[:6]) or "needs review"
    return (
        f"CV needs review: {_cv_job(title, company)}. Reasons: {why}. "
        f"{_cv_link(canonical_id, panel_url)}"
    )


def cv_failed_text(title: str, company: str, error: str | None) -> str:
    return f"CV failed: {_cv_job(title, company)}. {F.safe_text(error, 200)}"


def cover_ready_text(title: str, company: str, canonical_id: int, panel_url: str | None) -> str:
    return f"Cover letter ready: {_cv_job(title, company)}. {_cv_link(canonical_id, panel_url)}"


def cover_review_text(
    title: str, company: str, reasons: list[str], canonical_id: int, panel_url: str | None
) -> str:
    why = ", ".join(F.safe_text(r, 40) for r in reasons[:6]) or "needs review"
    return (
        f"Cover letter needs review: {_cv_job(title, company)}. Reasons: {why}. "
        f"{_cv_link(canonical_id, panel_url)}"
    )


def cover_failed_text(title: str, company: str, error: str | None) -> str:
    return f"Cover letter failed: {_cv_job(title, company)}. {F.safe_text(error, 200)}"


def cv_config_text(problem: str) -> str:
    return (
        "CV cannot start: check config/resume (profile.md, gaps.yaml, cvboost cv "
        f"check-profile). {F.safe_text(problem, 300)}"
    )


def cv_stopped_text(detail: str) -> str:
    return f"CV is waiting: {F.safe_text(detail, 200)}. The request stays in the queue."


def worker_started_alert(store: Store, cfg: WorkerConfig, now: datetime) -> None:
    hour = now.astimezone(ZoneInfo(cfg.timezone)).strftime("%Y-%m-%d %H")
    store.insert_system_alert("worker_started", hour, "CVBoost worker started.", now)


__all__ = [
    "CV_KINDS",
    "cover_failed_text",
    "cover_ready_text",
    "cover_review_text",
    "cv_config_text",
    "cv_failed_text",
    "cv_ready_text",
    "cv_review_text",
    "cv_stopped_text",
    "SendStats",
    "SelectStats",
    "build_view",
    "in_quiet_hours",
    "queue_system_alerts",
    "select_candidates",
    "send_pending",
    "worker_started_alert",
]
