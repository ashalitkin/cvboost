"""SQLite storage: connection, forward-only migrations, the Store."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from cvboost import plugins
from cvboost.llm.schema import JobVerdict
from cvboost.models import Job

if TYPE_CHECKING:
    from cvboost.connectors.base import Connector

MIGRATIONS_PACKAGE = "cvboost.storage.migrations"


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def connect(path: Path | str, busy_ms: int = 5000) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: autocommit; transactions are explicit (BEGIN/COMMIT)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
    return conn


def _migration_files() -> list[tuple[int, str]]:
    """Core migrations plus those of the loaded plugins, by number. A number used twice is an
    error: each plugin owns a reserved range (e.g. 20-27 and 9000-9999)."""
    found: dict[int, tuple[str, str]] = {}
    for package in (MIGRATIONS_PACKAGE, *plugins.migration_packages()):
        for entry in resources.files(package).iterdir():
            name = entry.name
            if name.endswith(".sql") and name[:4].isdigit():
                version = int(name[:4])
                if version in found:
                    raise ValueError(
                        f"migration number {version:04d} is used twice: "
                        f"{found[version][0]} and {package}"
                    )
                found[version] = (package, entry.read_text(encoding="utf-8"))
    return [(v, found[v][1]) for v in sorted(found)]


FK_OFF_MARKER = "-- cvboost: foreign_keys=off"


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every migration whose version is not recorded yet, in order.

    Versions are compared as a set, not against the maximum: a migration number that was
    reserved and merged later (e.g. 0005 after 0006) is still applied.

    A script whose first line is `-- cvboost: foreign_keys=off` rebuilds tables (SQLite's
    12-step procedure): foreign keys are switched off before BEGIN, `foreign_key_check` must be
    empty before COMMIT (else ROLLBACK and an error), and they are switched back on in `finally`.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    for version, sql in _migration_files():
        if version in applied:
            continue
        record = (
            "\nINSERT INTO schema_migrations (version, applied_at) "
            f"VALUES ({version}, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));\n"
        )
        if sql.lstrip().startswith(FK_OFF_MARKER):
            _apply_fk_off(conn, version, sql, record)
        else:
            # executescript commits first, so wrap the script and the version row in one txn
            conn.executescript("BEGIN;\n" + sql + record + "COMMIT;")
        applied.add(version)
    return max(applied, default=0)


def _apply_fk_off(conn: sqlite3.Connection, version: int, sql: str, record: str) -> None:
    conn.execute("PRAGMA foreign_keys=OFF")  # a no-op inside a transaction, so before BEGIN
    try:
        try:
            conn.executescript("BEGIN;\n" + sql + record)
            bad = conn.execute("PRAGMA foreign_key_check").fetchall()
            if bad:
                raise sqlite3.IntegrityError(
                    f"migration {version:04d}: foreign_key_check found {len(bad)} violations"
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


@dataclass(frozen=True)
class SourceState:
    enabled: bool
    interval_override_s: int | None
    last_started_at: datetime | None
    consecutive_failures: int


@dataclass(frozen=True)
class SourceHealth:
    name: str
    enabled: bool
    interval_override_s: int | None
    last_started_at: datetime | None
    last_status: str | None
    last_found: int | None
    last_new: int | None
    last_success_at: datetime | None
    consecutive_failures: int
    last_error: str | None
    jobs_total: int


@dataclass(frozen=True)
class JobRow:
    id: int
    source: str
    company: str
    title: str
    url: str
    posted_at: datetime | None
    first_seen_at: datetime


@dataclass(frozen=True)
class KeptGroup:
    canonical_id: int
    representative_id: int
    relevance: int
    profile: str | None
    flags: list[str]
    member_ids: list[int]
    sources: list[str]
    company: str
    title: str
    url: str
    rate_display: str
    posted_at: datetime | None


@dataclass(frozen=True)
class LLMJobInput:
    job_id: int
    title: str
    company: str
    location_text: str | None
    employment_type: str | None
    tags: list[str]
    description_text: str
    first_seen_at: datetime
    posted_at: datetime | None
    rule_flags: list[str]
    rule_evidence: dict[str, str]
    # the owner's pasted text (#41): always for CV prompts, for the Haiku body only if scored
    extra_description: str | None = None
    extra_scored: bool = False


@dataclass(frozen=True)
class UsageRow:
    day: str
    model_alias: str
    calls: int
    ok: int
    errors: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass(frozen=True)
class ScoredGroup:
    canonical_id: int
    job_id: int
    verdict: JobVerdict
    decision: str
    at: datetime
    input_hash: str
    model: str | None
    cached: bool


DECISION_NOTE_MAX = 500  # decisions.note (migration 0010)

APPLICATION_STATUSES = (
    "preparing",
    "ready",
    "applied",
    "replied",
    "interview",
    "rejected",
    "offer",
)
WITHDRAWN = "withdrawn"  # "Withdrawn": set only by `Store.withdraw_application`


@dataclass(frozen=True)
class ApplicationRow:
    canonical_id: int
    title: str
    company: str
    status: str  # effective: the owner's later-stage status, else derived from the CV request
    derived: bool  # True when the status comes from cv_requests, not from the owner
    manual_status: str | None  # what the `applications` row says (may be overridden by the CV)
    note: str | None
    updated_at: datetime | None
    cv_status: str | None
    cv_requested_at: datetime | None
    cv_finished_at: datetime | None
    cv_error: str | None
    applied_at: datetime | None
    withdraw_note: str | None = None  # "reason: text" of the newest "Withdrawn"


@dataclass(frozen=True)
class Explanation:
    job: sqlite3.Row
    members: list[sqlite3.Row]
    verdicts: dict[int, sqlite3.Row | None]


def group_reasons(members) -> set[str]:
    """The reject codes of one dedupe group from its members' (codes, flags) pairs.

    Normally the union. `latam_only` that came only from a sighting's location text is dropped
    when another sighting names a place that is not Latin American (Portugal, EU, worldwide):
    the posting is then not a LatAm-only hire (issue 67). An explicit "LATAM only" in the
    description always stands."""
    codes: set[str] = set()
    other_place = False
    explicit = False
    for c, f in members:
        codes |= set(c)
        other_place = other_place or "loc_not_latam" in f
        explicit = explicit or ("latam_only" in c and "latam_loc" not in f)
    if "latam_only" in codes and other_place and not explicit:
        codes.discard("latam_only")
    return codes


def rule_outcome(codes, best_relevance: int | None, threshold: float) -> list[str]:
    """The reasons that reject a group, [] = kept. The single rule for Inbox and explanations:
    any reason but low_relevance rejects; low_relevance counts only when even the group's best
    member is under the threshold."""
    from cvboost.core.rules import REASON_ORDER

    hard = {c for c in codes if c != "low_relevance"}
    shown = [c for c in REASON_ORDER if c in hard] + sorted(hard - set(REASON_ORDER))
    if best_relevance is not None and best_relevance < threshold:
        shown.append("low_relevance")
    return shown


def rate_display(rows) -> str:
    """The first member's stated rate as text, '?' if none states one (kept groups and Inbox)."""
    for m in rows:
        if m["salary_text"] or m["salary_max"] or m["salary_min"]:
            return m["salary_text"] or (
                f"{m['salary_min'] or 0:g}-{m['salary_max'] or 0:g} "
                f"{m['salary_currency'] or ''}/{m['salary_period'] or ''}"
            )
    return "?"


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


CORE_LLM_KINDS = frozenset({"score", "preflight", "cv", "email"})  # plugins add theirs
CORE_PAUSE_KINDS = ("score", "cv")  # kinds that can hit the usage limit (plugins add theirs)


