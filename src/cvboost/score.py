"""LLM scoring step: kept groups after triage -> `claude -p` in batches -> `llm` verdicts.

The LLM only adds a verdict; it never overrides a rule reject and changes no other state.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from cvboost import stub_alert
from cvboost.core.rules import MAX_TEXT, RuleInput, RulesConfig, compile_rules, evaluate
from cvboost.llm import client as C
from cvboost.llm.budget import PAUSE_STATUSES, daily_gate, fmt_local, local_day, pause_for
from cvboost.llm.client import LLMClient, LLMRequest, LLMResult
from cvboost.llm.config import LLMConfig
from cvboost.llm.prompts import registry
from cvboost.llm.prompts import score_v1 as P
from cvboost.llm.prompts.corrections import render_corrections
from cvboost.llm.schema import JobVerdict, parse_batch
from cvboost.storage.db import KeptGroup, LLMJobInput, Store

CIRCUIT_BREAKER_BATCHES = 2
# a pasted description can show what the title did not: these hard rules reject it without a call
TEXT_RULES = (
    "language",
    "us_only",
    "w2_only",
    "onsite",
    "region_restricted",
    "region_excluded",
    "latam_only",
    "region_eastern_europe",
    "region_uk",
)


def _reject_by_rules(
    store: Store,
    g: KeptGroup,
    job: LLMJobInput,
    rules_cfg: RulesConfig | None,
    cfg: LLMConfig,
    now: datetime,
) -> bool:
    """Owner's re-score with a pasted description (#41, #68): run the text-based hard rules over
    the posting plus that text first. True = rejected here, no LLM call."""
    if rules_cfg is None or not (job.extra_scored and job.extra_description):
        return False
    row = store.conn.execute("SELECT * FROM jobs WHERE id = ?", (g.representative_id,)).fetchone()
    inp = RuleInput.from_row(row)
    inp = replace(
        inp, description_text=(inp.description_text + "\n" + job.extra_description)[:MAX_TEXT]
    )
    res = evaluate(inp, compile_rules(rules_cfg), first_seen_at=row["first_seen_at"])
    hit = [r for r in res.reasons if r in TEXT_RULES]
    if not hit:
        return False
    detail = "; ".join(
        f"{r}: {res.details.get('evidence', {}).get(r, '')}".strip(": ") for r in hit
    )
    v = JobVerdict(
        eligible=False,
        eligibility_reason=f"Hard rule on the pasted description: {detail}"[:300],
        fit=0,
        profile="none",
        seniority="unknown",
        employment="unknown",
        rate_text=None,
        red_flags=hit[:6],
        summary_ru="Rejected by the rules on the pasted description: " + ", ".join(hit),
    )
    body = P.render_body(job, g, cfg.scoring.description)
    with store.transaction():
        store.insert_llm_verdict(
            job_id=g.representative_id,
            verdict=v,
            prompt_version=registry.current().score.version,
            item_hash=P.item_hash(body),
            call_id=None,
            model=None,
            cached=False,
            now=now,
        )
    return True


def _stub_rule(v: JobVerdict, g: KeptGroup, job: LLMJobInput | None) -> JobVerdict:
    """#68: only for a verdict written now; stored verdicts and the cache stay as the model gave."""
    if job is None:
        return v
    return stub_alert.apply(
        v,
        sources=g.sources,
        description=job.description_text,
        has_extra=bool(job.extra_description),
        title=job.title,
        location=job.location_text,
    )


FEEDBACK_ITEMS = 15  # owner corrections shown to Haiku (issue #27)
FEEDBACK_WITHDRAWN_MAX = 5  # of them at most this many "Withdrawn" (issue #29)


@dataclass
class _Item:
    group: KeptGroup
    body: str
    hash: str
    job: LLMJobInput | None = None


@dataclass
class ScoreStats:
    kept_groups: int = 0
    pending: int = 0
    cached: int = 0
    calls: int = 0
    scored: int = 0
    keep: int = 0
    reject: int = 0
    errors: int = 0
    skipped_errors: int = 0
    cost_usd: float = 0.0
    stopped: str | None = None
    stop_code: str | None = (
        None  # daily_cap | daily_cost_cap | run_cap | paused | preflight | circuit
    )
    fail_streak: int = 0
    lines: list[str] = field(default_factory=list)


