"""Command line: collect | sources | companies | jobs | triage | score | usage | alerts | ..."""

from __future__ import annotations

import argparse
import logging
import sys
import tarfile
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cvboost import connectors, plugins
from cvboost.collect import collect
from cvboost.config import Settings
from cvboost.core.rules import load_config, rule_version
from cvboost.envfile import load_dotenv
from cvboost.http import HttpClient
from cvboost.llm.client import make_client
from cvboost.llm.config import load_llm_config
from cvboost.llm.prompts import registry
from cvboost.locks import LockBusy, pipeline_lock
from cvboost.notify.redact import RedactingFilter
from cvboost.score import local_day, score
from cvboost.sources_view import fmt_dt as _fmt_dt
from cvboost.sources_view import source_rows
from cvboost.storage.db import Store, connect, migrate
from cvboost.triage import triage


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [
        max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)
    ]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True))]
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in rows]
    return "\n".join(lines)


def _open_store(settings: Settings) -> Store:
    conn = connect(settings.db_path)
    migrate(conn)
    store = Store(conn)
    store.sync_sources(connectors.REGISTRY, datetime.now(UTC))
    return store


def cmd_collect(store: Store, args: argparse.Namespace) -> int:
    selected = connectors.get(
        args.source,
        companies_path=getattr(args, "companies_path", None),
        rules_path=getattr(args, "rules_path", None),
        imap=getattr(args, "imap", None),
        forwarders=getattr(args, "forwarders", ()),
    )
    now = datetime.now(UTC)
    store.mark_stale_runs(timedelta(hours=1), now)
    http = HttpClient()
    try:
        results = collect(
            store,
            http,
            selected,
            limit=args.limit,
            force=args.force,
            now=now,
            named=frozenset(args.source or []),
        )
    finally:
        http.close()
    failed = 0
    for r in results:
        if r.status == "skipped":
            print(f"{r.source}: skipped ({r.skip_reason})")
        elif r.status == "ok":
            print(f"{r.source}: ok, found {r.found}, new {r.new}, invalid {r.invalid}")
        else:
            failed += 1
            print(f"{r.source}: ERROR {r.error_type}: {r.error_message}")
    return 1 if failed else 0


def cmd_companies_discover(settings: Settings, args: argparse.Namespace) -> int:
    """Fill the ATS company list. Reads the jobs DB read-only, writes only companies.yaml."""
    from cvboost import companies as co
    from cvboost.http import FetchError

    db_path = Path(args.db) if args.db else settings.db_path
    path = Path(args.companies) if args.companies else settings.companies_path
    if not db_path.exists():
        print(f"error: no database at {db_path}", file=sys.stderr)
        return 2
    existing, rejected = co.load_companies(path)
    for r in rejected:
        print(f"warning: rejected {path}: {r}", file=sys.stderr)
    found = co.discover_from_db(db_path)
    print(f"jobs DB: {len(found)} companies ({_per(co.per_ats(found))})")
    found, from_text, dropped = co.cap_description_finds(found, existing, args.max_description_new)
    if from_text:
        print(
            f"  {from_text} new from job descriptions (low confidence), "
            f"kept {from_text - dropped}, cap {args.max_description_new}"
        )
    if not args.no_fetch:
        http = HttpClient()
        try:
            remote = co.parse_remote_jobs_archive(co.fetch_remote_jobs_archive(http))
        except (FetchError, OSError, ValueError, EOFError, tarfile.TarError, zlib.error) as exc:
            print(f"error: remoteintech/remote-jobs: {exc}", file=sys.stderr)
            return 1
        finally:
            http.close()
        print(f"remoteintech/remote-jobs: {len(remote)} companies ({_per(co.per_ats(remote))})")
        found = co.merge(found, remote)
    merged = co.merge(existing, found)
    added = len(merged) - len(existing)
    print(f"{path}: {len(existing)} before, +{added} new, {len(merged)} total")
    for ats, n in co.per_ats(merged).items():
        print(f"  {ats}: {n}")
    if args.dry_run:
        print("dry run: nothing written")
    else:
        co.write_companies(path, merged)
    return 0