def _pause_kinds_sql() -> str:
    return ",".join(f"'{k}'" for k in (*CORE_PAUSE_KINDS, *plugins.pause_kinds()))


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def sync_sources(self, registry: Mapping[str, type[Connector]], now: datetime) -> None:
        for name, cls in registry.items():
            for source in (name, *cls.job_sources):  # e-mail connectors own several job sources
                self.conn.execute(
                    "INSERT OR IGNORE INTO sources (name, enabled, created_at) VALUES (?, ?, ?)",
                    (source, int(cls.default_enabled), iso(now)),
                )

    # --- alerts mailbox (#17) ---------------------------------------------------------------

    def email_cursor(self, mailbox: str, uidvalidity: int) -> int:
        """Highest UID already recorded for this mailbox generation (0 = none)."""
        row = self.conn.execute(
            "SELECT COALESCE(MAX(uid), 0) FROM email_messages "
            "WHERE mailbox = ? AND uidvalidity = ?",
            (mailbox, uidvalidity),
        ).fetchone()
        return row[0]

    def known_msgid_hashes(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT DISTINCT msgid_hash FROM email_messages")}

    def record_email_messages(self, rows: list[dict], run_id: int | None, now: datetime) -> None:
        """One row per message part; INSERT OR IGNORE so a re-read never duplicates."""
        with self.transaction():
            for r in rows:
                self.conn.execute(
                    "INSERT OR IGNORE INTO email_messages (mailbox, uidvalidity, uid, part, "
                    "msgid_hash, message_id, sender_domain, platform, run_id, received_at, "
                    "first_seen_at, status, jobs_found, llm_input, links_json, note) "
                    "VALUES (:mailbox, :uidvalidity, :uid, :part, :msgid_hash, :message_id, "
                    ":sender_domain, :platform, :run_id, :received_at, :first_seen_at, :status, "
                    ":jobs_found, :llm_input, :links_json, :note)",
                    {**r, "run_id": run_id, "first_seen_at": iso(now)},
                )

    def email_pending(self, limit: int) -> list[sqlite3.Row]:
        """Messages waiting for the LLM fallback, oldest first."""
        return self.conn.execute(
            "SELECT * FROM email_messages WHERE status = 'needs_llm' AND llm_input IS NOT NULL "
            "ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()

    def calls_today_kind(self, kind: str, local_day: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE kind = ? AND local_day = ?", (kind, local_day)
        ).fetchone()[0]

    def finish_email_message(
        self,
        row_id: int,
        *,
        status: str,
        note: str | None = None,
        jobs_found: int = 0,
        call_id: int | None = None,
        attempts_inc: int = 1,
        keep_input: bool = False,
    ) -> None:
        """Record the LLM outcome; the stored text is dropped unless the message stays pending."""
        self.conn.execute(
            "UPDATE email_messages SET status = ?, note = ?, jobs_found = ?, llm_call_id = ?, "
            "llm_attempts = llm_attempts + ?, "
            "llm_input = CASE WHEN ? THEN llm_input ELSE NULL END WHERE id = ?",
            (
                status,
                note[:200] if note else None,
                jobs_found,
                call_id,
                attempts_inc,
                int(keep_input),
                row_id,
            ),
        )

    def expire_email_inputs(self, now: datetime, ttl_days: int) -> int:
        cutoff = iso(now - timedelta(days=ttl_days))
        cur = self.conn.execute(
            "UPDATE email_messages SET status = 'expired', llm_input = NULL "
            "WHERE status = 'needs_llm' AND first_seen_at < ?",
            (cutoff,),
        )
        return cur.rowcount

    def latest_run_id(self, source: str) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(id) FROM source_runs WHERE source = ?", (source,)
        ).fetchone()
        return row[0]

    def start_run(self, source: str, now: datetime) -> int:
        cur = self.conn.execute(
            "INSERT INTO source_runs (source, started_at, status) VALUES (?, ?, 'running')",
            (source, iso(now)),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        found: int,
        new: int,
        invalid: int,
        error_type: str | None = None,
        error_message: str | None = None,
        http_status: int | None = None,
        now: datetime,
    ) -> None:
        started = self.conn.execute(
            "SELECT started_at FROM source_runs WHERE id = ?", (run_id,)
        ).fetchone()
        duration_ms = int((now - datetime.fromisoformat(started[0])).total_seconds() * 1000)
        self.conn.execute(
            "UPDATE source_runs SET finished_at=?, status=?, found=?, new=?, invalid=?, "
            "error_type=?, error_message=?, http_status=?, duration_ms=? WHERE id=?",
            (
                iso(now),
                status,
                found,
                new,
                invalid,
                error_type,
                error_message[:500] if error_message else None,
                http_status,
                duration_ms,
                run_id,
            ),
        )

    def upsert_job(self, job: Job, run_id: int, now: datetime) -> tuple[int, bool]:
        """Insert or refresh a job and record a sighting. Returns (job_id, is_new).

        Must run inside `Store.transaction()`: the insert, the sighting and the run counters
        have to commit or roll back together.
        """
        assert self.conn.in_transaction, "upsert_job must run inside Store.transaction()"
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE source = ? AND source_id = ?",
            (job.source, job.source_id),
        ).fetchone()
        values = {
            "url": job.url,
            "apply_url": job.apply_url,
            "company": job.company,
            "title": job.title,
            "description_html": job.description_html,
            "description_text": job.description_text,
            "location_text": job.location_text,
            "remote": None if job.remote is None else int(job.remote),
            "employment_type": job.employment_type,
            "tags_json": json.dumps(job.tags, ensure_ascii=False),
            "salary_min": job.salary_min,
            "salary_max": job.salary_max,
            "salary_currency": job.salary_currency,
            "salary_period": job.salary_period,
            "salary_text": job.salary_text,
            "posted_at": iso(job.posted_at) if job.posted_at else None,
            "fetched_at": iso(job.fetched_at),
            "raw_json": json.dumps(job.raw, ensure_ascii=False, default=str),
            "content_hash": job.content_hash,
        }
        if row is None:
            # canonical_id points at itself until dedupe (#3) redirects it; allocate the id
            # up front so the self-reference holds on insert (writers are serialised by BEGIN)
            job_id = self.conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM jobs").fetchone()[0]
            cols = [
                "id",
                "canonical_id",
                "source",
                "source_id",
                "first_seen_at",
                "last_seen_at",
                *values,
            ]
            params = [
                job_id,
                job_id,
                job.source,
                job.source_id,
                iso(now),
                iso(now),
                *values.values(),
            ]
            self.conn.execute(
                f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                params,
            )
            is_new = True
        else:
            job_id = row["id"]
            # refresh when any stored field changed (not only the text behind content_hash)
            if any(row[c] != v for c, v in values.items() if c != "fetched_at"):
                sets = ", ".join(f"{c} = ?" for c in values)
                self.conn.execute(
                    f"UPDATE jobs SET {sets}, last_seen_at = ? WHERE id = ?",
                    [*values.values(), iso(now), job_id],
                )
            else:
                self.conn.execute(
                    "UPDATE jobs SET last_seen_at = ? WHERE id = ?", (iso(now), job_id)
                )
            is_new = False
        self.conn.execute(
            "INSERT OR IGNORE INTO sightings (job_id, run_id, seen_at) VALUES (?, ?, ?)",
            (job_id, run_id, iso(now)),
        )
        return job_id, is_new

    def _consecutive_failures(self, source: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM source_runs WHERE source = ? AND status = 'error' "
            "AND id > COALESCE((SELECT MAX(id) FROM source_runs "
            "WHERE source = ? AND status = 'ok'), 0)",
            (source, source),
        ).fetchone()[0]

    def source_state(self, source: str) -> SourceState:
        row = self.conn.execute(
            "SELECT enabled, interval_override_s, "
            "(SELECT MAX(started_at) FROM source_runs r WHERE r.source = s.name) AS last_started "
            "FROM sources s WHERE name = ?",
            (source,),
        ).fetchone()
        return SourceState(
            enabled=bool(row["enabled"]),
            interval_override_s=row["interval_override_s"],
            last_started_at=_dt(row["last_started"]),
            consecutive_failures=self._consecutive_failures(source),
        )

    def source_health(self) -> list[SourceHealth]:
        out = []
        for s in self.conn.execute("SELECT * FROM sources ORDER BY name").fetchall():
            name = s["name"]
            last = self.conn.execute(
                "SELECT started_at, status, found, new FROM source_runs WHERE source = ? "
                "ORDER BY id DESC LIMIT 1",
                (name,),
            ).fetchone()
            ok = self.conn.execute(
                "SELECT MAX(finished_at) FROM source_runs WHERE source = ? AND status = 'ok'",
                (name,),
            ).fetchone()[0]
            err = self.conn.execute(
                "SELECT error_type, error_message FROM source_runs "
                "WHERE source = ? AND status = 'error' ORDER BY id DESC LIMIT 1",
                (name,),
            ).fetchone()
            fails = self._consecutive_failures(name)
            last_error = None
            if err is not None and fails > 0:
                last_error = f"{err['error_type']}: {err['error_message']}"
            out.append(
                SourceHealth(
                    name=name,
                    enabled=bool(s["enabled"]),
                    interval_override_s=s["interval_override_s"],
                    last_started_at=_dt(last["started_at"]) if last else None,
                    last_status=last["status"] if last else None,
                    last_found=last["found"] if last else None,
                    last_new=last["new"] if last else None,
                    last_success_at=_dt(ok),
                    consecutive_failures=fails,
                    last_error=last_error,
                    jobs_total=self.conn.execute(
                        "SELECT COUNT(*) FROM jobs WHERE source = ?", (name,)
                    ).fetchone()[0],
                )
            )
        return out

    def latest_jobs(self, n: int) -> list[JobRow]:
        rows = self.conn.execute(
            "SELECT id, source, company, title, url, posted_at, first_seen_at FROM jobs "
            "ORDER BY posted_at IS NULL, posted_at DESC, first_seen_at DESC LIMIT ?",
            (n,),
        ).fetchall()
        return [
            JobRow(
                id=r["id"],
                source=r["source"],
                company=r["company"],
                title=r["title"],
                url=r["url"],
                posted_at=_dt(r["posted_at"]),
                first_seen_at=datetime.fromisoformat(r["first_seen_at"]),
            )
            for r in rows
        ]

    def mark_stale_runs(self, older_than: timedelta, now: datetime) -> int:
        cutoff = iso(now - older_than)
        cur = self.conn.execute(
            "UPDATE source_runs SET status='error', error_type='interrupted', "
            "error_message='run did not finish', finished_at=? "
            "WHERE status='running' AND started_at < ?",
            (iso(now), cutoff),
        )
        return cur.rowcount

    # --- triage (issue #3) ---

    def jobs_for_triage(self, rule_version: str) -> list[sqlite3.Row]:
        """Jobs with no `rules` verdict for this version, or seen after their latest verdict."""
        return self.conn.execute(
            "SELECT j.*, "
            "(SELECT MAX(at) FROM verdicts v WHERE v.job_id = j.id AND v.stage = 'rules' "
            "  AND v.rule_version = ?1) AS last_verdict_at "
            "FROM jobs j WHERE NOT EXISTS (SELECT 1 FROM verdicts v WHERE v.job_id = j.id "
            "  AND v.stage = 'rules' AND v.rule_version = ?1) "
            "OR j.last_seen_at > (SELECT MAX(at) FROM verdicts v WHERE v.job_id = j.id "
            "  AND v.stage = 'rules' AND v.rule_version = ?1) "
            "ORDER BY j.id",
            (rule_version,),
        ).fetchall()

    def insert_verdict(
        self,
        *,
        job_id: int,
        stage: str,
        decision: str,
        reasons: list[str],
        flags: list[str],
        relevance: int | None,
        profile: str | None,
        details: dict,
        rule_version: str,
        input_hash: str,
        now: datetime,
    ) -> None:
        self.conn.execute(
            "INSERT INTO verdicts (job_id, stage, decision, reasons_json, flags_json, relevance, "
            "profile, details_json, rule_version, input_hash, at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(job_id, stage, rule_version, input_hash) DO UPDATE SET at = excluded.at",
            (
                job_id,
                stage,
                decision,
                json.dumps(reasons),
                json.dumps(flags),
                relevance,
                profile,
                json.dumps(details, ensure_ascii=False),
                rule_version,
                input_hash,
                iso(now),
            ),
        )

    def latest_verdicts(
        self, rule_version: str | None, job_ids: list[int] | None = None
    ) -> dict[int, sqlite3.Row]:
        """Latest `rules` verdict per job in one query (rule_version None = any version)."""
        where, params = ["stage = 'rules'"], []
        if rule_version is not None:
            where.append("rule_version = ?")
            params.append(rule_version)
        if job_ids is not None:
            where.append(f"job_id IN ({','.join('?' * len(job_ids))})")
            params += job_ids
        rows = self.conn.execute(
            "SELECT * FROM (SELECT v.*, ROW_NUMBER() OVER "
            "(PARTITION BY job_id ORDER BY at DESC, id DESC) AS rn FROM verdicts v "
            f"WHERE {' AND '.join(where)}) WHERE rn = 1",
            params,
        ).fetchall()
        return {r["job_id"]: r for r in rows}

    def latest_verdict(self, job_id: int, rule_version: str | None) -> sqlite3.Row | None:
        return self.latest_verdicts(rule_version, [job_id]).get(job_id)

    def kept_canonicals(self, rule_version: str, threshold: float) -> list[KeptGroup]:
        """One entry per canonical group that is keep: no member has a hard reason and the
        best member relevance reaches the threshold. Ordered by relevance desc, posted_at desc."""
        return self.classified_groups(rule_version, threshold)[0]

    def classified_groups(
        self,
        rule_version: str,
        threshold: float,
        *,
        rejected_since: datetime | None = None,
        rejected_limit: int = 500,
    ) -> tuple[list[KeptGroup], list[KeptGroup], bool]:
        """(kept, rejected, rejected_capped) in one pass over the jobs.

        kept: ordered by relevance desc, posted_at desc. rejected: groups the rules turned down,
        posted since `rejected_since` (groups without a date stay), newest first, at most
        `rejected_limit`; the flag says the list was cut."""
        jobs = self.conn.execute(
            "SELECT id, canonical_id, source, company, title, url, posted_at, salary_text, "
            "salary_min, salary_max, salary_currency, salary_period FROM jobs ORDER BY id"
        ).fetchall()
        groups: dict[int, list[sqlite3.Row]] = {}
        for j in jobs:
            groups.setdefault(j["canonical_id"], []).append(j)
        verdicts = self.latest_verdicts(rule_version)
        kept: list[KeptGroup] = []
        rejected: list[KeptGroup] = []
        for cid, members in groups.items():
            flags: set[str] = set()
            pairs: list[tuple[list[str], list[str]]] = []
            best: tuple[int, int, sqlite3.Row] | None = None
            profile = None
            for m in members:
                v = verdicts.get(m["id"])
                if v is None:
                    continue
                pairs.append((json.loads(v["reasons_json"]), json.loads(v["flags_json"])))
                flags |= set(pairs[-1][1])
                rel = v["relevance"] or 0
                if best is None or rel > best[0] or (rel == best[0] and m["id"] < best[1]):
                    best = (rel, m["id"], m)
                    profile = v["profile"]
            if best is None:
                continue  # no rules verdict yet
            rep = best[2]
            group = KeptGroup(
                canonical_id=cid,
                representative_id=rep["id"],
                relevance=best[0],
                profile=profile,
                flags=sorted(flags),
                member_ids=[m["id"] for m in members],
                sources=sorted({m["source"] for m in members}),
                company=rep["company"],
                title=rep["title"],
                url=rep["url"],
                rate_display=rate_display(members),
                posted_at=max(
                    (_dt(m["posted_at"]) for m in members if m["posted_at"]), default=None
                ),
            )
            codes = group_reasons(pairs)
            (rejected if rule_outcome(codes, best[0], threshold) else kept).append(group)
        kept.sort(
            key=lambda g: (
                -g.relevance,
                -(g.posted_at.timestamp() if g.posted_at else 0),
                g.canonical_id,
            )
        )
        if rejected_since is not None:
            rejected = [g for g in rejected if g.posted_at is None or g.posted_at >= rejected_since]
        rejected.sort(
            key=lambda g: (-(g.posted_at.timestamp() if g.posted_at else 0), g.canonical_id)
        )
        return kept, rejected[:rejected_limit], len(rejected) > rejected_limit

    def explain(self, job_id: int, rule_version: str | None = None) -> Explanation | None:
        job = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            return None
        members = self.conn.execute(
            "SELECT * FROM jobs WHERE canonical_id = ? ORDER BY id", (job["canonical_id"],)
        ).fetchall()
        found = self.latest_verdicts(rule_version, [m["id"] for m in members])
        return Explanation(
            job=job, members=members, verdicts={m["id"]: found.get(m["id"]) for m in members}
        )

    # --- LLM scoring (issue #4) ---

    def job_for_llm(self, job_id: int, rule_version: str | None) -> LLMJobInput:
        j = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        v = self.latest_verdict(job_id, rule_version)
        extra = self.extra_description(j["canonical_id"])
        flags: list[str] = []
        evidence: dict[str, str] = {}
        if v is not None:
            flags = json.loads(v["flags_json"])
            evidence = dict(json.loads(v["details_json"]).get("evidence", {}))
        return LLMJobInput(
            job_id=job_id,
            title=j["title"],
            company=j["company"],
            location_text=j["location_text"],
            employment_type=j["employment_type"],
            tags=json.loads(j["tags_json"] or "[]"),
            description_text=j["description_text"],
            first_seen_at=datetime.fromisoformat(j["first_seen_at"]),
            posted_at=_dt(j["posted_at"]),
            rule_flags=flags,
            rule_evidence=evidence,
            extra_description=extra["body"] if extra else None,
            extra_scored=bool(extra and extra["use_in_score"]),
        )

    # --- Owner's extra description (#41): untrusted job-ad text, one per duplicate group.

    def extra_description(self, canonical_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM job_extra_descriptions WHERE canonical_id = ?", (canonical_id,)
        ).fetchone()

    def set_extra_description(self, canonical_id: int, body: str, now: datetime) -> None:
        """Save (or replace) the text; an empty body deletes it. A new text is not used by the
        Haiku score until the owner asks for a re-score."""
        with self.transaction():
            if not body:
                self.conn.execute(
                    "DELETE FROM job_extra_descriptions WHERE canonical_id = ?", (canonical_id,)
                )
                return
            self.conn.execute(
                "INSERT INTO job_extra_descriptions (canonical_id, body, updated_at, use_in_score) "
                "VALUES (?, ?, ?, 0) ON CONFLICT(canonical_id) DO UPDATE SET "
                "body = excluded.body, updated_at = excluded.updated_at, use_in_score = 0 "
                "WHERE body != excluded.body",
                (canonical_id, body, iso(now)),
            )

    def request_rescore(self, canonical_id: int, now: datetime) -> int | None:
        """Queue one Haiku re-score of the group with its extra text. None if there is no text
        or one is already queued."""
        with self.transaction():
            if self.extra_description(canonical_id) is None:
                return None
            if self.conn.execute(
                "SELECT 1 FROM rescore_requests WHERE canonical_id = ? AND status = 'queued'",
                (canonical_id,),
            ).fetchone():
                return None
            self.conn.execute(
                "UPDATE job_extra_descriptions SET use_in_score = 1 WHERE canonical_id = ?",
                (canonical_id,),
            )
            cur = self.conn.execute(
                "INSERT INTO rescore_requests (canonical_id, status, requested_at) "
                "VALUES (?, 'queued', ?)",
                (canonical_id, iso(now)),
            )
            return cur.lastrowid

    def queued_rescore_for(self, canonical_id: int) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM rescore_requests WHERE canonical_id = ? AND status = 'queued'",
                (canonical_id,),
            ).fetchone()
            is not None
        )

    def queued_rescore(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM rescore_requests WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()

    def finish_rescore(self, request_id: int, status: str, now: datetime) -> None:
        self.conn.execute(
            "UPDATE rescore_requests SET status = ?, finished_at = ? "
            "WHERE id = ? AND status = 'queued'",
            (status, iso(now), request_id),
        )

    def cache_get(self, item_hash: str, alias: str, prompt_version: str) -> JobVerdict | None:
        row = self.conn.execute(
            "SELECT output_json FROM llm_cache WHERE item_hash=? AND model_alias=? "
            "AND prompt_version=?",
            (item_hash, alias, prompt_version),
        ).fetchone()
        return JobVerdict.model_validate_json(row[0]) if row else None

    def cache_put(
        self,
        item_hash: str,
        alias: str,
        prompt_version: str,
        verdict: JobVerdict,
        call_id: int | None,
        now: datetime,
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO llm_cache (item_hash, model_alias, prompt_version, "
            "output_json, call_id, created_at) VALUES (?,?,?,?,?,?)",
            (item_hash, alias, prompt_version, verdict.model_dump_json(), call_id, iso(now)),
        )

    def insert_llm_call(
        self,
        *,
        kind: str,
        model_alias: str,
        prompt_version: str | None,
        n_items: int,
        attempt: int,
        now: datetime,
        local_day: str,
        status: str,
        model: str | None = None,
        duration_ms: int | None = None,
        exit_code: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_creation_tokens: int | None = None,
        total_cost_usd: float | None = None,
        error_message: str | None = None,
        auth_method: str | None = None,
        pause_until: datetime | None = None,
        credential_expires_at: str | None = None,
    ) -> int:
        if kind not in CORE_LLM_KINDS and kind not in plugins.llm_kinds():
            raise ValueError(f"unknown llm call kind {kind!r}")
        cur = self.conn.execute(
            "INSERT INTO llm_calls (kind, model_alias, model, prompt_version, n_items, attempt, "
            "started_at, local_day, duration_ms, status, exit_code, input_tokens, output_tokens, "
            "cache_read_tokens, cache_creation_tokens, total_cost_usd, error_message, "
            "auth_method, pause_until, credential_expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                kind,
                model_alias,
                model,
                prompt_version,
                n_items,
                attempt,
                iso(now),
                local_day,
                duration_ms,
                status,
                exit_code,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_creation_tokens,
                total_cost_usd,
                error_message[:300] if error_message else None,
                auth_method,
                iso(pause_until) if pause_until else None,
                credential_expires_at,
            ),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def item_error_get(self, item_hash: str, alias: str, prompt_version: str) -> str | None:
        row = self.conn.execute(
            "SELECT error FROM llm_item_errors WHERE item_hash=? AND model_alias=? "
            "AND prompt_version=?",
            (item_hash, alias, prompt_version),
        ).fetchone()
        return row[0] if row else None

    def item_error_put(
        self,
        job_id: int,
        item_hash: str,
        alias: str,
        prompt_version: str,
        call_id: int | None,
        error: str,
        now: datetime,
    ) -> None:
        self.conn.execute(
            "INSERT INTO llm_item_errors (job_id, item_hash, model_alias, prompt_version, "
            "call_id, error, at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(item_hash, model_alias, prompt_version) DO UPDATE SET "
            "error=excluded.error, call_id=excluded.call_id, at=excluded.at, "
            "job_id=excluded.job_id",
            (job_id, item_hash, alias, prompt_version, call_id, error, iso(now)),
        )

    def item_error_clear(self, item_hash: str, alias: str, prompt_version: str) -> None:
        self.conn.execute(
            "DELETE FROM llm_item_errors WHERE item_hash=? AND model_alias=? AND prompt_version=?",
            (item_hash, alias, prompt_version),
        )

    def insert_llm_verdict(
        self,
        *,
        job_id: int,
        verdict: JobVerdict,
        prompt_version: str,
        item_hash: str,
        call_id: int | None,
        model: str | None,
        cached: bool,
        now: datetime,
        feedback_rev: str | None = None,
    ) -> bool:
        """Write the verdict unless it is already the latest one for this job.

        Returns True if something was written. If the text went A -> B -> A, the old row for A
        is bumped (`at = now`) so the latest verdict by time is the current text's.
        """
        latest = self.conn.execute(
            "SELECT input_hash FROM verdicts WHERE job_id=? AND stage='llm' AND rule_version=? "
            "ORDER BY at DESC, id DESC LIMIT 1",
            (job_id, prompt_version),
        ).fetchone()
        if latest is not None and latest[0] == item_hash:
            return False
        self.conn.execute(
            "INSERT INTO verdicts (job_id, stage, decision, reasons_json, flags_json, relevance, "
            "profile, details_json, rule_version, input_hash, at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(job_id, stage, rule_version, input_hash) DO UPDATE SET "
            "at = excluded.at, details_json = excluded.details_json, decision = excluded.decision, "
            "reasons_json = excluded.reasons_json, relevance = excluded.relevance, "
            "profile = excluded.profile",
            (
                job_id,
                "llm",
                "keep" if verdict.eligible else "reject",
                json.dumps([] if verdict.eligible else ["llm_ineligible"]),
                "[]",
                verdict.fit,
                None if verdict.profile == "none" else verdict.profile,
                json.dumps(
                    {
                        "verdict": verdict.model_dump(),
                        "call_id": call_id,
                        "model": model,
                        "cached": cached,
                        **({"feedback_rev": feedback_rev} if feedback_rev else {}),
                    },
                    ensure_ascii=False,
                ),
                prompt_version,
                item_hash,
                iso(now),
            ),
        )
        return True

    def update_llm_call(
        self,
        call_id: int,
        *,
        status: str,
        model: str | None = None,
        duration_ms: int | None = None,
        exit_code: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_creation_tokens: int | None = None,
        total_cost_usd: float | None = None,
        error_message: str | None = None,
        pause_until: datetime | None = None,
        credential_expires_at: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE llm_calls SET status=?, model=?, duration_ms=?, exit_code=?, input_tokens=?, "
            "output_tokens=?, cache_read_tokens=?, cache_creation_tokens=?, total_cost_usd=?, "
            "error_message=?, pause_until=?, credential_expires_at=? WHERE id=?",
            (
                status,
                model,
                duration_ms,
                exit_code,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_creation_tokens,
                total_cost_usd,
                error_message[:300] if error_message else None,
                iso(pause_until) if pause_until else None,
                credential_expires_at,
                call_id,
            ),
        )

    def calls_today(self, alias: str, local_day: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE kind != 'preflight' AND model_alias=? "
            "AND local_day=?",
            (alias, local_day),
        ).fetchone()[0]

    def cost_today(self, local_day: str) -> float:
        return self.conn.execute(
            "SELECT COALESCE(SUM(total_cost_usd), 0) FROM llm_calls WHERE local_day=?",
            (local_day,),
        ).fetchone()[0]

    def active_pause(self, now: datetime) -> tuple[datetime, str] | None:
        rows = self.conn.execute(
            "SELECT pause_until, status FROM llm_calls WHERE pause_until IS NOT NULL "
            "ORDER BY id DESC LIMIT 20"
        ).fetchall()
        best: tuple[datetime, str] | None = None
        for r in rows:
            until = datetime.fromisoformat(r["pause_until"])
            if until > now and (best is None or until > best[0]):
                best = (until, r["status"])
        return best

    def consecutive_limit_hits(self) -> int:
        """usage_limit calls since the last ok call."""
        kinds = _pause_kinds_sql()
        return self.conn.execute(
            f"SELECT COUNT(*) FROM llm_calls WHERE kind IN ({kinds}) "
            "AND status='usage_limit' "
            f"AND id > COALESCE((SELECT MAX(id) FROM llm_calls WHERE kind IN ({kinds}) "
            "AND status IN ('ok','partial')), 0)"
        ).fetchone()[0]

    def usage_summary(self, since_day: str) -> list[UsageRow]:
        rows = self.conn.execute(
            "SELECT local_day, model_alias, COUNT(*) AS calls, "
            "SUM(status IN ('ok','partial')) AS ok, SUM(status NOT IN ('ok','partial')) AS err, "
            "COALESCE(SUM(input_tokens + COALESCE(cache_read_tokens,0) "
            "  + COALESCE(cache_creation_tokens,0)), 0) AS tin, "
            "COALESCE(SUM(output_tokens), 0) AS tout, COALESCE(SUM(total_cost_usd), 0) AS cost "
            "FROM llm_calls WHERE kind != 'preflight' AND local_day >= ? "
            "GROUP BY local_day, model_alias ORDER BY local_day DESC, model_alias",
            (since_day,),
        ).fetchall()
        return [UsageRow(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows]

    def last_auth_info(self) -> tuple[str | None, str | None]:
        """(auth method, credential expiry), each from its own latest non-null row.

        Only preflight rows carry the auth method; score/cv rows carry just the expiry, so a
        single "latest row with either" would hide the method.
        """

        def latest(col: str) -> str | None:
            row = self.conn.execute(
                f"SELECT {col} FROM llm_calls WHERE {col} IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None

        return latest("auth_method"), latest("credential_expires_at")

    def llm_verdicts(
        self, prompt_version: str, canonical_ids: list[int] | None = None
    ) -> list[ScoredGroup]:
        """Latest `llm` verdict per canonical group (contract for #5/#6)."""
        where, params = ["v.stage = 'llm'", "v.rule_version = ?"], [prompt_version]
        if canonical_ids is not None:
            where.append(f"j.canonical_id IN ({','.join('?' * len(canonical_ids))})")
            params += canonical_ids
        rows = self.conn.execute(
            "SELECT * FROM (SELECT v.job_id, v.decision, v.at, v.details_json, v.input_hash, "
            "j.canonical_id, "
            "ROW_NUMBER() OVER (PARTITION BY j.canonical_id ORDER BY v.at DESC, v.id DESC) AS rn "
            f"FROM verdicts v JOIN jobs j ON j.id = v.job_id WHERE {' AND '.join(where)}) "
            "WHERE rn = 1",
            params,
        ).fetchall()
        return [
            ScoredGroup(
                canonical_id=r["canonical_id"],
                job_id=r["job_id"],
                verdict=JobVerdict.model_validate(json.loads(r["details_json"])["verdict"]),
                decision=r["decision"],
                at=datetime.fromisoformat(r["at"]),
                input_hash=r["input_hash"],
                model=json.loads(r["details_json"]).get("model"),
                cached=bool(json.loads(r["details_json"]).get("cached")),
            )
            for r in rows
        ]

    def llm_errors(self, prompt_version: str) -> dict[int, str]:
        """canonical_id -> last llm_error reason for this prompt version.

        Consumers (#5/#6) must intersect with `kept_canonicals`: an error row can outlive the
        group's keep status, and a later verdict (see `llm_verdicts`) supersedes the error.
        """
        rows = self.conn.execute(
            "SELECT j.canonical_id, e.error FROM llm_item_errors e JOIN jobs j ON j.id = e.job_id "
            "WHERE e.prompt_version = ? ORDER BY e.at, e.id",
            (prompt_version,),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def latest_llm_verdict(self, job_id: int) -> tuple[JobVerdict, dict, str] | None:
        """Latest llm verdict for a job's canonical group: (verdict, details, prompt_version)."""
        row = self.conn.execute(
            "SELECT v.details_json, v.rule_version, v.at FROM verdicts v JOIN jobs j ON "
            "j.id = v.job_id WHERE v.stage='llm' AND j.canonical_id = "
            "(SELECT canonical_id FROM jobs WHERE id = ?) ORDER BY v.at DESC, v.id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        d = json.loads(row["details_json"])
        return JobVerdict.model_validate(d["verdict"]), d, row["rule_version"]

    # --- worker (issue #5) ---

    def state_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM worker_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def state_set(self, key: str, value: str, now: datetime) -> None:
        self.conn.execute(
            "INSERT INTO worker_state (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, iso(now)),
        )

    # --- keyed API call budget (issue #19, migration 0017) ---

    def api_calls_add(self, api: str, now: datetime, n: int = 1) -> None:
        self.conn.execute(
            "INSERT INTO api_calls (api, day, calls, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(api, day) DO UPDATE SET calls = calls + excluded.calls, "
            "updated_at = excluded.updated_at",
            (api, now.astimezone(UTC).date().isoformat(), n, iso(now)),
        )

    def api_calls_day(self, api: str, now: datetime) -> int:
        row = self.conn.execute(
            "SELECT calls FROM api_calls WHERE api = ? AND day = ?",
            (api, now.astimezone(UTC).date().isoformat()),
        ).fetchone()
        return row[0] if row else 0

    def api_calls_month(self, api: str, now: datetime) -> int:
        month = now.astimezone(UTC).strftime("%Y-%m")
        row = self.conn.execute(
            "SELECT COALESCE(SUM(calls), 0) FROM api_calls WHERE api = ? AND substr(day, 1, 7) = ?",
            (api, month),
        ).fetchone()
        return int(row[0])

    def state_get_dt(self, key: str) -> datetime | None:
        return _dt(self.state_get(key))

    def start_cycle(self, now: datetime) -> int:
        cur = self.conn.execute(
            "INSERT INTO worker_cycles (started_at, status) VALUES (?, 'running')", (iso(now),)
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def finish_cycle(self, cycle_id: int, *, status: str, now: datetime, **fields: object) -> None:
        allowed = {
            "sources_run",
            "sources_deferred",
            "new_jobs",
            "scored",
            "score_stopped",
            "alerts_sent",
            "alerts_summarized",
            "error",
        }
        assert set(fields) <= allowed, set(fields) - allowed
        started = self.conn.execute(
            "SELECT started_at FROM worker_cycles WHERE id = ?", (cycle_id,)
        ).fetchone()[0]
        duration_ms = int((now - datetime.fromisoformat(started)).total_seconds() * 1000)
        sets = "".join(f", {k} = ?" for k in fields)
        vals = [v[:500] if isinstance(v, str) else v for v in fields.values()]
        self.conn.execute(
            f"UPDATE worker_cycles SET finished_at = ?, status = ?, duration_ms = ?{sets} "
            "WHERE id = ?",
            [iso(now), status, duration_ms, *vals, cycle_id],
        )

    def interrupt_running_cycles(self, now: datetime) -> int:
        cur = self.conn.execute(
            "UPDATE worker_cycles SET status='interrupted', finished_at=? WHERE status='running'",
            (iso(now),),
        )
        return cur.rowcount

    # --- alerts, decisions, CV requests (issue #5; #6 and #7 use the decision/CV methods) ---

    def alerted_ids(self) -> tuple[set[int], set[int]]:
        """(canonical ids, job ids) that already have an `alerts` row, whatever its status."""
        rows = self.conn.execute("SELECT canonical_id, job_id FROM alerts").fetchall()
        return {r[0] for r in rows}, {r[1] for r in rows}

    def group_members(self, canonical_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, source, url, company, title, location_text, posted_at, first_seen_at, "
            "raw_json, salary_text FROM jobs WHERE canonical_id = ? ORDER BY id",
            (canonical_id,),
        ).fetchall()

    def llm_verdict_row_id(self, job_id: int, prompt_version: str, input_hash: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM verdicts WHERE job_id = ? AND stage = 'llm' AND rule_version = ? "
            "AND input_hash = ? ORDER BY at DESC, id DESC LIMIT 1",
            (job_id, prompt_version, input_hash),
        ).fetchone()
        return row[0] if row else None

    def verdict_by_id(self, verdict_id: int | None) -> tuple[JobVerdict, str] | None:
        """(llm verdict, input_hash) of one `verdicts` row, or None if it is not an llm verdict."""
        if verdict_id is None:
            return None
        row = self.conn.execute(
            "SELECT details_json, input_hash FROM verdicts WHERE id = ? AND stage = 'llm'",
            (verdict_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            return JobVerdict.model_validate(json.loads(row[0])["verdict"]), row[1]
        except (ValueError, KeyError):
            return None

    def insert_alert(
        self,
        *,
        canonical_id: int,
        job_id: int,
        llm_verdict_id: int | None,
        fit: int,
        status: str,
        now: datetime,
    ) -> int | None:
        """None if the group already has an alert (canonical_id is unique)."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO alerts (canonical_id, job_id, llm_verdict_id, fit, status, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (canonical_id, job_id, llm_verdict_id, fit, status, iso(now)),
        )
        return cur.lastrowid if cur.rowcount else None

    def alert(self, alert_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()

    def pending_alerts(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM alerts WHERE status = 'pending' ORDER BY fit DESC, created_at, id"
        ).fetchall()

    def set_alert_status(self, alert_ids: list[int], status: str, now: datetime) -> None:
        for aid in alert_ids:
            self.conn.execute(
                "UPDATE alerts SET status = ?, sent_at = CASE WHEN ? IN ('sent','summarized') "
                "THEN ? ELSE sent_at END WHERE id = ?",
                (status, status, iso(now), aid),
            )

    def mark_alert_sent(
        self, alert_id: int, *, chat_id: int, message_id: int, text: str, now: datetime
    ) -> None:
        self.conn.execute(
            "UPDATE alerts SET status='sent', chat_id=?, message_id=?, text=?, sent_at=?, "
            "attempts = attempts + 1, last_error = NULL WHERE id = ?",
            (chat_id, message_id, text, iso(now), alert_id),
        )

    def llm_verdict_for_hash(
        self, canonical_id: int, prompt_version: str, input_hash: str
    ) -> tuple[int, JobVerdict, int] | None:
        """(verdict row id, verdict, job_id) of this group's llm verdict for one input hash."""
        row = self.conn.execute(
            "SELECT v.id, v.job_id, v.details_json FROM verdicts v JOIN jobs j ON j.id = v.job_id "
            "WHERE j.canonical_id = ? AND v.stage = 'llm' AND v.rule_version = ? "
            "AND v.input_hash = ? ORDER BY v.at DESC, v.id DESC LIMIT 1",
            (canonical_id, prompt_version, input_hash),
        ).fetchone()
        if row is None:
            return None
        try:
            return row[0], JobVerdict.model_validate(json.loads(row[2])["verdict"]), row[1]
        except (ValueError, KeyError):
            return None

    def rebind_alert(self, alert_id: int, verdict_id: int, job_id: int, fit: int) -> None:
        self.conn.execute(
            "UPDATE alerts SET llm_verdict_id = ?, job_id = ?, fit = ? WHERE id = ?",
            (verdict_id, job_id, fit, alert_id),
        )

    def mark_alert_attempt_failed(self, alert_id: int, error: str, max_attempts: int) -> str:
        """Count a failed send. Returns the new status ('pending' or 'failed')."""
        self.conn.execute(
            "UPDATE alerts SET attempts = attempts + 1, last_error = ?, "
            "status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE status END WHERE id = ?",
            (error[:300], max_attempts, alert_id),
        )
        return self.conn.execute("SELECT status FROM alerts WHERE id = ?", (alert_id,)).fetchone()[
            0
        ]

    def latest_alerts(self, n: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT a.*, j.title, j.company FROM alerts a JOIN jobs j ON j.id = a.job_id "
            "ORDER BY a.id DESC LIMIT ?",
            (n,),
        ).fetchall()

    def insert_system_alert(self, kind: str, dedupe_key: str, text: str, now: datetime) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO system_alerts (kind, dedupe_key, text, status, created_at) "
            "VALUES (?,?,?, 'pending', ?)",
            (kind, dedupe_key, text[:1000], iso(now)),
        )
        return cur.rowcount == 1

    def pending_system_alerts(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM system_alerts WHERE status = 'pending' ORDER BY id"
        ).fetchall()

    def set_system_alert_status(self, alert_id: int, status: str, now: datetime) -> None:
        self.conn.execute(
            "UPDATE system_alerts SET status = ?, sent_at = ? WHERE id = ?",
            (status, iso(now) if status == "sent" else None, alert_id),
        )

    def system_alert_attempt_failed(self, alert_id: int, error: str, max_attempts: int) -> None:
        self.conn.execute(
            "UPDATE system_alerts SET attempts = attempts + 1, last_error = ?, "
            "status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE status END WHERE id = ?",
            (error[:300], max_attempts, alert_id),
        )

    def last_pause_call(self) -> sqlite3.Row | None:
        """The newest llm_calls row that set a pause (usage limit or auth problem)."""
        return self.conn.execute(
            "SELECT status, pause_until, local_day, error_message FROM llm_calls "
            "WHERE pause_until IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def recent_decisions(self, n: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT d.*, j.title, j.company FROM decisions d JOIN jobs j ON j.id = d.job_id "
            "ORDER BY d.id DESC LIMIT ?",
            (n,),
        ).fetchall()

    # decisions: the current decision of a job is its newest row; 'undo' cancels the previous one

    def _insert_decision(
        self,
        job_id: int,
        action: str,
        *,
        channel: str,
        reason: str | None,
        note: str | None,
        alert_id: int | None,
        tg_update_id: int | None,
        now: datetime,
        withdrawn: bool = False,
    ) -> int:
        row = self.conn.execute("SELECT canonical_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"no job {job_id}")
        cur = self.conn.execute(
            "INSERT INTO decisions (canonical_id, job_id, action, reason, note, channel, alert_id, "
            "tg_update_id, at, withdrawn) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                row["canonical_id"],
                job_id,
                action,
                reason,
                note,
                channel,
                alert_id,
                tg_update_id,
                iso(now),
                int(withdrawn),
            ),
        )
        assert cur.lastrowid is not None
        self.mark_seen([row["canonical_id"]], now)  # any decision (panel or Telegram) = seen
        return cur.lastrowid

    def mark_seen(self, canonical_ids: Iterable[int], now: datetime) -> None:
        """Mark duplicate groups as seen; the first `seen_at` is kept. Unknown ids are skipped."""
        self.conn.executemany(
            "INSERT OR IGNORE INTO job_views (canonical_id, seen_at) "
            "SELECT id, ? FROM jobs WHERE id = ? AND canonical_id = id",
            [(iso(now), int(c)) for c in canonical_ids],
        )

    def seen_ids(self) -> set[int]:
        return {r[0] for r in self.conn.execute("SELECT canonical_id FROM job_views")}

    def _decision_by_update(self, tg_update_id: int | None) -> int | None:
        if tg_update_id is None:
            return None
        row = self.conn.execute(
            "SELECT id FROM decisions WHERE tg_update_id = ?", (tg_update_id,)
        ).fetchone()
        return row[0] if row else None

    def record_decision(
        self,
        job_id: int,
        action: str,
        *,
        channel: str,
        reason: str | None = None,
        note: str | None = None,
        alert_id: int | None = None,
        tg_update_id: int | None = None,
        now: datetime,
    ) -> tuple[int, bool]:
        """Append a decision. Returns (decision_id, created); a repeated `tg_update_id` returns
        the stored decision with created=False. 'undo' also cancels a still-queued CV request
        of the job's group (one in progress is left alone). 'cv_requested' goes through
        `request_cv`."""
        if action not in ("interested", "rejected", "undo", "override_in"):
            raise ValueError(f"record_decision cannot write {action!r}; use request_cv")
        if reason is not None and action != "rejected":
            raise ValueError("reason only goes with 'rejected'")
        if note is not None:
            note = note.strip()[:DECISION_NOTE_MAX] or None
        if action == "override_in" and note is None:
            raise ValueError("override_in needs the owner's explanation")
        with self.transaction():
            existing = self._decision_by_update(tg_update_id)
            if existing is not None:
                return existing, False
            did = self._insert_decision(
                job_id,
                action,
                channel=channel,
                reason=reason,
                note=note,
                alert_id=alert_id,
                tg_update_id=tg_update_id,
                now=now,
            )
            if action == "undo":
                self.conn.execute(
                    "UPDATE cv_requests SET status='cancelled', finished_at=? "
                    "WHERE status='queued' AND canonical_id = "
                    "(SELECT canonical_id FROM jobs WHERE id = ?)",
                    (iso(now), job_id),
                )
        return did, True

    def request_cv(
        self,
        job_id: int,
        *,
        channel: str,
        alert_id: int | None = None,
        tg_update_id: int | None = None,
        now: datetime,
    ) -> tuple[int, int, bool]:
        """'cv_requested' decision + queued CV request in one transaction.

        Returns (decision_id, request_id, created). The decision is always written (it is the
        owner's latest word, e.g. after an 'undo'); if the group already has an active request
        (queued or in progress) no second request is created and that one is returned with
        created=False."""
        with self.transaction():
            existing = self._decision_by_update(tg_update_id)
            if existing is not None:
                req = self.conn.execute(
                    "SELECT id FROM cv_requests WHERE decision_id = ?", (existing,)
                ).fetchone()
                return existing, (req[0] if req else 0), False
            active = self.conn.execute(
                "SELECT id, decision_id FROM cv_requests WHERE status IN ('queued','in_progress') "
                "AND kind = 'cv' AND canonical_id = (SELECT canonical_id FROM jobs WHERE id = ?)",
                (job_id,),
            ).fetchone()
            did = self._insert_decision(
                job_id,
                "cv_requested",
                channel=channel,
                reason=None,
                note=None,
                alert_id=alert_id,
                tg_update_id=tg_update_id,
                now=now,
            )
            if active is not None:
                return did, active["id"], False
            cur = self.conn.execute(
                "INSERT INTO cv_requests (canonical_id, job_id, decision_id, status, requested_at) "
                "SELECT canonical_id, id, ?, 'queued', ? FROM jobs WHERE id = ?",
                (did, iso(now), job_id),
            )
            assert cur.lastrowid is not None
        return did, cur.lastrowid, True

    # --- CV drafting queue (#7). All status changes are guarded by the expected old status, so
    # a request cancelled or edited elsewhere is never overwritten by a late result.

    def newest_queued_cv_request(self) -> int:
        """Highest id of a queued CV request (0 if none); cheap poll for the worker's fast path."""
        row = self.conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM cv_requests WHERE status='queued'"
        ).fetchone()
        return int(row[0])

    def claim_next_cv_request(self, now: datetime, max_attempts: int) -> sqlite3.Row | None:
        """Atomically take the oldest queued request (attempt counted); None if none."""
        return self.conn.execute(
            "UPDATE cv_requests SET status='in_progress', started_at=?1, attempts=attempts+1 "
            "WHERE id = (SELECT id FROM cv_requests WHERE status='queued' AND attempts < ?2 "
            "ORDER BY requested_at, id LIMIT 1) RETURNING *",
            (iso(now), max_attempts),
        ).fetchone()

    def claim_cv_request(
        self, request_id: int, now: datetime, max_attempts: int
    ) -> sqlite3.Row | None:
        """Like claim_next_cv_request, for one named request (CLI `cv run --request`)."""
        return self.conn.execute(
            "UPDATE cv_requests SET status='in_progress', started_at=?, attempts=attempts+1 "
            "WHERE id=? AND status='queued' AND attempts < ? RETURNING *",
            (iso(now), request_id, max_attempts),
        ).fetchone()

    def has_queued_cv_request(self, max_attempts: int) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM cv_requests WHERE status='queued' AND attempts < ? LIMIT 1",
                (max_attempts,),
            ).fetchone()
            is not None
        )

    def requeue_cv_request(self, request_id: int, *, feedback: str | None, refund: bool) -> bool:
        """in_progress -> queued (retry next cycle). `refund` gives the attempt back."""
        cur = self.conn.execute(
            "UPDATE cv_requests SET status='queued', started_at=NULL, feedback=?, "
            "attempts = attempts - ? WHERE id=? AND status='in_progress'",
            ((feedback or "")[:2000] or None, 1 if refund else 0, request_id),
        )
        return cur.rowcount == 1

    def finish_cv_request(
        self,
        request_id: int,
        *,
        status: str,
        now: datetime,
        kit_dir: str | None = None,
        review_reasons: list[str] | None = None,
        error: str | None = None,
        prompt_version: str | None = None,
        profile_sha: str | None = None,
        call_id: int | None = None,
        pdf_status: str | None = None,
        recommendation: str | None = None,
        fit_summary: str | None = None,
    ) -> bool:
        """in_progress -> done / needs_review / failed. False if cancelled meanwhile."""
        reasons = json.dumps(review_reasons) if review_reasons else None
        cur = self.conn.execute(
            "UPDATE cv_requests SET status=?, finished_at=?, kit_dir=?, review_reasons=?, "
            "error=?, prompt_version=?, profile_sha=?, last_call_id=COALESCE(?, last_call_id), "
            "pdf_status=?, recommendation=?, fit_summary=?, feedback=NULL "
            "WHERE id=? AND status='in_progress'",
            (
                status,
                iso(now),
                kit_dir,
                reasons[:2000] if reasons else None,
                error[:300] if error else None,
                prompt_version,
                profile_sha,
                call_id,
                pdf_status,
                recommendation,
                fit_summary[:600] if fit_summary else None,
                request_id,
            ),
        )
        return cur.rowcount == 1

    def cv_verdicts(self, canonical_ids: list[int]) -> dict[int, sqlite3.Row]:
        """canonical id -> the newest CV request of the group that carries an Opus verdict."""
        if not canonical_ids:
            return {}
        rows = self.conn.execute(
            "SELECT r.id, r.status, r.recommendation, r.fit_summary, j.canonical_id AS cid "
            "FROM cv_requests r JOIN jobs j ON j.id = r.canonical_id "
            f"WHERE r.recommendation IS NOT NULL AND j.canonical_id IN "
            f"({','.join('?' * len(canonical_ids))}) ORDER BY r.id",
            canonical_ids,
        ).fetchall()
        return {r["cid"]: r for r in rows}  # ORDER BY id: the later request wins

    def backfill_cv_verdict(self, request_id: int, recommendation: str, fit_summary: str) -> bool:
        """Fill an empty verdict only; never overwrites one the worker stored."""
        cur = self.conn.execute(
            "UPDATE cv_requests SET recommendation=?, fit_summary=? "
            "WHERE id=? AND recommendation IS NULL",
            (recommendation, fit_summary[:600], request_id),
        )
        return cur.rowcount == 1

    def recover_stale_cv_requests(
        self, started_before: datetime, max_attempts: int, now: datetime
    ) -> tuple[int, list[int]]:
        """Housekeeping before a CV run: (requeued, ids that became failed).

        in_progress for too long (worker died) -> queued, or failed if its attempts are used up;
        a queued request with no attempts left -> failed. Failed ones get `finished_at`."""
        failed: list[int] = []
        msg = "attempts used up"
        rows = self.conn.execute(
            "UPDATE cv_requests SET status='failed', finished_at=?, error=? "
            "WHERE status='in_progress' AND started_at < ? AND attempts >= ? RETURNING id",
            (iso(now), msg, iso(started_before), max_attempts),
        ).fetchall()
        failed += [r[0] for r in rows]
        cur = self.conn.execute(
            "UPDATE cv_requests SET status='queued', started_at=NULL "
            "WHERE status='in_progress' AND started_at < ?",
            (iso(started_before),),
        )
        requeued = cur.rowcount
        rows = self.conn.execute(
            "UPDATE cv_requests SET status='failed', finished_at=?, error=? "
            "WHERE status='queued' AND attempts >= ? RETURNING id",
            (iso(now), msg, max_attempts),
        ).fetchall()
        failed += [r[0] for r in rows]
        return requeued, failed

    def cv_request(self, request_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM cv_requests WHERE id=?", (request_id,)).fetchone()

    def recent_cv_requests(self, n: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT r.*, j.title, j.company FROM cv_requests r JOIN jobs j ON j.id = r.job_id "
            "ORDER BY r.id DESC LIMIT ?",
            (n,),
        ).fetchall()

    def mark_kit_edited(self, canonical_id: int, *, pdf_status: str, now: datetime) -> bool:
        """Owner edited resume.md (7b): remember when and whether the PDF matches."""
        cur = self.conn.execute(
            "UPDATE cv_requests SET edited_at=?, pdf_status=? WHERE id = (SELECT id FROM "
            "cv_requests WHERE kind='cv' AND canonical_id IN (SELECT id FROM jobs "
            "WHERE canonical_id = ?) AND status IN ('done','needs_review') "
            "ORDER BY id DESC LIMIT 1)",
            (iso(now), pdf_status, canonical_id),
        )
        return cur.rowcount == 1

    def approve_kit(self, canonical_id: int, *, now: datetime) -> bool:
        """needs_review -> done (7b). False if the latest request is in another status."""
        cur = self.conn.execute(
            "UPDATE cv_requests SET status='done', reviewed_at=? WHERE id = (SELECT id FROM "
            "cv_requests WHERE kind='cv' AND canonical_id IN (SELECT id FROM jobs "
            "WHERE canonical_id = ?) ORDER BY id DESC LIMIT 1) AND status='needs_review'",
            (iso(now), canonical_id),
        )
        return cur.rowcount == 1

    def _decision_stacks(self) -> dict[int, list[sqlite3.Row]]:
        """Per duplicate group: the decisions that still stand, oldest first. 'undo' takes the
        newest one off the stack (the owner's "undo" cancels the previous decision)."""
        stacks: dict[int, list[sqlite3.Row]] = {}
        for d in self.conn.execute(
            "SELECT d.*, j.canonical_id AS gid, j.title AS j_title, j.company AS j_company "
            "FROM decisions d JOIN jobs j ON j.id = d.job_id ORDER BY d.id"
        ):
            st = stacks.setdefault(d["gid"], [])
            if d["action"] == "undo":
                if st:
                    st.pop()
            else:
                st.append(d)
        return stacks

    @staticmethod
    def _standing_override(st: list[sqlite3.Row]) -> sqlite3.Row | None:
        """The `override_in` of a stack while only "Interested" / "Prepare CV" came after it;
        a later "No" ends it."""
        for d in reversed(st):
            if d["action"] == "override_in":
                return d
            if d["action"] not in ("interested", "cv_requested"):
                return None
        return None

    def owner_overrides(self) -> dict[int, sqlite3.Row]:
        """group id -> the owner's standing `override_in` decision (job returned to the Inbox)."""
        out: dict[int, sqlite3.Row] = {}
        for gid, st in self._decision_stacks().items():
            d = self._standing_override(st)
            if d is not None:
                out[gid] = d
        return out

    def owner_override_tops(self) -> set[int]:
        """Groups whose newest standing decision IS the `override_in` (so «undo» takes exactly it
        back); with "Interested" on top, undo only takes that mark off."""
        return {
            gid
            for gid, st in self._decision_stacks().items()
            if st and st[-1]["action"] == "override_in"
        }

    def owner_override_note(self, job_id: int) -> str | None:
        """The owner's explanation if he returned this job's group to the Inbox, else None."""
        row = self.conn.execute("SELECT canonical_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
        d = self.owner_overrides().get(row["canonical_id"]) if row else None
        return d["note"] if d else None

    def owner_corrections(
        self, prompt_version: str, limit: int, max_withdrawn: int | None = None
    ) -> list[dict]:
        """The owner's newest standing corrections of the AI, both ways, newest first:
        `override_in` (AI said no, owner says yes) and a "No" with a note on a job Haiku had
        passed (AI said yes, owner says no). Plain dicts; the text is the owner's, not a posting's.
        """
        stacks = self._decision_stacks()
        withdrawn_now = {
            r[0]
            for r in self.conn.execute(
                "SELECT canonical_id FROM applications WHERE status = 'withdrawn'"
            )
        }
        cands = []
        for gid, st in stacks.items():
            if not st:
                continue
            d = self._standing_override(st)  # only what the owner's newest word still says
            top = st[-1]
            if d is None and top["action"] == "rejected":
                if top["withdrawn"]:
                    # "Withdrawn" counts only while the application still says so
                    if gid in withdrawn_now:
                        d = top
                elif top["note"]:
                    d = top
            if d is not None:
                cands.append((d["id"], gid, d))
        cands.sort(key=lambda c: -c[0])
        verdicts = {
            sg.canonical_id: sg.verdict
            for sg in self.llm_verdicts(prompt_version, sorted({c[1] for c in cands}) or [-1])
        }
        out: list[dict] = []
        n_withdrawn = 0
        for _, gid, d in cands:
            v = verdicts.get(gid)
            if d["action"] == "rejected" and (v is None or not v.eligible):
                continue  # the AI agreed (or never judged): nothing to learn from
            if d["withdrawn"]:
                if max_withdrawn is not None and n_withdrawn >= max_withdrawn:
                    continue  # a burst of "Withdrawn" must not push out the ordinary ones
                n_withdrawn += 1
            out.append(
                {
                    "id": d["id"],
                    "title": d["j_title"],
                    "company": d["j_company"],
                    "action": d["action"],
                    "reason": d["reason"],
                    "note": d["note"],
                    "withdrawn": bool(d["withdrawn"]),
                    "ai_eligible": v.eligible if v else None,
                    "ai_fit": v.fit if v else None,
                    "at": d["at"],
                }
            )
            if len(out) >= limit:
                break
        return out

    def latest_decisions(self, job_ids: list[int]) -> dict[int, sqlite3.Row]:
        """Newest decision row per job id, looked up for the job's whole duplicate group (the
        deciding job may have been another member). An 'undo' row means: no decision."""
        out: dict[int, sqlite3.Row] = {}
        for jid in job_ids:
            row = self.conn.execute(
                "SELECT d.* FROM decisions d WHERE d.canonical_id = "
                "(SELECT canonical_id FROM jobs WHERE id = ?1) OR d.job_id IN "
                "(SELECT id FROM jobs WHERE canonical_id = (SELECT canonical_id FROM jobs "
                "WHERE id = ?1)) ORDER BY d.at DESC, d.id DESC LIMIT 1",
                (jid,),
            ).fetchone()
            if row is not None:
                out[jid] = row
        return out

    # --- web panel (issue #6) ---

    def set_source_enabled(self, name: str, enabled: bool) -> None:
        cur = self.conn.execute(
            "UPDATE sources SET enabled = ? WHERE name = ?", (int(enabled), name)
        )
        if cur.rowcount == 0:
            raise KeyError(name)

    def set_interval_override(self, name: str, seconds: int | None) -> None:
        cur = self.conn.execute(
            "UPDATE sources SET interval_override_s = ? WHERE name = ?", (seconds, name)
        )
        if cur.rowcount == 0:
            raise KeyError(name)

    def canonical_of(self, job_id: int) -> int | None:
        row = self.conn.execute("SELECT canonical_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row[0] if row else None

    def group_jobs(self, canonical_id: int) -> list[sqlite3.Row]:
        """Every member of a duplicate group with all columns except the raw payload."""
        return self.conn.execute(
            "SELECT id, canonical_id, source, url, apply_url, company, title, description_html, "
            "description_text, location_text, remote, employment_type, tags_json, salary_text, "
            "posted_at, first_seen_at, last_seen_at, raw_json FROM jobs WHERE canonical_id = ? "
            "ORDER BY id",
            (canonical_id,),
        ).fetchall()

    def latest_cv_request(self, canonical_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM cv_requests WHERE kind='cv' AND canonical_id IN "
            "(SELECT id FROM jobs WHERE canonical_id = ?) ORDER BY id DESC LIMIT 1",
            (canonical_id,),
        ).fetchone()

    def latest_cover_request(self, canonical_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM cv_requests WHERE kind='cover' AND canonical_id IN "
            "(SELECT id FROM jobs WHERE canonical_id = ?) ORDER BY id DESC LIMIT 1",
            (canonical_id,),
        ).fetchone()

    def request_cover(self, canonical_id: int, *, now: datetime) -> int | None:
        """Queue a cover-letter-only request next to the group's finished CV request. None when
        there is no finished kit, or any request of the group is already queued or running."""
        with self.transaction():
            cv = self.latest_cv_request(canonical_id)
            if cv is None or cv["status"] not in ("done", "needs_review"):
                return None
            busy = self.conn.execute(
                "SELECT 1 FROM cv_requests WHERE status IN ('queued','in_progress') "
                "AND canonical_id = ?",
                (cv["canonical_id"],),
            ).fetchone()
            if busy is not None:
                return None
            cur = self.conn.execute(
                "INSERT INTO cv_requests (canonical_id, job_id, decision_id, status, "
                "requested_at, kind, kit_dir) VALUES (?, ?, ?, 'queued', ?, 'cover', ?)",
                (cv["canonical_id"], cv["job_id"], cv["decision_id"], iso(now), cv["kit_dir"]),
            )
            return cur.lastrowid

    def approve_cover(self, canonical_id: int, *, now: datetime) -> bool:
        """The owner checked the letter: needs_review -> done for the group's latest letter."""
        cur = self.conn.execute(
            "UPDATE cv_requests SET status='done', reviewed_at=? WHERE id = (SELECT id FROM "
            "cv_requests WHERE kind='cover' AND canonical_id IN (SELECT id FROM jobs "
            "WHERE canonical_id = ?) ORDER BY id DESC LIMIT 1) AND status='needs_review'",
            (iso(now), canonical_id),
        )
        return cur.rowcount == 1

    def finish_cover_request(
        self,
        request_id: int,
        *,
        status: str,
        now: datetime,
        review_reasons: list[str] | None = None,
        error: str | None = None,
        prompt_version: str | None = None,
        profile_sha: str | None = None,
        call_id: int | None = None,
    ) -> bool:
        """in_progress -> done / needs_review / failed for a cover request."""
        reasons = json.dumps(review_reasons) if review_reasons else None
        cur = self.conn.execute(
            "UPDATE cv_requests SET status=?, finished_at=?, review_reasons=?, error=?, "
            "prompt_version=?, profile_sha=?, last_call_id=COALESCE(?, last_call_id), "
            "feedback=NULL WHERE id=? AND status='in_progress' AND kind='cover'",
            (
                status,
                iso(now),
                reasons[:2000] if reasons else None,
                error[:300] if error else None,
                prompt_version,
                profile_sha,
                call_id,
                request_id,
            ),
        )
        return cur.rowcount == 1

    def application(self, canonical_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM applications WHERE canonical_id IN "
            "(SELECT id FROM jobs WHERE canonical_id = ?) ORDER BY updated_at DESC LIMIT 1",
            (canonical_id,),
        ).fetchone()

    def application_events(self, canonical_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM application_events WHERE canonical_id IN "
            "(SELECT id FROM jobs WHERE canonical_id = ?) ORDER BY at, id",
            (canonical_id,),
        ).fetchall()

    def set_application_status(
        self, canonical_id: int, status: str, note: str | None, now: datetime
    ) -> None:
        """Upsert the owner's status and append an event, in one transaction."""
        if status not in APPLICATION_STATUSES:
            raise ValueError(f"bad status {status!r}")
        note = (note or "").strip()[:2000] or None
        if self.canonical_of(canonical_id) != canonical_id:
            raise ValueError(f"no job group {canonical_id}")
        with self.transaction():
            self.conn.execute(
                "INSERT INTO applications (canonical_id, status, note, updated_at) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(canonical_id) DO UPDATE SET status = excluded.status, "
                "note = excluded.note, updated_at = excluded.updated_at",
                (canonical_id, status, note, iso(now)),
            )
            self.conn.execute(
                "INSERT INTO application_events (canonical_id, status, note, at) VALUES (?,?,?,?)",
                (canonical_id, status, note, iso(now)),
            )

    def set_application_note(self, canonical_id: int, note: str | None, now: datetime) -> None:
        """Save the owner's note without choosing a status. A new row gets the placeholder
        'preparing', which the CV request overrides (see `applications_view`)."""
        note = (note or "").strip()[:2000] or None
        if self.canonical_of(canonical_id) != canonical_id:
            raise ValueError(f"no job group {canonical_id}")
        with self.transaction():
            self.conn.execute(
                "INSERT INTO applications (canonical_id, status, note, updated_at) "
                "VALUES (?, 'preparing', ?, ?) ON CONFLICT(canonical_id) DO UPDATE SET "
                "note = excluded.note, updated_at = excluded.updated_at",
                (canonical_id, note, iso(now)),
            )

    def withdraw_application(
        self, canonical_id: int, reason: str, note: str | None, now: datetime
    ) -> None:
        """Withdraw in one transaction: status -> 'withdrawn' (the owner's card note is kept),
        an event «reason: text», a 'rejected' decision with the same reason/text (flagged
        `withdrawn`, so it feeds OWNER CORRECTIONS), and a still-queued CV request cancelled (one
        in progress is left alone). ValueError: not an application, or already withdrawn."""
        note = (note or "").strip()[:DECISION_NOTE_MAX] or None
        if self.canonical_of(canonical_id) != canonical_id:
            raise ValueError(f"no job group {canonical_id}")
        with self.transaction():
            app = self.application(canonical_id)
            if app is None:  # a card without a row needs a live (not cancelled) CV request
                cv = self.latest_cv_request(canonical_id)
                if cv is None or cv["status"] == "cancelled":
                    raise ValueError("no application")
            if app is not None and app["status"] == WITHDRAWN:
                raise ValueError("already withdrawn")
            self.conn.execute(
                "INSERT INTO applications (canonical_id, status, note, updated_at) "
                "VALUES (?, 'withdrawn', NULL, ?) ON CONFLICT(canonical_id) DO UPDATE SET "
                "status = 'withdrawn', updated_at = excluded.updated_at",
                (canonical_id, iso(now)),
            )
            self.conn.execute(
                "INSERT INTO application_events (canonical_id, status, note, at) VALUES (?,?,?,?)",
                (canonical_id, WITHDRAWN, reason + (f": {note}" if note else ""), iso(now)),
            )
            self._insert_decision(
                canonical_id,
                "rejected",
                channel="panel",
                reason=reason,
                note=note,
                alert_id=None,
                tg_update_id=None,
                now=now,
                withdrawn=True,
            )
            self.conn.execute(
                "UPDATE cv_requests SET status='cancelled', finished_at=? "
                "WHERE status='queued' AND canonical_id IN "
                "(SELECT id FROM jobs WHERE canonical_id = ?)",
                (iso(now), canonical_id),
            )

    def restore_application(self, canonical_id: int, now: datetime) -> str:
        """Undo withdrawal: the status before "Withdrawn" (from the events; 'preparing' if there was
        none, i.e. the card was only derived from a CV request) comes back. The decision stack is
        touched only when "Withdrawn" is its newest word: then an 'undo' takes exactly it off (so it
        leaves OWNER CORRECTIONS); under another decision no 'undo' is written, the status alone
        ends the correction. A CV request that this "Withdrawn" cancelled is queued again.
        Returns the new status."""
        if self.canonical_of(canonical_id) != canonical_id:
            raise ValueError(f"no job group {canonical_id}")
        with self.transaction():
            app = self.application(canonical_id)
            if app is None or app["status"] != WITHDRAWN:
                raise ValueError("not withdrawn")
            prev = "preparing"
            seen = False
            withdrawn_at = None
            for e in reversed(self.application_events(canonical_id)):
                if not seen:
                    seen = e["status"] == WITHDRAWN
                    withdrawn_at = e["at"] if seen else None
                elif e["status"] != WITHDRAWN and e["status"] in APPLICATION_STATUSES:
                    prev = e["status"]
                    break
            self.conn.execute(
                "UPDATE applications SET status = ?, updated_at = ? WHERE canonical_id = ?",
                (prev, iso(now), canonical_id),
            )
            self.conn.execute(
                "INSERT INTO application_events (canonical_id, status, note, at) VALUES (?,?,?,?)",
                (canonical_id, prev, 'returned from "Withdrawn"', iso(now)),
            )
            if withdrawn_at is not None:
                # re-queue exactly the request this "Withdrawn" cancelled (same timestamp)
                latest = self.latest_cv_request(canonical_id)
                if (
                    latest is not None
                    and latest["status"] == "cancelled"
                    and latest["finished_at"] == withdrawn_at
                ):
                    self.conn.execute(
                        "UPDATE cv_requests SET status='queued', finished_at=NULL WHERE id = ?",
                        (latest["id"],),
                    )
            top = (self._decision_stacks().get(canonical_id) or [None])[-1]
            if top is not None and top["action"] == "rejected" and top["withdrawn"]:
                self._insert_decision(
                    canonical_id,
                    "undo",
                    channel="panel",
                    reason=None,
                    note=None,
                    alert_id=None,
                    tg_update_id=None,
                    now=now,
                )
        return prev

    def is_withdrawn_on_top(self, canonical_id: int) -> bool:
        """The group's newest standing decision is the one written by "Withdrawn"."""
        top = (self._decision_stacks().get(canonical_id) or [None])[-1]
        app = self.application(canonical_id)
        return bool(
            top is not None
            and top["action"] == "rejected"
            and top["withdrawn"]
            and app is not None
            and app["status"] == WITHDRAWN
        )

    def applications_view(self) -> list[ApplicationRow]:
        """Groups with an application row or a (non-cancelled) CV request, newest activity first."""
        apps = {
            r["cid"]: r
            for r in self.conn.execute(
                "SELECT a.*, j.canonical_id AS cid FROM applications a "
                "JOIN jobs j ON j.id = a.canonical_id ORDER BY a.updated_at"
            )
        }
        cvs = {
            r["cid"]: r
            for r in self.conn.execute(
                "SELECT r.*, j.canonical_id AS cid FROM cv_requests r "
                "JOIN jobs j ON j.id = r.canonical_id WHERE r.kind='cv' ORDER BY r.id"
            )
        }
        with_events = {
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT j.canonical_id FROM application_events e "
                "JOIN jobs j ON j.id = e.canonical_id"
            )
        }
        applied: dict[int, str] = {}
        for e in self.conn.execute(
            "SELECT e.at, j.canonical_id AS cid FROM application_events e "
            "JOIN jobs j ON j.id = e.canonical_id WHERE e.status = 'applied' ORDER BY e.at"
        ):
            applied.setdefault(e["cid"], e["at"])
        withdraw_notes: dict[int, str] = {}
        for e in self.conn.execute(
            "SELECT e.note, j.canonical_id AS cid FROM application_events e "
            "JOIN jobs j ON j.id = e.canonical_id WHERE e.status = 'withdrawn' ORDER BY e.at, e.id"
        ):
            withdraw_notes[e["cid"]] = e["note"] or ""
        out: list[ApplicationRow] = []
        for cid in set(apps) | set(cvs):
            a, cv = apps.get(cid), cvs.get(cid)
            cv_status = cv["status"] if cv is not None and cv["status"] != "cancelled" else None
            manual = a["status"] if a is not None else None
            if manual == "preparing" and cid not in with_events:
                manual = None  # the note-only placeholder, not a choice: status "auto"
            derived_status = None
            if cv_status is not None:
                derived_status = {
                    "queued": "preparing",
                    "in_progress": "preparing",
                    "done": "ready",
                    "failed": "cv_failed",
                    "needs_review": "needs_review",
                }.get(cv_status, cv_status)  # an unknown CV status is shown, not dropped
            # the CV request wins over the owner's early-stage statuses; later ones win over it
            if derived_status is not None and manual in (None, "preparing", "ready"):
                status, derived = derived_status, True
            elif manual is not None:
                status, derived = manual, False
            else:
                continue  # no live CV request and no chosen status: not an application column
            job = self.conn.execute(
                "SELECT title, company FROM jobs WHERE id = ?", (cid,)
            ).fetchone()
            if job is None:
                continue
            stamps = [a["updated_at"]] if a is not None else []
            if cv is not None:
                stamps.append(cv["requested_at"])
            activity = max(stamps, default=None)
            out.append(
                ApplicationRow(
                    canonical_id=cid,
                    title=job["title"],
                    company=job["company"],
                    status=status,
                    derived=derived,
                    manual_status=manual,
                    note=a["note"] if a else None,
                    updated_at=_dt(activity),
                    cv_status=cv["status"] if cv else None,
                    cv_requested_at=_dt(cv["requested_at"]) if cv else None,
                    cv_finished_at=_dt(cv["finished_at"]) if cv else None,
                    cv_error=cv["error"] if cv else None,
                    applied_at=_dt(applied.get(cid)),
                    withdraw_note=withdraw_notes.get(cid) if status == WITHDRAWN else None,
                )
            )
        out.sort(key=lambda r: r.updated_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return out
