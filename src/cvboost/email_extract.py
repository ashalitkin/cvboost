"""LLM fallback for e-mails the deterministic parsers could not read (17b).

Runs as a separate worker step after collect and before triage: collection stays AI-free. A batch of
messages with status `needs_llm` goes to the cheap model as untrusted data; the answer is
validated against a schema. The model never returns URLs: a job gets a canonical link we computed
earlier (`link_ref`) or a link to the original mail in the owner's Gmail. Limits: its own daily
call cap (`email_extract.daily_calls`, counted by llm_calls.kind='email'), the shared haiku caps and
pauses, `max_calls_per_cycle`. Nothing is called when the subscription is paused.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote

from cvboost.llm import client as C
from cvboost.llm.budget import PAUSE_STATUSES, daily_gate, fmt_local, local_day, pause_for
from cvboost.llm.client import LLMClient, LLMRequest, LLMResult
from cvboost.llm.config import LLMConfig
from cvboost.llm.prompts import email_v1 as P
from cvboost.llm.prompts import registry
from cvboost.llm.schema import EmailVerdict, parse_email_batch
from cvboost.mail.canonical import canonicalise
from cvboost.mail.mask import mask_contacts
from cvboost.models import UNDISCLOSED_COMPANY, Job
from cvboost.storage.db import Store

MAX_ATTEMPTS = 2  # unreadable answers per message before it becomes llm_error
SUB_SOURCES = {"linkedin", "dice", "indeed", "freelancermap", "recruiter"}


@dataclass
class EmailExtractStats:
    pending: int = 0
    calls: int = 0
    new_jobs: int = 0
    not_job: int = 0
    errors: int = 0
    expired: int = 0
    cost_usd: float = 0.0
    stopped: str | None = None
    stop_code: str | None = None
    lines: list[str] = field(default_factory=list)


def gmail_link(message_id: str | None) -> str:
    if not message_id:
        return "https://mail.google.com/mail/#inbox"
    return f"https://mail.google.com/mail/#search/rfc822msgid%3A{quote(message_id, safe='')}"


def _job(verdict_job, row, link_urls: list[str], idx: int, fetched_at: datetime) -> Job:
    platform = row["platform"] if row["platform"] in SUB_SOURCES else "recruiter"
    url = gmail_link(row["message_id"])
    source_id = f"m{row['msgid_hash'][:16]}-{idx}"
    if verdict_job.link_ref and 1 <= verdict_job.link_ref <= len(link_urls):
        link = canonicalise(
            link_urls[verdict_job.link_ref - 1]
        )  # our own canonical URL, re-checked
        if link is not None:
            url, source_id = link.url, link.job_id
            if link.platform in SUB_SOURCES:
                platform = link.platform
    parts = [mask_contacts(row["llm_input"] or "")] if platform == "recruiter" else []
    posted = datetime.fromisoformat(row["received_at"]) if row["received_at"] else None
    return Job(
        source=f"email:{platform}",
        source_id=source_id,
        url=url,
        company=verdict_job.company or UNDISCLOSED_COMPANY,
        title=verdict_job.title,
        description_text="\n".join(parts) or f"{verdict_job.title} (from an e-mail)",
        location_text=verdict_job.location,
        remote=verdict_job.remote,
        employment_type=verdict_job.employment_type,
        salary_text=verdict_job.rate_text,
        posted_at=posted,
        fetched_at=fetched_at,
        raw={
            "platform": platform,
            "job_id": source_id,
            "msgid_hash": row["msgid_hash"],
            "parser_version": registry.email_prompt().version,
        },
    )


def run(
    store: Store,
    client: LLMClient,
    cfg: LLMConfig,
    clock: Callable[[], datetime],
    *,
    max_calls: int | None = None,
    ignore_pause: bool = False,
) -> EmailExtractStats:
    stats = EmailExtractStats()
    ec = cfg.email_extract
    now = clock()
    tz = cfg.timezone
    alias = cfg.email_alias
    with store.transaction():
        stats.expired = store.expire_email_inputs(now, ec.pending_ttl_days)
    if not ec.enabled:
        stats.stop_code, stats.stopped = "disabled", "email_extract is disabled"
        return stats
    cap_calls = ec.max_calls_per_cycle if max_calls is None else max_calls
    pending = store.email_pending(ec.batch_size * max(cap_calls, 1))
    stats.pending = len(pending)
    if not pending or cap_calls <= 0:
        return stats
    ep = registry.email_prompt()  # not personal: runs without a profile
    pause = None if ignore_pause else store.active_pause(now)
    if pause:
        stats.stop_code = "paused"
        stats.stopped = f"paused until {fmt_local(pause[0], tz)} ({pause[1]})"
        return stats

    for i in range(0, len(pending), ec.batch_size):
        if stats.calls >= cap_calls:
            stats.stop_code, stats.stopped = "run_cap", f"per-cycle cap {cap_calls} reached"
            break
        t = clock()
        day = local_day(t, tz)
        gate = daily_gate(store, cfg, alias, day)
        if gate is None and store.calls_today_kind("email", day) >= ec.daily_calls:
            gate = ("email_daily_cap", f"daily e-mail cap {ec.daily_calls} reached")
        if gate:
            stats.stop_code, stats.stopped = gate
            break
        batch = pending[i : i + ec.batch_size]
        refs = [f"e{n + 1}" for n in range(len(batch))]
        stdin = "\n\n".join(
            P.render_email(ref, row["sender_domain"], row["llm_input"] or "")
            for ref, row in zip(refs, batch, strict=True)
        )
        with store.transaction():
            call_id = store.insert_llm_call(
                kind="email",
                model_alias=alias,
                prompt_version=ep.version,
                n_items=len(batch),
                attempt=1,
                now=t,
                local_day=day,
                status="started",
            )
        stats.calls += 1
        try:
            res = client.run(
                LLMRequest(
                    model_alias=alias,
                    system_prompt=ep.system,
                    json_schema=ep.schema,
                    task=ep.task,
                    stdin_text=stdin,
                    timeout_s=cfg.timeout_s,
                    max_budget_usd=cfg.max_budget_usd_per_call,
                )
            )
        except Exception as exc:  # noqa: BLE001 - any backend failure is an accounted error
            res = LLMResult(status=C.ERROR, error_message=repr(exc)[:300])
        stats.cost_usd += res.total_cost_usd or 0.0
        valid: dict[str, EmailVerdict] = {}
        invalid: dict[str, str] = dict.fromkeys(refs, res.status)
        status = res.status
        if status == C.OK:
            valid, invalid = parse_email_batch(res.structured, refs)
            if not valid:
                status = C.INVALID_OUTPUT
            elif invalid:
                status = "partial"
        t_end = clock()
        until = pause_for(store, cfg, status, t_end)
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
                pause_until=until,
                credential_expires_at=res.credential_expires_at,
            )
            for ref, row in zip(refs, batch, strict=True):
                v = valid.get(ref)
                if v is not None:
                    _apply(store, row, v, call_id, t_end, stats)
                elif status in PAUSE_STATUSES:
                    continue  # the limit is not the message's fault: it stays pending, no attempt
                else:
                    failed = row["llm_attempts"] + 1 >= MAX_ATTEMPTS
                    store.finish_email_message(
                        row["id"],
                        status="llm_error" if failed else "needs_llm",
                        note=invalid.get(ref),
                        call_id=call_id,
                        keep_input=not failed,
                    )
                    stats.errors += failed
        stats.lines.append(f"call #{call_id} {alias} {len(batch)} e-mails {status}")
        if until is not None:
            stats.stop_code = "paused"
            stats.stopped = f"paused until {fmt_local(until, tz)} ({status})"
            break
    return stats


def _apply(
    store: Store, row, v: EmailVerdict, call_id: int, now: datetime, stats: EmailExtractStats
) -> None:
    if v.injection:
        store.finish_email_message(
            row["id"], status="not_job", note="prompt_injection", call_id=call_id
        )
        stats.not_job += 1
        return
    if not v.is_job_offer or not v.jobs:
        reason = v.not_job_reason or "no roles found"
        store.finish_email_message(row["id"], status="not_job", note=reason, call_id=call_id)
        stats.not_job += 1
        return
    run_id = row["run_id"] or store.latest_run_id("email_alerts")
    if run_id is None:
        store.finish_email_message(
            row["id"], status="llm_error", note="no source run", call_id=call_id
        )
        stats.errors += 1
        return
    links = json.loads(row["links_json"] or "[]")
    found = 0
    for n, vj in enumerate(v.jobs, 1):
        try:
            job = _job(vj, row, links, n, now)
        except ValueError:  # e.g. an empty title after sanitising
            continue
        _, is_new = store.upsert_job(job, run_id, now)
        found += 1
        stats.new_jobs += is_new
    store.finish_email_message(
        row["id"],
        status="llm_job" if found else "not_job",
        note=None if found else "no valid roles",
        jobs_found=found,
        call_id=call_id,
    )
    stats.not_job += not found