def cmd_companies_reactivate(store: Store, args: argparse.Namespace) -> int:
    from cvboost import ats_state
    from cvboost.companies import valid_slug

    if not valid_slug(args.slug):
        print("error: slug must match ^[a-z0-9-]{1,64}$", file=sys.stderr)
        return 2
    if not ats_state.reactivate(store, args.ats, args.slug):
        print(f"error: no such company: {args.ats} {args.slug}", file=sys.stderr)
        return 1
    print(f"{args.ats} {args.slug}: active, polled in the next cycle")
    return 0


def _per(counts: dict[str, int]) -> str:
    return ", ".join(f"{a} {n}" for a, n in counts.items())


def cmd_sources(store: Store, args: argparse.Namespace) -> int:
    rows = []
    for r in source_rows(
        store,
        datetime.now(UTC),
        frozenset() if getattr(args, "imap", None) else frozenset({"email_alerts"}),
    ):
        err = r.last_error
        rows.append(
            [
                r.name,
                r.kind,
                "yes" if r.enabled else "no",
                r.interval,
                r.last_run,
                r.last_success,
                str(r.fails),
                r.next_due,
                str(r.jobs),
                err if len(err) <= 80 else err[:77] + "...",
            ]
        )
    print(
        _table(
            [
                "name",
                "kind",
                "enabled",
                "interval",
                "last run (found/new)",
                "last success",
                "fails",
                "next due",
                "jobs",
                "last error",
            ],
            rows,
        )
    )
    from cvboost import ats_state

    for ats, c in sorted(ats_state.counts(store).items()):
        print(f"{ats} companies: {c['active']} active, {c['inactive']} inactive")
    return 0


def cmd_jobs(store: Store, args: argparse.Namespace) -> int:
    if args.kept:
        return _jobs_kept(store, args)
    if args.why is not None:
        return _jobs_why(store, args)
    rows = [
        [_fmt_dt(j.posted_at), j.source, j.company, j.title, j.url]
        for j in store.latest_jobs(args.latest)
    ]
    print(_table(["posted_at", "source", "company", "title", "url"], rows))
    return 0


def _jobs_kept(store: Store, args: argparse.Namespace) -> int:
    cfg = load_config(args.rules_path)
    version = rule_version(cfg)
    groups = store.kept_canonicals(version, cfg.relevance.threshold)
    n = store.conn.execute(
        "SELECT COUNT(*) FROM verdicts WHERE stage='rules' AND rule_version = ?", (version,)
    ).fetchone()[0]
    if n == 0:
        print(f"no verdicts for rule_version {version}; run cvboost triage")
        return 0
    rows = [
        [
            str(g.representative_id),
            str(g.relevance),
            g.profile or "-",
            g.rate_display + (" (rate unknown)" if "rate_unknown" in g.flags else ""),
            g.company,
            g.title,
            ",".join(g.sources),
            g.url,
        ]
        for g in groups
    ]
    print(_table(["id", "rel", "profile", "rate", "company", "title", "sources", "url"], rows))
    return 0


def _jobs_why(store: Store, args: argparse.Namespace) -> int:
    import json

    cfg = load_config(args.rules_path)
    version = rule_version(cfg)
    ex = store.explain(args.why, version)
    if ex is None:
        print(f"error: no job with id {args.why}", file=sys.stderr)
        return 2
    j = ex.job
    print(f"#{j['id']} [{j['source']}] {j['company']} - {j['title']}")
    print(f"url: {j['url']}")
    print(
        f"canonical: {j['canonical_id']}; members: "
        + ", ".join(f"#{m['id']} ({m['source']}) {m['url']}" for m in ex.members)
    )
    v = ex.verdicts.get(j["id"])
    if v is None:
        print("no verdict yet - run cvboost triage")
        return 0
    details = json.loads(v["details_json"])
    reasons = json.loads(v["reasons_json"])
    print(f"rule_version: {v['rule_version']}  at: {v['at']}")
    print(f"decision: {v['decision']}")
    for r in reasons:
        print(f"  reason {r}: {details.get('evidence', {}).get(r, '')}")
    flags = json.loads(v["flags_json"])
    if flags:
        print("flags: " + ", ".join(flags))
    rate = details.get("rate")
    print("rate: " + (json.dumps(rate) if rate else "unknown"))
    print(f"relevance: {v['relevance']} (profile {v['profile']})")
    for name, p in details.get("relevance", {}).items():
        print(
            f"  {name}: {p['score']} title={p['title']} tags={p['tags']} "
            f"description={p['description']}"
        )
    for m in ex.members:
        if m["id"] != j["id"]:
            mv = ex.verdicts.get(m["id"])
            print(
                f"member #{m['id']}: "
                + (f"{mv['decision']} {json.loads(mv['reasons_json'])}" if mv else "no verdict")
            )
    llm = store.latest_llm_verdict(j["id"])
    if llm is None:
        print("llm: no verdict yet - run cvboost score")
    else:
        lv, ld, pv = llm
        print(f"llm: eligible={lv.eligible} fit={lv.fit} profile={lv.profile} (prompt {pv})")
        print(f"  model: {ld.get('model') or '-'}  cached: {ld.get('cached')}")
        print(f"  reason: {lv.eligibility_reason}")
        print(f"  seniority: {lv.seniority}  employment: {lv.employment}  rate: {lv.rate_text}")
        print("  red_flags: " + (", ".join(lv.red_flags) or "-"))
        print(f"  summary: {lv.summary_ru}")
    return 0


