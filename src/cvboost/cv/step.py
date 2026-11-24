"""The worker's CV step: one queued request -> one Opus call -> validated draft -> kit on disk.

One call per run (the worker runs it once per cycle). A rejected draft is retried in the NEXT
cycle (the request goes back to `queued` with a fixed-template feedback), at most
`cv.max_attempts` calls per request. Shared budget rules (pause, daily caps) come from
`llm.budget`: the subscription is one for scoring and CV.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import ValidationError

from cvboost.config import Settings
from cvboost.cv import kit as K
from cvboost.cv.profile import GapsConfig, Profile, check_gaps, load_gaps, load_profile
from cvboost.cv.render import Renderer, RenderError, RendererUnavailable
from cvboost.cv.schema import CoverDraft, CvDraft
from cvboost.cv.validate import (
    Violation,
    feedback_text,
    needs_retry,
    validate,
    validate_cover,
)
from cvboost.llm import client as C
from cvboost.llm.budget import PAUSE_STATUSES, daily_gate, fmt_local, local_day, pause_for
from cvboost.llm.client import LLMClient, LLMRequest, LLMResult
from cvboost.llm.config import LLMConfig
from cvboost.llm.prompts import cv_v1 as P
from cvboost.llm.prompts import registry
from cvboost.notify import alerts as N
from cvboost.storage.db import Store
from cvboost.textutil import clean_text

log = logging.getLogger("cvboost.cv")

_REC_TEXT = {"apply": "apply", "apply_with_caveats": "apply with caveats", "skip": "skip"}


@dataclass
class CvStats:
    claimed: int = 0
    calls: int = 0
    done: int = 0
    needs_review: int = 0
    failed: int = 0
    requeued: int = 0
    cost_usd: float = 0.0
    stopped: str | None = None
    stop_code: str | None = (
        None  # paused | daily_cap | daily_cost_cap | preflight | config | disabled
    )
    lines: list[str] = field(default_factory=list)

    @property
    def finished(self) -> int:
        return self.done + self.needs_review + self.failed


def load_resume_inputs(settings: Settings) -> tuple[Profile, GapsConfig]:
    """Profile and gaps.yaml, checked against each other. ValueError describes the problem."""
    profile = load_profile(settings.resume_dir)
    gaps = load_gaps(settings.resume_dir)
    problems = check_gaps(profile, gaps)
    if problems:
        raise ValueError("; ".join(problems))
    return profile, gaps


def fit_notes_md(draft: CvDraft) -> str:
    n = draft.fit_notes_ru

    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {x}" for x in items) or "- none"

    return (
        f"# Job analysis\n\nRecommendation: **{_REC_TEXT[n.recommendation]}**\n\n{n.summary}\n\n"
        f"## Matches\n{bullets(n.matches)}\n\n## Risks\n{bullets(n.risks)}\n\n"
        f"## Gaps\n{bullets(n.gaps)}\n"
    )


def recruiter_note_md(draft: CvDraft) -> str:
    m = draft.recruiter_message
    return f"# LinkedIn connection note\n\n{m.connection_note}\n\n# InMail\n\n{m.inmail}\n"


def _fail_text(status: str, message: str | None) -> str:
    return clean_text(f"{status}: {message or ''}".strip(": "))[:200]


def run_cv_step(
    store: Store,
    client: LLMClient,
    cfg: LLMConfig,
    settings: Settings,
    renderer: Renderer,
    clock: Callable[[], datetime],
    *,
    request_id: int | None = None,
    ignore_pause: bool = False,
    lock_wait_s: float = 20.0,
    sleep: Callable[[float], None] = time.sleep,
) -> CvStats:
    """Process at most one queued request. `request_id` (CLI) takes that request instead of the
    oldest queued one. Never raises for LLM, validation or render problems."""
    stats = CvStats()
    cv = cfg.cv
    if not cv.enabled:
        stats.stop_code = "disabled"
        return stats
    now = clock()
    alias = cfg.cv_alias
    tz = cfg.timezone

    with store.transaction():
        _, exhausted = store.recover_stale_cv_requests(
            now - timedelta(seconds=2 * cv.timeout_s), cv.max_attempts, now
        )
        for rid in exhausted:  # nobody else would ever tell the owner about these
            row = store.cv_request(rid)
            job = store.job_for_llm(row["job_id"], None)
            failed = N.cover_failed_text if row["kind"] == "cover" else N.cv_failed_text
            text = failed(job.title, job.company, "attempts used up")
            store.insert_system_alert("cv_failed", str(rid), text, now)
            stats.failed += 1
    if request_id is not None:
        row = store.cv_request(request_id)
        if row is None or row["status"] != "queued":
            stats.stopped = f"request {request_id} is not queued"
            return stats
    elif not store.has_queued_cv_request(cv.max_attempts):
        return stats

    day = local_day(now, tz)
    try:
        registry.current()  # no user profile: the step stops with a message, nothing crashes
        profile, gaps = load_resume_inputs(settings)
    except ValueError as exc:
        stats.stop_code, stats.stopped = "config", f"resume config: {exc}"[:300]
        store.insert_system_alert("cv_config", day, N.cv_config_text(str(exc)), now)
        return stats

    pause = None if ignore_pause else store.active_pause(now)
    if pause:
        stats.stop_code = "paused"
        stats.stopped = f"paused until {fmt_local(pause[0], tz)} ({pause[1]})"
        return stats
    closed = daily_gate(store, cfg, alias, day)
    if closed:
        stats.stop_code, stats.stopped = closed
        store.insert_system_alert(
            "cv_stopped", f"{day}:{closed[0]}", N.cv_stopped_text(closed[1]), now
        )
        return stats

    pre = client.preflight()
    t = clock()
    pre_status = pre.status if pre.ok or pre.status in PAUSE_STATUSES else C.ERROR
    until = pause_for(store, cfg, pre_status, t) if pre_status in PAUSE_STATUSES else None
    with store.transaction():
        store.insert_llm_call(
            kind="preflight",
            model_alias=alias,
            prompt_version=None,
            n_items=0,
            attempt=1,
            now=t,
            local_day=local_day(t, tz),
            status=C.OK if pre.ok else pre_status,
            error_message=pre.message,
            auth_method=pre.auth_method,
            pause_until=until,
            credential_expires_at=pre.credential_expires_at,
        )
    if not pre.ok:
        stats.stop_code = "preflight"
        stats.stopped = f"preflight failed ({pre_status}: {pre.message})"
        return stats

    t = clock()
    if request_id is not None:
        with store.transaction():
            claimed = store.claim_cv_request(request_id, t, cv.max_attempts)
    else:
        with store.transaction():
            claimed = store.claim_next_cv_request(t, cv.max_attempts)
    if claimed is None:
        return stats
    stats.claimed = 1
    # the kit is locked BEFORE the paid call and stays locked until the kit is written, so a
    # busy kit costs nothing and a finished draft is never thrown away for a busy lock
    try:
        K.kit_dir(settings.kits_dir, claimed["canonical_id"])
        lock = K.kit_lock(
            settings.kits_dir, claimed["canonical_id"], wait_s=lock_wait_s, sleep=sleep
        )
    except K.KitBusy:
        with store.transaction():  # nothing was spent: the attempt goes back
            store.requeue_cv_request(claimed["id"], feedback=claimed["feedback"], refund=True)
        stats.requeued += 1
        stats.stopped = f"kit {claimed['canonical_id']} is busy"
        return stats
    except (K.KitError, ValueError, OSError) as exc:
        log.error("kit path refused for %s: %s", claimed["canonical_id"], exc)
        _fail(store, claimed, "kit path refused", None, clock(), stats)
        return stats
    try:
        _process(store, client, cfg, settings, renderer, clock, profile, gaps, claimed, stats)
    finally:
        lock.release()
    return stats


def _process(
    store: Store,
    client: LLMClient,
    cfg: LLMConfig,
    settings: Settings,
    renderer: Renderer,
    clock: Callable[[], datetime],
    profile: Profile,
    gaps: GapsConfig,
    req,
    stats: CvStats,
) -> None:
    if req["kind"] == "cover":
        _process_cover(store, client, cfg, settings, renderer, clock, profile, gaps, req, stats)
        return
    cv, alias, tz = cfg.cv, cfg.cv_alias, cfg.timezone
    rid, attempt = req["id"], req["attempts"]
    job = store.job_for_llm(req["job_id"], None)
    sections = profile.section_names
    t = clock()
    with store.transaction():
        call_id = store.insert_llm_call(
            kind="cv",
            model_alias=alias,
            prompt_version=registry.current().cv.version,
            n_items=1,
            attempt=attempt,
            now=t,
            local_day=local_day(t, tz),
            status="started",
        )
    stats.calls += 1
    llm_req = LLMRequest(
        model_alias=alias,
        system_prompt=registry.current().cv.build_system_prompt(profile.prompt_text, sections),
        json_schema=P.json_schema(sections),
        task=P.TASK,
        stdin_text=P.render_job(
            job, cv.description, req["feedback"], store.owner_override_note(req["job_id"])
        ),
        timeout_s=cv.timeout_s,
        max_budget_usd=cv.max_budget_usd_per_call,
    )
    try:
        res = client.run(llm_req)
    except Exception as exc:  # noqa: BLE001 - any backend failure is an accounted error
        res = LLMResult(status=C.ERROR, error_message=repr(exc)[:300])
    stats.cost_usd += res.total_cost_usd or 0.0

    draft: CvDraft | None = None
    status = res.status
    if status == C.OK:
        try:
            draft = CvDraft.model_validate(res.structured)
        except ValidationError:
            status = C.INVALID_OUTPUT
    t_end = clock()
    pause = pause_for(store, cfg, status, t_end)
    with store.transaction():
        store.update_llm_call(
            call_id,
            status=status,
            model=res.model,
            duration_ms=res.duration_ms,
            exit_code=res.exit_code,
            input_tokens=res.input_tokens,
            output_tokens=res.output_tokens,
            cache_read_tokens=res.cache_read_tokens,
            cache_creation_tokens=res.cache_creation_tokens,
            total_cost_usd=res.total_cost_usd,
            error_message=res.error_message,
            pause_until=pause,
            credential_expires_at=res.credential_expires_at,
        )
    stats.lines.append(f"call #{call_id} {alias} request {rid} {status}")

    if pause is not None:  # usage limit / auth: not the draft's fault, the attempt is refunded
        with store.transaction():
            store.requeue_cv_request(rid, feedback=req["feedback"], refund=True)
        stats.requeued += 1
        stats.stop_code = "paused"
        stats.stopped = f"paused until {fmt_local(pause, tz)} ({status})"
        return

    if draft is None:  # timeout / error / invalid_output / budget: the attempt is used
        _retry_or_fail(
            store, req, status, res.error_message, cv.max_attempts, stats, call_id, settings, t_end
        )
        return

    violations = validate(draft, profile, gaps)
    if violations and needs_retry(violations) and attempt < cv.max_attempts:
        with store.transaction():
            store.requeue_cv_request(rid, feedback=feedback_text(violations, gaps), refund=False)
        stats.requeued += 1
        stats.lines.append("rejected: " + ", ".join(v.reason for v in violations))
        return
    _publish(store, settings, renderer, clock, req, draft, violations, call_id, profile.sha, stats)


def _fail(store, req, err, call_id, now, stats) -> None:
    with store.transaction():
        ok = store.finish_cv_request(
            req["id"],
            status="failed",
            now=now,
            error=err,
            call_id=call_id,
            prompt_version=registry.current().cv.cover_version
            if req["kind"] == "cover"
            else registry.current().cv.version,
        )
        if ok:
            job = store.job_for_llm(req["job_id"], None)
            failed = N.cover_failed_text if req["kind"] == "cover" else N.cv_failed_text
            text = failed(job.title, job.company, err)
            store.insert_system_alert("cv_failed", str(req["id"]), text, now)
    if ok:
        stats.failed += 1


def _retry_or_fail(store, req, status, message, max_attempts, stats, call_id, settings, now):
    if req["attempts"] < max_attempts:
        with store.transaction():
            store.requeue_cv_request(req["id"], feedback=req["feedback"], refund=False)
        stats.requeued += 1
        return
    _fail(store, req, _fail_text(status, message), call_id, now, stats)


def _publish(store, settings, renderer, clock, req, draft, violations, call_id, profile_sha, stats):
    rid, cid = req["id"], req["canonical_id"]
    reasons = [v.reason for v in violations]
    now = clock()
    cur = store.cv_request(rid)
    if cur is None or cur["status"] != "in_progress":
        return  # cancelled while the model worked: write nothing, change nothing
    files: dict[str, bytes | str] = {
        K.RESUME_MD: draft.resume_md,
        K.RECRUITER_NOTE: recruiter_note_md(draft),
        K.FIT_NOTES: fit_notes_md(draft),
        K.CLAIMS_JSON: json.dumps(
            [c.model_dump() for c in draft.claims], ensure_ascii=False, indent=2
        ),
    }
    if draft.cover_letter_md.strip():
        files[K.COVER_MD] = draft.cover_letter_md
        try:
            files[K.COVER_PDF] = renderer.render_letter(draft.cover_letter_md).pdf
        except RenderError as exc:  # an unavailable renderer is flagged below by the resume PDF
            reasons.append("cover_pdf_failed")
            log.warning("cover letter pdf failed: %s", exc)
        except RendererUnavailable:
            pass
    pdf_status = "ok"
    try:
        files[K.RESUME_PDF] = renderer.render(draft.resume_md).pdf
    except RendererUnavailable as exc:
        pdf_status = "unavailable"
        reasons.append("pdf_unavailable")
        log.warning("pdf renderer unavailable: %s", exc)
    except RenderError as exc:
        pdf_status = "failed"
        reasons.append("pdf_failed")
        log.warning("pdf render failed: %s", exc)
    try:
        K.write_kit(settings.kits_dir, cid, files)
    except (K.KitError, OSError) as exc:
        log.error("kit write failed for %s: %s", cid, exc)
        _fail(store, req, "kit write failed", call_id, now, stats)
        return
    status = "needs_review" if reasons else "done"
    job = store.job_for_llm(req["job_id"], None)
    with store.transaction():
        ok = store.finish_cv_request(
            rid,
            status=status,
            now=now,
            kit_dir=str(cid),
            review_reasons=reasons or None,
            prompt_version=registry.current().cv.version,
            profile_sha=profile_sha,
            call_id=call_id,
            pdf_status=pdf_status,
            recommendation=draft.fit_notes_ru.recommendation,
            fit_summary=draft.fit_notes_ru.summary,
        )
        if ok:
            if status == "done":
                text = N.cv_ready_text(
                    job.title, job.company, draft.fit_notes_ru.recommendation, cid,
                    settings.panel_url,
                )  # fmt: skip
                store.insert_system_alert("cv_ready", str(rid), text, now)
            else:
                text = N.cv_review_text(job.title, job.company, reasons, cid, settings.panel_url)
                store.insert_system_alert("cv_review", str(rid), text, now)
    if ok:
        if status == "done":
            stats.done += 1
        else:
            stats.needs_review += 1


def _process_cover(
    store, client, cfg, settings, renderer, clock, profile, gaps, req, stats: CvStats
) -> None:
    """A cover-letter-only request (#32): one Opus call of the same budget and the same caps as a
    full kit; only cover_letter.md/pdf are written, the resume and the CV verdict stay."""
    cv, alias, tz = cfg.cv, cfg.cv_alias, cfg.timezone
    rid, attempt = req["id"], req["attempts"]
    job = store.job_for_llm(req["job_id"], None)
    sections = profile.section_names
    t = clock()
    with store.transaction():
        call_id = store.insert_llm_call(
            kind="cv",
            model_alias=alias,
            prompt_version=registry.current().cv.cover_version,
            n_items=1,
            attempt=attempt,
            now=t,
            local_day=local_day(t, tz),
            status="started",
        )
    stats.calls += 1
    llm_req = LLMRequest(
        model_alias=alias,
        system_prompt=registry.current().cv.build_cover_system_prompt(
            profile.prompt_text, sections
        ),
        json_schema=P.cover_json_schema(sections),
        task=P.COVER_TASK,
        stdin_text=P.render_job(
            job, cv.description, req["feedback"], store.owner_override_note(req["job_id"])
        ),
        timeout_s=cv.timeout_s,
        max_budget_usd=cv.max_budget_usd_per_call,
    )
    try:
        res = client.run(llm_req)
    except Exception as exc:  # noqa: BLE001 - any backend failure is an accounted error
        res = LLMResult(status=C.ERROR, error_message=repr(exc)[:300])
    stats.cost_usd += res.total_cost_usd or 0.0

    draft: CoverDraft | None = None
    status = res.status
    if status == C.OK:
        try:
            draft = CoverDraft.model_validate(res.structured)
        except ValidationError:
            status = C.INVALID_OUTPUT
    t_end = clock()
    pause = pause_for(store, cfg, status, t_end)
    with store.transaction():
        store.update_llm_call(
            call_id,
            status=status,
            model=res.model,
            duration_ms=res.duration_ms,
            exit_code=res.exit_code,
            input_tokens=res.input_tokens,
            output_tokens=res.output_tokens,
            cache_read_tokens=res.cache_read_tokens,
            cache_creation_tokens=res.cache_creation_tokens,
            total_cost_usd=res.total_cost_usd,
            error_message=res.error_message,
            pause_until=pause,
            credential_expires_at=res.credential_expires_at,
        )
    stats.lines.append(f"call #{call_id} {alias} cover request {rid} {status}")

    if pause is not None:
        with store.transaction():
            store.requeue_cv_request(rid, feedback=req["feedback"], refund=True)
        stats.requeued += 1
        stats.stop_code = "paused"
        stats.stopped = f"paused until {fmt_local(pause, tz)} ({status})"
        return
    if draft is None:
        _retry_or_fail(
            store, req, status, res.error_message, cv.max_attempts, stats, call_id, settings, t_end
        )
        return

    violations = validate_cover(draft.cover_letter_md, draft.cover_claims, profile, gaps)
    if draft.injection_suspected:
        violations.append(Violation("injection_suspected", "", "injection"))
    if violations and needs_retry(violations) and attempt < cv.max_attempts:
        with store.transaction():
            store.requeue_cv_request(rid, feedback=feedback_text(violations, gaps), refund=False)
        stats.requeued += 1
        stats.lines.append("rejected: " + ", ".join(v.reason for v in violations))
        return

    reasons = [v.reason for v in violations]
    now = clock()
    cur = store.cv_request(rid)
    if cur is None or cur["status"] != "in_progress":
        return  # cancelled while the model worked
    cid = req["canonical_id"]
    pdf = None
    try:
        pdf = renderer.render_letter(draft.cover_letter_md).pdf
    except RendererUnavailable as exc:
        reasons.append("cover_pdf_unavailable")
        log.warning("pdf renderer unavailable: %s", exc)
    except RenderError as exc:
        reasons.append("cover_pdf_failed")
        log.warning("cover letter pdf failed: %s", exc)
    try:
        K.write_one(settings.kits_dir, cid, K.COVER_MD, draft.cover_letter_md)
        if pdf is not None:
            K.write_one(settings.kits_dir, cid, K.COVER_PDF, pdf)
        else:
            K.retire_pdf(settings.kits_dir, cid, K.COVER_PDF)
    except (K.KitError, OSError) as exc:
        log.error("cover letter write failed for %s: %s", cid, exc)
        _fail(store, req, "kit write failed", call_id, now, stats)
        return
    status = "needs_review" if reasons else "done"
    with store.transaction():
        ok = store.finish_cover_request(
            rid,
            status=status,
            now=now,
            review_reasons=reasons or None,
            prompt_version=registry.current().cv.cover_version,
            profile_sha=profile.sha,
            call_id=call_id,
        )
        if ok:
            job = store.job_for_llm(req["job_id"], None)
            if status == "done":
                text = N.cover_ready_text(job.title, job.company, cid, settings.panel_url)
            else:
                text = N.cover_review_text(job.title, job.company, reasons, cid, settings.panel_url)
            store.insert_system_alert(
                "cv_ready" if status == "done" else "cv_review", str(rid), text, now
            )
    if ok:
        if status == "done":
            stats.done += 1
        else:
            stats.needs_review += 1