def score(
    store: Store,
    client: LLMClient,
    cfg: LLMConfig,
    rule_version: str,
    threshold: float,
    clock: Callable[[], datetime],
    *,
    limit: int | None = None,
    max_calls: int | None = None,
    retry_errors: bool = False,
    ignore_pause: bool = False,
    dry_run: bool = False,
    only_canonical: int | None = None,
    rules_cfg: RulesConfig | None = None,
) -> ScoreStats:
    """`clock` is read before every gate and every call row, so the local day, pauses and
    timestamps follow real time during a long run."""
    stats = ScoreStats()
    if not registry.is_loaded():  # no profile: collect and triage go on, the LLM step stops
        stats.stop_code, stats.stopped = "no_profile", registry.not_loaded_message()
        return stats
    prompt = registry.current().score
    now = clock()
    alias = cfg.score_alias
    pv = prompt.version
    tz = cfg.timezone
    run_cap = max_calls if max_calls is not None else cfg.scoring.max_calls_per_run

    groups = store.kept_canonicals(rule_version, threshold)
    stats.kept_groups = len(groups)
    cutoff = now - timedelta(days=cfg.scoring.max_age_days)
    fresh = []
    for g in groups:
        if only_canonical is not None and g.canonical_id != only_canonical:
            continue
        job = store.job_for_llm(g.representative_id, rule_version)
        # a one-group re-score asked by the owner (#41) ignores the age window
        if only_canonical is not None or (job.posted_at or job.first_seen_at) >= cutoff:
            if only_canonical is not None and _reject_by_rules(store, g, job, rules_cfg, cfg, now):
                stats.scored += 1
                stats.reject += 1
                continue
            fresh.append((g, job))
    fresh.sort(key=lambda gj: gj[1].first_seen_at, reverse=True)
    if limit is not None:
        fresh = fresh[:limit]

    # the owner's corrections: a data block in front of the postings; not in the cache key
    block, rev = render_corrections(
        store.owner_corrections(pv, FEEDBACK_ITEMS, FEEDBACK_WITHDRAWN_MAX)
    )

    pending: list[_Item] = []
    for g, job in fresh:
        body = P.render_body(job, g, cfg.scoring.description)
        h = P.item_hash(body)
        cached = store.cache_get(h, alias, pv)
        if cached is not None:
            cached = _stub_rule(cached, g, job)
            with store.transaction():
                if store.insert_llm_verdict(
                    job_id=g.representative_id,
                    verdict=cached,
                    prompt_version=pv,
                    item_hash=h,
                    call_id=None,
                    model=None,
                    cached=True,
                    now=now,
                ):
                    _count(stats, cached)
            stats.cached += 1
            continue
        if not retry_errors and store.item_error_get(h, alias, pv):
            stats.skipped_errors += 1
            continue
        pending.append(_Item(g, body, h, job))
    stats.pending = len(pending)

    if dry_run:  # works during a pause: it never calls anything
        size = cfg.scoring.batch_size
        for n, i in enumerate(range(0, len(pending), size), 1):
            chunk = pending[i : i + size]
            chars = sum(len(it.body) + 30 for it in chunk) + len(prompt.system) + len(block)
            stats.lines.append(f"dry-run batch {n}: {len(chunk)} items, ~{chars // 4} tokens")
        return stats

    pause = None if ignore_pause else store.active_pause(now)
    if pause:
        stats.stop_code = "paused"
        stats.stopped = f"paused until {fmt_local(pause[0], tz)} {tz.split('/')[-1]} ({pause[1]})"
        return stats
    if not pending:
        return stats

    t = clock()
    gate = _gate(store, cfg, alias, local_day(t, tz), 0, run_cap, len(pending))
    if gate:
        stats.stop_code, stats.stopped = gate
        return stats

    pre = client.preflight()
    t = clock()
    # only real auth problems pause; a timeout / unparsable `auth status` just stops this run
    pre_status = pre.status if pre.ok or pre.status in PAUSE_STATUSES else C.ERROR
    until = (
        t + timedelta(minutes=cfg.limit_pause.auth_minutes)
        if pre_status in PAUSE_STATUSES
        else None
    )
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
        stats.stopped = f"preflight failed ({pre_status}: {pre.message})" + (
            f"; paused until {fmt_local(until, tz)}" if until else ""
        )
        return stats

    remaining = len(pending)
    for i in range(0, len(pending), cfg.scoring.batch_size):
        batch = pending[i : i + cfg.scoring.batch_size]
        if not _run_batch(
            store, client, cfg, alias, pv, clock, run_cap, batch, stats, remaining, block, rev
        ):
            break
        remaining -= len(batch)
    return stats


def _count(stats: ScoreStats, v: JobVerdict) -> None:
    stats.scored += 1
    if v.eligible:
        stats.keep += 1
    else:
        stats.reject += 1


def _gate(
    store: Store, cfg: LLMConfig, alias: str, day: str, run_calls: int, run_cap: int, left: int
) -> tuple[str, str] | None:
    """(reason code, text) of the first closed gate. Consumers branch on the code."""
    closed = daily_gate(store, cfg, alias, day)
    if closed:
        return closed
    if run_calls >= run_cap:
        return "run_cap", f"per-run cap {run_cap} reached; {left} groups left"
    return None


def _pause_for(store: Store, cfg: LLMConfig, status: str, at: datetime) -> datetime | None:
    return pause_for(store, cfg, status, at)


def _is_injection(v: JobVerdict) -> bool:
    return any("prompt_injection" in f.lower() for f in v.red_flags)