def cmd_triage(store: Store, args: argparse.Namespace) -> int:
    cfg = load_config(args.rules_path)
    st = triage(store, cfg, datetime.now(UTC))
    print(f"rule_version: {st.rule_version}")
    print(f"groups: {st.groups} (merged {st.merged})")
    print(f"evaluated {st.evaluated} (new {st.new}, changed {st.changed})")
    print(f"keep {st.keep} / reject {st.reject}; kept groups {st.kept_groups}")
    for reason, n in st.main_reasons.most_common():
        print(f"  {reason}: {n}")
    for flag, n in sorted(st.flag_counts.items()):
        print(f"flag {flag}: {n}")
    return 0


def cmd_score(store: Store, args: argparse.Namespace) -> int:
    cfg = load_llm_config(args.llm_config_path)
    rules = load_config(args.rules_path)
    version = rule_version(rules)
    client = None if args.dry_run else make_client(cfg)
    st = score(
        store,
        client,  # type: ignore[arg-type]  # dry-run never touches the client
        cfg,
        version,
        rules.relevance.threshold,
        lambda: datetime.now(UTC),
        limit=args.limit,
        max_calls=args.max_calls,
        retry_errors=args.retry_errors,
        ignore_pause=args.ignore_pause,
        dry_run=args.dry_run,
    )
    print(f"kept groups {st.kept_groups}, pending {st.pending}, cached {st.cached}")
    if st.skipped_errors:
        print(f"skipped {st.skipped_errors} groups with earlier llm_error (use --retry-errors)")
    for line in st.lines:
        print(line)
    if st.calls or st.scored:
        print(
            f"scored {st.scored} (keep {st.keep} / reject {st.reject}), llm_error {st.errors}, "
            f"cost ${st.cost_usd:.3f}"
        )
    if st.stopped:
        print(f"stopped: {st.stopped}")
    return 1 if st.errors or st.stop_code == "no_profile" else 0


def cmd_email(store: Store, args: argparse.Namespace) -> int:
    if args.email_command == "status":
        rows = store.conn.execute(
            "SELECT status, COUNT(*) n, SUM(jobs_found) j FROM email_messages GROUP BY status"
        ).fetchall()
        for r in rows:
            print(f"{r['status']:<11} messages {r['n']:>4}  jobs {r['j'] or 0}")
        if not rows:
            print("no e-mail messages recorded yet")
        return 0
    from cvboost import email_extract

    cfg = load_llm_config(args.llm_config_path)
    if args.dry_run:
        pending = store.email_pending(1000)
        print(f"needs_llm: {len(pending)} messages, batch {cfg.email_extract.batch_size}")
        return 0
    st = email_extract.run(
        store,
        make_client(cfg),
        cfg,
        lambda: datetime.now(UTC),
        max_calls=args.max_calls,
        ignore_pause=args.ignore_pause,
    )
    print(
        f"pending {st.pending}, calls {st.calls}, new jobs {st.new_jobs}, not a job {st.not_job}, "
        f"errors {st.errors}, expired {st.expired}, cost ${st.cost_usd:.3f}"
    )
    if st.stopped:
        print(f"stopped: {st.stopped}")
    return 1 if st.errors else 0


