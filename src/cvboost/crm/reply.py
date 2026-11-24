"""The worker's reply step: one queued owner request -> one Opus call -> a draft in the DB.

Shares the CV step's budget rules (pause, daily cap, cost cap) and its alias: the subscription
is one. Calls are logged as llm_calls kind 'cv' with this prompt's version. Nothing is sent.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from cvboost.config import Settings
from cvboost.crm import store as S
from cvboost.crm.outreach import OutreachConfig, is_email, load_outreach
from cvboost.cv.profile import GapsConfig, Profile
from cvboost.cv.step import CvStats, load_resume_inputs
from cvboost.llm import client as C
from cvboost.llm.budget import PAUSE_STATUSES, daily_gate, fmt_local, local_day, pause_for
from cvboost.llm.client import LLMClient, LLMRequest, LLMResult
from cvboost.llm.config import LLMConfig
from cvboost.llm.prompts import registry
from cvboost.llm.prompts import reply_v1 as P
from cvboost.storage.db import Store

log = logging.getLogger("cvboost.crm")

_MONEY = re.compile(r"[$€£]\s?\d|\d\s?(USD|EUR|GBP)\b|\d\s?(dollars|euros)\b", re.IGNORECASE)


class ReplyDraft(BaseModel):
    """The LinkedIn reply, plus an optional e-mail (all three e-mail fields or none)."""

    model_config = ConfigDict(extra="forbid")
    draft: str = Field(min_length=1, max_length=P.DRAFT_MAX + 500)
    injection_suspected: bool = False
    email_to: str | None = Field(default=None, max_length=254)
    email_subject: str | None = Field(default=None, max_length=P.SUBJECT_MAX + 50)
    email_body: str | None = Field(default=None, max_length=P.EMAIL_BODY_MAX + 500)

    @model_validator(mode="after")
    def _email_block(self) -> ReplyDraft:
        parts = [(self.email_to or "").strip(), (self.email_subject or "").strip(),
                 (self.email_body or "").strip()]  # fmt: skip
        if not any(parts):
            self.email_to = self.email_subject = self.email_body = None
            return self
        if not all(parts):
            raise ValueError("email needs to, subject and body")
        if not is_email(parts[0]):
            raise ValueError("email_to is not an e-mail address")
        if "\n" in parts[1] or "\r" in parts[1]:
            raise ValueError("subject must be one line")
        self.email_to, self.email_subject, self.email_body = parts
        return self


def violations(
    out: ReplyDraft,
    gaps: GapsConfig,
    outreach: OutreachConfig,
    found_emails: list[str] | None = None,
) -> list[str]:
    """Checks on every text the owner may send. `found_emails` are the addresses present in the
    contact's newest message: email_to must be one of them, copied exactly, never invented."""
    texts = [out.draft, out.email_subject or "", out.email_body or ""]
    joined = "\n".join(texts)
    v = [
        f"gap_term:{g.id}" for g in gaps.gaps if any(re.search(p, joined, re.I) for p in g.patterns)
    ]
    if _MONEY.search(joined):
        v.append("money")
    if outreach.foreign_links(*texts):
        v.append("link")
    if len(out.draft) > P.DRAFT_MAX:
        v.append("too_long")
    if len(out.email_body or "") > P.EMAIL_BODY_MAX or len(out.email_subject or "") > P.SUBJECT_MAX:
        v.append("email_too_long")
    if found_emails:
        if out.email_to is None:
            v.append("email_missing")
        elif out.email_to not in found_emails:
            v.append("email_address_not_in_contact_text")
    elif out.email_to is not None:
        v.append("email_address_not_in_contact_text")
    return v


def _feedback(v: list[str]) -> str:
    return (
        "The previous draft was rejected: "
        + ", ".join(v)
        + ". Remove tools or skills that are not in the profile, any money amounts and any link "
        "that is not in <approved_links>; keep the LinkedIn draft under 900 characters. "
        "email_to must be an address copied exactly from <latest_message_emails>, and the e-mail "
        "block is required when that list has an address and forbidden when it is empty."
    )