def _run_batch(
    store: Store,
    client: LLMClient,
    cfg: LLMConfig,
    alias: str,
    pv: str,
    clock: Callable[[], datetime],
    run_cap: int,
    batch: list[_Item],
    stats: ScoreStats,
    remaining: int,
    feedback_block: str = "",
    feedback_rev: str | None = None,
) -> bool:
    """Score one batch: at most one retry of the failed part. False = stop the whole run.

    Items that were never answered because a gate closed stay pending (no llm_error).
    """
    tz = cfg.timezone
    prompt = registry.current().score
    todo = list(batch)
    reasons: dict[str, str] = {}
    last_call: int | None = None
    last_status = C.ERROR
    attempts = 0
    for _ in range(len(batch) + 2):
        if not todo or attempts >= 2:
            break
        t = clock()
        day = local_day(t, tz)
        gate = _gate(store, cfg, alias, day, stats.calls, run_cap, remaining)
        if gate:
            stats.stop_code, stats.stopped = gate
            return False
        refs = [f"j{i + 1}" for i in range(len(todo))]
        stdin = "\n\n".join(P.render_job(r, it.body) for r, it in zip(refs, todo, strict=True))
        if feedback_block:
            stdin = feedback_block + "\n\n" + stdin
        # the row exists (and counts against the caps) before the call, so even a crash or
        # Ctrl-C during the call leaves it accounted for
        with store.transaction():
            call_id = store.insert_llm_call(
                kind="score",
                model_alias=alias,
                prompt_version=pv,
                n_items=len(todo),
                attempt=attempts + 1,
                now=t,
                local_day=day,
                status="started",
            )
        stats.calls += 1
        last_call = call_id
        try:
            res = client.run(
                LLMRequest(
                    model_alias=alias,
                    system_prompt=prompt.system,
                    json_schema=prompt.schema,
                    task=prompt.task,
                    stdin_text=stdin,
                    timeout_s=cfg.timeout_s,
                    max_budget_usd=cfg.max_budget_usd_per_call,
                )
            )
        except Exception as exc:  # noqa: BLE001 - any backend failure is an accounted error
            res = LLMResult(status=C.ERROR, error_message=repr(exc)[:300])
        stats.cost_usd += res.total_cost_usd or 0.0
        valid: dict[str, JobVerdict] = {}
        invalid: dict[str, str] = {}
        status = res.status
        if status == C.OK:
            valid, invalid = parse_batch(res.structured, refs)
            if not valid and invalid:
                status = C.INVALID_OUTPUT
            elif invalid:
                status = "partial"
        # a posting that talks the model into something taints the whole call: keep only its
        # own (forced ineligible) verdict and score the others again without it
        injectors = {r for r, v in valid.items() if _is_injection(v)}
        store_now = {r: v for r, v in valid.items() if not injectors or r in injectors}
        for r in injectors:
            store_now[r] = valid[r].model_copy(update={"eligible": False})
        t_end = clock()
        pause = _pause_for(store, cfg, status, t_end)
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
            for ref, it in zip(refs, todo, strict=True):
                v = store_now.get(ref)
                if v is None:
                    continue
                store.cache_put(it.hash, alias, pv, v, call_id, t_end)
                store.item_error_clear(it.hash, alias, pv)
                v = _stub_rule(v, it.group, it.job)
                if store.insert_llm_verdict(
                    job_id=it.group.representative_id,
                    verdict=v,
                    prompt_version=pv,
                    item_hash=it.hash,
                    call_id=call_id,
                    model=res.model,
                    cached=False,
                    now=t_end,
                    feedback_rev=feedback_rev,
                ):
                    _count(stats, v)
        stats.lines.append(_call_line(call_id, alias, len(todo), status, res))
        if pause is not None:
            stats.stop_code = "paused"
            stats.stopped = f"paused until {fmt_local(pause, tz)} ({status})"
            return False
        last_status = status
        if status in (C.OK, "partial"):
            stats.fail_streak = 0
        for ref, it in zip(refs, todo, strict=True):
            if ref not in store_now:
                reasons[it.hash] = invalid.get(ref) or (
                    "invalid_output" if status == C.INVALID_OUTPUT else status
                )
        todo = [it for r, it in zip(refs, todo, strict=True) if r not in store_now]
        if status == C.BUDGET:
            break
        if not injectors:  # re-scoring around an injector does not use up the retry
            attempts += 1
    if not todo:
        return True
    if len(todo) == len(batch) and last_status in (C.ERROR, C.TIMEOUT):
        stats.fail_streak += 1
        if stats.fail_streak >= CIRCUIT_BREAKER_BATCHES:
            # something systemic: leave these items pending instead of marking them llm_error
            stats.stop_code = "circuit"
            stats.stopped = f"circuit open: {stats.fail_streak} consecutive failed batches"
            return False
    else:
        stats.fail_streak = 0
    with store.transaction():
        for it in todo:
            store.item_error_put(
                it.group.representative_id,
                it.hash,
                alias,
                pv,
                last_call,
                reasons.get(it.hash, "error"),
                clock(),
            )
    stats.errors += len(todo)
    return True


def _call_line(call_id: int, alias: str, n: int, status: str, res: LLMResult) -> str:
    secs = f"{(res.duration_ms or 0) / 1000:.1f}s"
    cost = f"${res.total_cost_usd:.3f}" if res.total_cost_usd is not None else "$-"
    return f"call #{call_id} {alias} {n} items {status} {secs} {cost}"