def cmd_usage(store: Store, args: argparse.Namespace) -> int:
    cfg = load_llm_config(args.llm_config_path)
    now = datetime.now(UTC)
    today = local_day(now, cfg.timezone)
    since = local_day(now - timedelta(days=max(args.days - 1, 0)), cfg.timezone)
    rows = [
        [
            r.day,
            r.model_alias,
            str(r.calls),
            str(r.ok),
            str(r.errors),
            str(r.input_tokens),
            str(r.output_tokens),
            f"{r.cost_usd:.3f}",
            str(cfg.daily_call_caps.get(r.model_alias, 0)) if r.day == today else "-",
        ]
        for r in store.usage_summary(since)
    ]
    print(
        _table(
            ["day", "model", "calls", "ok", "err", "tokens_in", "tokens_out", "cost_usd", "cap"],
            rows,
        )
    )
    pause = store.active_pause(now)
    print("pause: " + (f"until {_fmt_dt(pause[0])} UTC ({pause[1]})" if pause else "none"))
    method, expires = store.last_auth_info()
    print(f"auth: {method or '-'}, credential expires {expires or '-'}")
    print(f"prompt_version: {registry.score_version()}")
    return 0


def cmd_alerts(store: Store, args: argparse.Namespace) -> int:
    rows = [
        [
            str(a["id"]),
            a["status"],
            str(a["fit"]),
            str(a["attempts"]),
            (a["sent_at"] or a["created_at"])[:16],
            f"{a['company']} - {a['title']}"[:70],
            (a["last_error"] or "-")[:60],
        ]
        for a in store.latest_alerts(args.latest)
    ]
    print(_table(["id", "status", "fit", "tries", "at", "job", "last error"], rows))
    print()
    rows = [
        [
            str(d["id"]),
            d["action"],
            d["reason"] or "-",
            d["channel"],
            d["at"][:16],
            f"{d['company']} - {d['title']}"[:70],
        ]
        for d in store.recent_decisions(args.latest)
    ]
    print(_table(["id", "action", "reason", "channel", "at", "job"], rows))
    return 0


def cmd_cv_check_profile(settings: Settings, _args: argparse.Namespace) -> int:
    from cvboost.cv.profile import check_gaps, load_gaps, load_profile

    profile = load_profile(settings.resume_dir)
    gaps = load_gaps(settings.resume_dir)
    problems = check_gaps(profile, gaps)
    print(f"profile sha {profile.sha[:12]}, {len(profile.sections)} claimable sections")
    print(f"known gaps {len(profile.known_gaps)}, gaps.yaml entries {len(gaps.gaps)}")
    for item in profile.known_gaps:
        ids = [g.id for g in gaps.gaps if g.source == item]
        print(f"  {item}: {', '.join(ids) or 'NOT COVERED'}")
    for problem in problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    return 1 if problems else 0