def run_reply_step(
    store: Store,
    client: LLMClient,
    cfg: LLMConfig,
    settings: Settings,
    clock: Callable[[], datetime],
) -> CvStats:
    """Process at most one queued reply request. Never raises for LLM or validation problems."""
    stats = CvStats()
    cv = cfg.cv
    if not cv.enabled:
        stats.stop_code = "disabled"
        return stats
    conn = store.conn
    now = clock()
    alias, tz = cfg.cv_alias, cfg.timezone
    with store.transaction():
        S.recover_stale_replies(
            conn, now - timedelta(seconds=2 * cv.timeout_s), cv.max_attempts, now
        )
    if not S.has_queued_reply(conn, cv.max_attempts):
        return stats
    day = local_day(now, tz)
    try:
        registry.current()  # no user profile: the step stops with a message
        profile, gaps = load_resume_inputs(settings)
    except ValueError as exc:
        stats.stop_code, stats.stopped = "config", f"resume config: {exc}"[:300]
        return stats
    try:
        outreach = load_outreach(settings.outreach_path)
    except ValueError as exc:
        stats.stop_code, stats.stopped = "config", f"outreach config: {exc}"[:300]
        return stats
    pause = store.active_pause(now)
    if pause:
        stats.stop_code = "paused"
        stats.stopped = f"paused until {fmt_local(pause[0], tz)} ({pause[1]})"
        return stats
    closed = daily_gate(store, cfg, alias, day)
    if closed:
        stats.stop_code, stats.stopped = closed
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

    with store.transaction():
        req = S.claim_next_reply(conn, clock(), cv.max_attempts)
    if req is None:
        return stats
    stats.claimed = 1
    _process(store, client, cfg, clock, profile, gaps, outreach, req, stats)
    return stats


def _process(store, client, cfg, clock, profile: Profile, gaps, outreach, req, stats) -> None:
    conn, cv, alias, tz = store.conn, cfg.cv, cfg.cv_alias, cfg.timezone
    rid, attempt = req["id"], req["attempts"]
    contact = S.contact(conn, req["contact_id"])
    if contact is None:
        with store.transaction():
            S.finish_reply(conn, rid, status="cancelled", now=clock(), error="contact removed")
        return
    events = S.events(conn, contact["id"], P.EVENTS_MAX)
    t = clock()
    with store.transaction():
        call_id = store.insert_llm_call(
            kind="cv",
            model_alias=alias,
            prompt_version=registry.current().reply.version,
            n_items=1,
            attempt=attempt,
            now=t,
            local_day=local_day(t, tz),
            status="started",
        )
    stats.calls += 1
    llm_req = LLMRequest(
        model_alias=alias,
        system_prompt=registry.current().reply.build_system_prompt(
            profile.prompt_text, outreach.links
        ),
        json_schema=P.json_schema(),
        task=P.TASK,
        stdin_text=P.render_contact(
            contact, events, req["feedback"], owner_context=contact["owner_context"]
        ),
        timeout_s=cv.timeout_s,
        max_budget_usd=cv.max_budget_usd_per_call,
    )
    try:
        res = client.run(llm_req)
    except Exception as exc:  # noqa: BLE001 - any backend failure is an accounted error
        res = LLMResult(status=C.ERROR, error_message=repr(exc)[:300])
    stats.cost_usd += res.total_cost_usd or 0.0

    out: ReplyDraft | None = None
    status = res.status
    if status == C.OK:
        try:
            out = ReplyDraft.model_validate(res.structured)
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
    stats.lines.append(f"call #{call_id} {alias} reply request {rid} {status}")

    def fail(err: str) -> None:
        with store.transaction():
            S.finish_reply(conn, rid, status="failed", now=clock(), error=err, call_id=call_id,
                           prompt_version=registry.current().reply.version)  # fmt: skip
        stats.failed += 1

    if pause is not None:  # usage limit / auth: not the draft's fault, the attempt is refunded
        with store.transaction():
            S.requeue_reply(conn, rid, feedback=req["feedback"], refund=True)
        stats.requeued += 1
        stats.stop_code = "paused"
        stats.stopped = f"paused until {fmt_local(pause, tz)} ({status})"
        return
    if out is None:
        if attempt < cv.max_attempts:
            with store.transaction():
                S.requeue_reply(conn, rid, feedback=req["feedback"], refund=False)
            stats.requeued += 1
        else:
            fail(f"{status}: {res.error_message or ''}"[:200])
        return
    if out.injection_suspected:
        fail("injection_suspected")
        return
    bad = violations(out, gaps, outreach, P.latest_emails(events))
    if bad:
        if attempt < cv.max_attempts:
            with store.transaction():
                S.requeue_reply(conn, rid, feedback=_feedback(bad), refund=False)
            stats.requeued += 1
            stats.lines.append("rejected: " + ", ".join(bad))
        else:
            fail("draft rejected: " + ", ".join(bad))
        return
    now = clock()
    with store.transaction():
        ok = S.finish_reply(
            conn, rid, status="done", now=now, draft=out.draft.strip(), call_id=call_id,
            prompt_version=registry.current().reply.version, profile_sha=profile.sha,
            email=(out.email_to, out.email_subject, out.email_body) if out.email_to else None,
        )  # fmt: skip
        if ok:
            store.insert_system_alert(
                "crm_reply_ready",
                str(rid),
                f"Reply draft ready: {contact['name']}. Open the CRM in the panel.",
                now,
            )
    if ok:
        stats.done += 1
