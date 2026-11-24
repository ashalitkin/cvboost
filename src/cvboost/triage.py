"""Deterministic triage step: dedupe, then hard rules + relevance, verdict per job row."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from cvboost.core.dedupe import assign_canonical
from cvboost.core.rules import RuleInput, RulesConfig, compile_rules, evaluate, rule_version
from cvboost.storage.db import Store


@dataclass
class TriageStats:
    rule_version: str = ""
    groups: int = 0
    merged: int = 0
    evaluated: int = 0
    new: int = 0
    changed: int = 0
    keep: int = 0
    reject: int = 0
    flag_counts: Counter = field(default_factory=Counter)
    main_reasons: Counter = field(default_factory=Counter)
    kept_groups: int = 0


def triage(store: Store, cfg: RulesConfig, now: datetime) -> TriageStats:
    version = rule_version(cfg)
    compiled = compile_rules(cfg)
    stats = TriageStats(rule_version=version)
    with store.transaction():
        d = assign_canonical(store.conn, cfg.dedupe.window_days)
        stats.groups, stats.merged = d.groups, d.merged
        for row in store.jobs_for_triage(version):
            inp = RuleInput.from_row(row)
            h = inp.input_hash()
            had_before = row["last_verdict_at"] is not None
            if had_before:
                latest = store.latest_verdict(row["id"], version)
                if latest is not None and latest["input_hash"] == h:
                    continue
            res = evaluate(inp, compiled, first_seen_at=row["first_seen_at"])
            store.insert_verdict(
                job_id=row["id"],
                stage="rules",
                decision=res.decision,
                reasons=res.reasons,
                flags=res.flags,
                relevance=res.relevance,
                profile=res.profile,
                details=res.details,
                rule_version=version,
                input_hash=h,
                now=now,
            )
            stats.evaluated += 1
            if had_before:
                stats.changed += 1
            else:
                stats.new += 1
    # summary over the latest verdict of every job for the current version
    for row in store.latest_verdicts(version).values():
        if row["decision"] == "keep":
            stats.keep += 1
        else:
            stats.reject += 1
            reasons = json.loads(row["reasons_json"])
            if reasons:
                stats.main_reasons[reasons[0]] += 1
        for flag in json.loads(row["flags_json"]):
            stats.flag_counts[flag] += 1
    stats.kept_groups = len(store.kept_canonicals(version, cfg.relevance.threshold))
    return stats