def cmd_profile_check(settings: Settings, _args: argparse.Namespace) -> int:
    """Everything the LLM steps need about the person, in one report (#58)."""
    import hashlib

    from cvboost.llm.prompts.registry import (
        BLOCK_NAMES,
        RESCORE_NOTE,
        block_warnings,
        build,
        language_warnings,
        read_blocks,
    )
    from cvboost.profile import load_user_profile, prompts_dir_of

    problems: list[str] = []
    try:
        prof = load_user_profile(settings.profile_path)
    except ValueError as exc:
        print(f"PROBLEM: {exc}", file=sys.stderr)
        return 1
    print(f"profile {settings.profile_path}: {prof.name} ({prof.first_name}), {prof.pronouns}, "
          f"{prof.timezone}")  # fmt: skip
    print(f"languages: ui {prof.ui_language}, explanations {prof.explain_language}, "
          f"documents {prof.documents_language}")  # fmt: skip
    directory = prompts_dir_of(prof, settings.profile_path)
    blocks, bad = read_blocks(directory)
    problems += bad
    print(f"prompt blocks in {directory}: {len(blocks)}/{len(BLOCK_NAMES)}")
    for name, text in blocks.items():
        print(f"  {name}: {len(text)} chars")
    try:
        rules = load_config(settings.rules_path)
    except ValueError as exc:
        problems.append(str(exc))
        rules = None
    for warning in block_warnings(blocks):
        print(f"WARNING: {warning}", file=sys.stderr)
    for warning in language_warnings(prof, blocks):
        print(f"WARNING: {warning}", file=sys.stderr)
    if rules is not None and not rules.relevance.profiles:
        problems.append(f"{settings.rules_path}: relevance.profiles is empty")
    if rules is not None and not bad:
        ps = build(prof, blocks, tuple(rules.relevance.profiles))
        print(f"relevance profiles (rules.yaml): {', '.join(ps.profiles) or '-'}")
        print(f"  note: {RESCORE_NOTE}")
        for kind, version in ps.versions().items():
            print(f"  {kind}: {version}")
    skill = settings.resume_dir / "SKILL.md"
    if prof.resume_skill_sha and skill.exists():
        have = hashlib.sha256(skill.read_bytes()).hexdigest()
        if have != prof.resume_skill_sha:
            problems.append(
                f"{skill} changed since the CV rules were written (sha256 {have}): re-read "
                "config/prompts/cv_*.md, then set resume_skill_sha in the profile"
            )
    try:
        from cvboost.cv.profile import check_gaps, linkedin_slugs, load_gaps, load_profile

        facts = load_profile(settings.resume_dir)
        gaps = load_gaps(settings.resume_dir)
        problems += check_gaps(facts, gaps)
        if not linkedin_slugs(facts.text):
            print("WARNING: no linkedin.com/in/<slug> link in the Contacts section of profile.md: "
                  "the CRM cannot tell your own profile link from the sender's",
                  file=sys.stderr)  # fmt: skip
        print(f"profile.md: sha {facts.sha[:12]}, {len(facts.sections)} sections, "
              f"{len(facts.known_gaps)} known gaps, {len(gaps.gaps)} in gaps.yaml")  # fmt: skip
    except ValueError as exc:
        problems.append(f"resume facts: {exc}")
    for problem in problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_telegram_chat_id(settings: Settings, _args: argparse.Namespace) -> int:
    """Read the bot's pending messages once and print who wrote to it (chat ids, no token)."""
    from cvboost.notify.telegram import TelegramClient, TelegramError

    if not settings.telegram_token:
        print("error: TELEGRAM_BOT_TOKEN is not set (put it in .env)", file=sys.stderr)
        return 2
    http = HttpClient()
    try:
        chats, ignored = TelegramClient(settings.telegram_token, http).recent_chats()
    except TelegramError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.status == 409:
            print(
                "hint: another program is reading this bot's messages (a running worker?). "
                "Stop it (scripts/local.sh down) and run this again.",
                file=sys.stderr,
            )
        return 1
    finally:
        http.close()
    if ignored:
        print(f"(ignored {ignored} group or channel chat(s): only private chats are listed)")
    if not chats:
        print(
            "No private chat found. Open your bot in Telegram, press Start (or send /start "
            "again; the worker may already have read the old message), then run this again."
        )
        return 1
    for c in chats:
        print(f"chat_id {int(c['id'])}  ({c['type']})")
    print("Put the id of YOUR private chat into .env as TELEGRAM_CHAT_ID=<id>.")
    return 0


def cmd_cv_render(settings: Settings, args: argparse.Namespace) -> int:
    from cvboost.cv.render import RenderError, RendererUnavailable, WeasyRenderer

    try:
        md = Path(args.markdown).read_text(encoding="utf-8")
        rendered = WeasyRenderer(settings.resume_dir).render(md)
    except OSError as exc:
        raise ValueError(f"cannot read {args.markdown}: {exc.strerror}") from None
    except RendererUnavailable as exc:
        raise ValueError(f"PDF renderer unavailable (pango missing?): {exc}") from None
    except RenderError as exc:
        raise ValueError(f"render failed: {exc}") from None
    Path(args.pdf).write_bytes(rendered.pdf)
    print(f"wrote {args.pdf}: {rendered.pages} page(s), {len(rendered.pdf)} bytes")
    return 0


def cmd_cv_list(store: Store, args: argparse.Namespace) -> int:
    rows = []
    for r in store.recent_cv_requests(args.latest):
        rows.append(
            [
                str(r["id"]),
                r["kind"],
                r["status"],
                str(r["attempts"]),
                r["pdf_status"] or "-",
                (r["finished_at"] or r["requested_at"])[:16],
                f"{r['company']} - {r['title']}"[:60],
                (r["review_reasons"] or r["error"] or "-")[:60],
            ]
        )
    print(_table(["id", "kind", "status", "tries", "pdf", "at", "job", "reasons / error"], rows))
    return 0


