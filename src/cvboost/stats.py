"""Read-only numbers for the "Statistics" page. Plain SELECTs over existing tables.

Cohort: the funnel and the per-source table count canonical groups whose first job was first
seen inside the period. A group is attributed to the source of its canonical (first) job.
Rules and Haiku verdicts are the latest ones for the current rule / prompt version.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cvboost.storage.db import Store, group_reasons, iso, rule_outcome

PERIODS = {
    "24h": ("24 h", timedelta(hours=24)),
    "7d": ("7 days", timedelta(days=7)),
    "all": ("all", None),
}


@dataclass
class SourceStat:
    source: str
    collected: int = 0  # jobs incl. duplicates
    unique: int = 0
    passed: int = 0
    eligible: int = 0
    fit: int = 0
    sent: int = 0

    @property
    def useful_pct(self) -> int | None:
        return round(100 * self.fit / self.unique) if self.unique else None


@dataclass
class Queue:
    waiting: int
    batches: int
    cycles: int
    eta_minutes: int
    batch_size: int
    per_cycle: int
    tick_minutes: int


@dataclass
class Stats:
    period: str
    funnel: dict[str, int] = field(default_factory=dict)
    decisions: dict[str, int] = field(default_factory=dict)
    sources: list[SourceStat] = field(default_factory=list)
    reject_reasons: list[tuple[str, int]] = field(default_factory=list)
    rejected_groups: int = 0
    no_reasons: list[tuple[str | None, int]] = field(default_factory=list)
    applications: list[tuple[str, int]] = field(default_factory=list)
    queue: Queue | None = None
    corrections: list[dict] = field(default_factory=list)  # the owner's newest 10
    corrections_in: int = 0  # all standing: AI said no, owner says yes
    corrections_out: int = 0  # all standing: AI said yes, owner says no (with a note)


def compute(
    store: Store,
    *,
    period: str,
    now: datetime,
    rule_version: str,
    relevance_threshold: float,
    prompt_version: str,
    fit_threshold: int,
    batch_size: int,
    per_cycle: int,
    tick_minutes: int,
    max_age_days: int,
) -> Stats:
    span = PERIODS[period][1]
    since = iso(now - span) if span else None
    conn = store.conn
    out = Stats(period=period)

    jobs = conn.execute(
        "SELECT id, canonical_id, source, first_seen_at, posted_at FROM jobs"
    ).fetchall()
    verdicts = store.latest_verdicts(rule_version)

    # per canonical group: reject codes, best relevance, rules seen
    pairs: dict[int, list[tuple[list[str], list[str]]]] = {}
    best: dict[int, int] = {}
    seen: set[int] = set()
    for j in jobs:
        v = verdicts.get(j["id"])
        if v is None:
            continue
        cid = j["canonical_id"]
        seen.add(cid)
        pairs.setdefault(cid, []).append(
            (json.loads(v["reasons_json"]), json.loads(v["flags_json"]))
        )
        best[cid] = max(best.get(cid, 0), v["relevance"] or 0)
    outcome = {c: rule_outcome(group_reasons(pairs[c]), best[c], relevance_threshold) for c in seen}

    llm = {
        r["cid"]: (r["eligible"], r["fit"])
        for r in conn.execute(
            "SELECT cid, eligible, fit FROM (SELECT j.canonical_id AS cid, "
            "json_extract(v.details_json, '$.verdict.eligible') AS eligible, "
            "json_extract(v.details_json, '$.verdict.fit') AS fit, "
            "ROW_NUMBER() OVER (PARTITION BY j.canonical_id ORDER BY v.at DESC, v.id DESC) AS rn "
            "FROM verdicts v JOIN jobs j ON j.id = v.job_id "
            "WHERE v.stage = 'llm' AND v.rule_version = ?) WHERE rn = 1",
            (prompt_version,),
        )
    }
    sent = {
        r[0]
        for r in conn.execute(
            "SELECT canonical_id FROM alerts WHERE status IN ('sent', 'summarized')"
        )
    }
    latest_dec = {
        r["cid"]: r["action"]
        for r in conn.execute(
            "SELECT cid, action FROM (SELECT canonical_id AS cid, action, "
            "ROW_NUMBER() OVER (PARTITION BY canonical_id ORDER BY at DESC, id DESC) AS rn "
            "FROM decisions) WHERE rn = 1"
        )
    }

    per_src: dict[str, SourceStat] = {}
    funnel = Counter()
    dec = Counter()
    rej = Counter()
    for j in jobs:
        if since and j["first_seen_at"] < since:
            continue
        st = per_src.setdefault(j["source"], SourceStat(j["source"]))
        st.collected += 1
        funnel["collected"] += 1
        if j["id"] != j["canonical_id"]:
            continue
        cid = j["id"]
        st.unique += 1
        funnel["unique"] += 1
        if cid not in seen:
            continue
        if outcome[cid]:
            funnel["rejected"] += 1
            for c in outcome[cid]:
                rej[c] += 1
            continue
        st.passed += 1
        funnel["passed"] += 1
        verdict = llm.get(cid)
        if verdict is not None:
            funnel["scored"] += 1
            if verdict[0]:
                st.eligible += 1
                funnel["eligible"] += 1
                if (verdict[1] or 0) >= fit_threshold:
                    st.fit += 1
                    funnel["fit"] += 1
        if cid in sent:
            st.sent += 1
            funnel["sent"] += 1
        action = latest_dec.get(cid)
        if action and action != "undo":
            dec[action] += 1
    out.funnel = dict(funnel)
    out.decisions = dict(dec)
    out.rejected_groups = funnel["rejected"]
    out.reject_reasons = rej.most_common(10)
    out.sources = sorted(per_src.values(), key=lambda s: (-s.passed, -s.unique, s.source))

    # owner's "No" reasons: latest decision of a group is «rejected», decided in the period
    args: list = []
    cond = ""
    if since:
        cond = "AND at >= ? "
        args.append(since)
    out.no_reasons = [
        (r["reason"], r["n"])
        for r in conn.execute(
            "SELECT reason, COUNT(*) AS n FROM (SELECT reason, action, at, "
            "ROW_NUMBER() OVER (PARTITION BY canonical_id ORDER BY at DESC, id DESC) AS rn "
            f"FROM decisions) WHERE rn = 1 AND action = 'rejected' {cond}"
            "GROUP BY reason ORDER BY n DESC, reason",
            args,
        )
    ]
    where = "WHERE updated_at >= ?" if since else ""
    out.applications = [
        (r["status"], r["n"])
        for r in conn.execute(
            f"SELECT status, COUNT(*) AS n FROM applications {where} GROUP BY status",
            [since] if since else [],
        )
    ]

    # the owner's corrections of the AI (not period-bound; the PM turns repeats into rules)
    every = store.owner_corrections(prompt_version, 10**9)
    out.corrections = every[:10]
    out.corrections_in = sum(1 for c in every if c["action"] == "override_in")
    out.corrections_out = len(every) - out.corrections_in

    # Haiku queue (not period-bound): kept, fresh groups with no verdict and no recorded error
    cutoff = iso(now - timedelta(days=max_age_days))
    errored = {
        r[0]
        for r in conn.execute(
            "SELECT j.canonical_id FROM llm_item_errors e JOIN jobs j ON j.id = e.job_id "
            "WHERE e.prompt_version = ?",
            (prompt_version,),
        )
    }
    waiting = 0
    for j in jobs:
        cid = j["canonical_id"]
        if j["id"] != cid or cid not in seen or outcome[cid] or cid in llm or cid in errored:
            continue
        if (j["posted_at"] or j["first_seen_at"]) >= cutoff:
            waiting += 1
    batches = math.ceil(waiting / batch_size) if waiting else 0
    cycles = math.ceil(batches / per_cycle) if batches else 0
    out.queue = Queue(
        waiting, batches, cycles, cycles * tick_minutes, batch_size, per_cycle, tick_minutes
    )
    return out