def cmd_cv_backfill_notes(store: Store, args: argparse.Namespace) -> int:
    from cvboost.cv.backfill import backfill_notes

    filled, skipped = backfill_notes(store, args.settings.kits_dir)
    print(f"filled {filled}, left without verdict {skipped}")
    return 0


def cmd_cv_run(store: Store, args: argparse.Namespace) -> int:
    from cvboost.cv.render import WeasyRenderer
    from cvboost.cv.step import load_resume_inputs, run_cv_step
    from cvboost.llm.prompts import cv_v1 as P

    settings = args.settings
    cfg = load_llm_config(args.llm_config_path)
    if args.dry_run:  # never calls claude, never claims a request
        cvp = registry.current().cv
        profile, _gaps = load_resume_inputs(settings)
        sections = profile.section_names
        print(f"prompt_version: {cvp.version}, profile sha {profile.sha[:12]}")
        print(f"system prompt: {len(cvp.build_system_prompt(profile.prompt_text, sections))} chars")
        row = store.cv_request(args.request) if args.request else None
        if row is None:
            queued = [r for r in store.recent_cv_requests(50) if r["status"] == "queued"]
            row = queued[-1] if queued else None
        if row is None:
            print("no queued request")
            return 0
        job = store.job_for_llm(row["job_id"], None)
        stdin = P.render_job(
            job, cfg.cv.description, row["feedback"], store.owner_override_note(row["job_id"])
        )
        print(f"request {row['id']} ({row['status']}): job input {len(stdin)} chars")
        return 0
    st = run_cv_step(
        store,
        make_client(cfg),
        cfg,
        settings,
        WeasyRenderer(settings.resume_dir),
        lambda: datetime.now(UTC),
        request_id=args.request,
        ignore_pause=args.ignore_pause,
    )
    for line in st.lines:
        print(line)
    print(
        f"claimed {st.claimed}, calls {st.calls}: done {st.done}, needs_review {st.needs_review}, "
        f"failed {st.failed}, requeued {st.requeued}, cost ${st.cost_usd:.3f}"
    )
    if st.stopped:
        print(f"stopped: {st.stopped}")
    return 1 if st.failed or st.stop_code == "config" else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cvboost")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect", help="fetch jobs from sources")
    c.add_argument("--source", action="append", metavar="NAME", help="repeatable; default all")
    c.add_argument("--limit", type=int, help="max postings per source")
    c.add_argument(
        "--force",
        action="store_true",
        help="ignore schedule and backoff (disabled sources still need --source NAME)",
    )
    so = sub.add_parser("sources", help="source health")
    so.add_argument(
        "--markdown",
        action="store_true",
        help="print SOURCES.md (connector facts); touches no database or .env",
    )
    from cvboost.companies import ATS_NAMES

    co_p = sub.add_parser("companies", help="ATS company list (greenhouse, lever, ashby, ...)")
    co_sub = co_p.add_subparsers(dest="companies_command", required=True)
    cd = co_sub.add_parser(
        "discover",
        help="add companies from apply URLs in the DB and from remoteintech/remote-jobs",
    )
    cd.add_argument("--db", metavar="PATH", help="jobs DB to read (read-only); default CVBOOST_DB")
    cd.add_argument("--companies", metavar="PATH", help="companies.yaml to update")
    ra = co_sub.add_parser("reactivate", help="poll an inactive company again")
    ra.add_argument("ats", choices=list(ATS_NAMES))
    ra.add_argument("slug")
    cd.add_argument(
        "--max-description-new",
        type=int,
        default=20,
        metavar="N",
        help="take at most N NEW companies found only in job descriptions (default 20)",
    )
    cd.add_argument("--no-fetch", action="store_true", help="offline: skip the GitHub download")
    cd.add_argument("--dry-run", action="store_true", help="print the result, write nothing")
    j = sub.add_parser("jobs", help="latest jobs")
    g = j.add_mutually_exclusive_group()
    g.add_argument("--latest", type=int, default=20, metavar="N")
    g.add_argument("--kept", action="store_true", help="kept groups, one row per canonical job")
    g.add_argument("--why", type=int, metavar="ID", help="explain the verdict for a job id")
    sub.add_parser("triage", help="dedupe and run hard rules on new jobs")
    sc = sub.add_parser("score", help="LLM verdicts for kept groups (claude -p, subscription)")
    sc.add_argument("--limit", type=int, metavar="N", help="consider at most N newest groups")
    sc.add_argument("--max-calls", type=int, metavar="N", help="override per-run call cap")
    sc.add_argument("--retry-errors", action="store_true", help="re-send groups with llm_error")
    sc.add_argument("--ignore-pause", action="store_true", help="ignore an active LLM pause")
    sc.add_argument("--dry-run", action="store_true", help="show batches, never call claude")
    em = sub.add_parser("email", help="alerts mailbox: extract (LLM fallback) | status")
    emsub = em.add_subparsers(dest="email_command", required=True)
    ex = emsub.add_parser("extract", help="LLM fallback for unreadable e-mails (claude -p)")
    ex.add_argument("--max-calls", type=int, metavar="N", help="override the per-cycle cap")
    ex.add_argument("--ignore-pause", action="store_true", help="ignore an active LLM pause")
    ex.add_argument("--dry-run", action="store_true", help="count pending, never call claude")
    emsub.add_parser("status", help="recorded e-mail messages by status")
    u = sub.add_parser("usage", help="LLM calls per day and model vs caps")
    u.add_argument("--days", type=int, default=7)
    al = sub.add_parser("alerts", help="latest Telegram alerts and owner decisions")
    al.add_argument("--latest", type=int, default=20, metavar="N")
    pf = sub.add_parser("profile", help="user profile and prompt blocks")
    pf.add_subparsers(dest="profile_command", required=True).add_parser(
        "check", help="profile.yaml, prompt blocks, profile.md vs gaps.yaml, prompt versions"
    )
    sub.add_parser("init", help="create config/ from config.example/ (public version)")
    sub.add_parser(
        "doctor", help="check the installation: claude login, .env, DB, profile, sources"
    )
    tg = sub.add_parser("telegram", help="Telegram helpers")
    tg.add_subparsers(dest="telegram_command", required=True).add_parser(
        "chat-id", help="print the chat ids that wrote to your bot (send it /start first)"
    )
    cv = sub.add_parser(
        "cv", help="CV drafting: check-profile | run | render | list | backfill-notes"
    )
    cvsub = cv.add_subparsers(dest="cv_command", required=True)
    cvsub.add_parser("check-profile", help="profile.md and gaps.yaml are in sync")
    cr = cvsub.add_parser("run", help="process one queued CV request (one Opus call)")
    cr.add_argument("--request", type=int, metavar="ID", help="this request instead of the oldest")
    cr.add_argument("--dry-run", action="store_true", help="prompt sizes only, never call claude")
    cr.add_argument("--ignore-pause", action="store_true", help="ignore an active LLM pause")
    rn = cvsub.add_parser("render", help="render a resume markdown file to PDF")
    rn.add_argument("markdown")
    rn.add_argument("pdf")
    cvsub.add_parser(
        "backfill-notes", help="fill the Opus verdict of old kits from their fit_notes.md"
    )
    cl = cvsub.add_parser("list", help="recent CV requests")
    cl.add_argument("--latest", type=int, default=20, metavar="N")
    for plugin in plugins.load_plugins():  # optional plugin commands, see cvboost.plugins
        if plugin.register_cli:
            plugin.register_cli(sub)
    w = sub.add_parser("worker", help="run the 10-minute collect/score/alert loop")
    w.add_argument("--once", action="store_true", help="run one cycle and exit (no bot thread)")
    w.add_argument("--health", action="store_true", help="exit 0 if the last cycle is recent")
    wb = sub.add_parser("web", help="web panel (FastAPI), 127.0.0.1:8080 by default")
    wb.add_argument("--host", default="127.0.0.1")
    wb.add_argument("--port", type=int, default=8080)
    return p


LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def cmd_web(settings: Settings, args: argparse.Namespace) -> int:
    has_auth = settings.web_user is not None and settings.web_password is not None
    if args.host not in LOOPBACK and not has_auth and not settings.web_trust_network:
        print(
            f"error: refusing to listen on {args.host} without basic auth. Set "
            "CVBOOST_WEB_USER and CVBOOST_WEB_PASSWORD, or CVBOOST_WEB_TRUST_NETWORK=1 when the "
            "network itself is the gate (Tailscale, docker port bound to 127.0.0.1).",
            file=sys.stderr,
        )
        return 2
    import uvicorn

    from cvboost.web.app import create_app

    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        workers=1,
        proxy_headers=False,
        server_header=False,
    )
    return 0


# steps that write jobs/verdicts/LLM state: never concurrent with a worker cycle
LOCKED_COMMANDS = {"collect", "triage", "score", "email"}


def _setup_logging(level: str, token: str | None = None, *secrets: str | None) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    for handler in logging.getLogger().handlers:  # last line of defence for the bot token
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter(token, *secrets))
    # httpx logs full request URLs at INFO; the Telegram bot token is part of that URL
    for noisy in ("httpx", "httpcore", "weasyprint", "fontTools"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "sources" and args.markdown:
        from cvboost.sources_doc import render_markdown

        try:
            print(render_markdown(), end="")
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.command == "init":
        from cvboost.init_config import run_init

        code, lines = run_init()
        print("\n".join(lines), file=sys.stderr if code else sys.stdout)
        return code
    load_dotenv()  # ./.env fills in what the real environment lacks (one Python parser)
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1 if args.command == "doctor" else 2
    args.rules_path = settings.rules_path
    args.llm_config_path = settings.llm_config_path
    args.companies_path = settings.companies_path
    args.imap = settings.imap_creds()
    args.forwarders = settings.mail_forwarders
    _setup_logging(settings.log_level, settings.telegram_token, settings.imap_password)
    registry.load(settings)  # no profile is not an error here: LLM steps say so when they run
    try:
        if args.command == "worker":
            from cvboost.worker import run_worker, worker_healthy

            if args.health:
                return 0 if worker_healthy(settings) else 1
            return run_worker(settings, once=args.once, install_signals=not args.once)
        if args.command == "profile":
            return cmd_profile_check(settings, args)
        if args.command == "doctor":
            from cvboost.doctor import cmd_doctor

            return cmd_doctor(settings, args)
        if args.command == "telegram":
            return cmd_telegram_chat_id(settings, args)
        if args.command == "web":
            return cmd_web(settings, args)
        for plugin in plugins.load_plugins():
            if plugin.dispatch_cli:
                rc = plugin.dispatch_cli(settings, args)
                if rc is not None:
                    return rc
        if args.command == "companies" and args.companies_command == "discover":
            return cmd_companies_discover(settings, args)
        if args.command == "cv" and args.cv_command in ("check-profile", "render"):
            fn = cmd_cv_check_profile if args.cv_command == "check-profile" else cmd_cv_render
            return fn(settings, args)
        store = _open_store(settings)
        if args.command == "cv":
            args.settings = settings
            if args.cv_command == "list":
                return cmd_cv_list(store, args)
            if args.cv_command == "backfill-notes":
                return cmd_cv_backfill_notes(store, args)
            if args.dry_run:
                return cmd_cv_run(store, args)
            try:
                with pipeline_lock(settings.data_dir, wait_s=0):
                    return cmd_cv_run(store, args)
            except LockBusy as exc:
                print(
                    f"pipeline busy: a worker cycle is running ({exc.holder}). Try again later.",
                    file=sys.stderr,
                )
                return 3
        handler = {
            "collect": cmd_collect,
            "sources": cmd_sources,
            "jobs": cmd_jobs,
            "triage": cmd_triage,
            "score": cmd_score,
            "email": cmd_email,
            "usage": cmd_usage,
            "alerts": cmd_alerts,
            "companies": cmd_companies_reactivate,
        }[args.command]
        if args.command in LOCKED_COMMANDS and not (
            getattr(args, "dry_run", False) or getattr(args, "email_command", "") == "status"
        ):
            try:
                with pipeline_lock(settings.data_dir, wait_s=0):
                    return handler(store, args)
            except LockBusy as exc:
                print(
                    f"pipeline busy: a worker cycle is running ({exc.holder}). Try again later.",
                    file=sys.stderr,
                )
                return 3
        return handler(store, args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
