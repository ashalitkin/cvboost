"""Why: one explanation per job group, shared by Inbox, Applications and the job page.

Three layers, each from data already stored: the rules verdict (reasons + evidence), Haiku's
verdict (eligible, fit, summary_ru) and Opus's recommendation on the CV request. Pure
composition over the Store; every string is shown through Jinja autoescape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from cvboost.core.rules import RulesConfig
from cvboost.llm.schema import JobVerdict
from cvboost.storage.db import Store, group_reasons, rule_outcome

REASON_LABELS = {
    "us_only": "US only",
    "region_restricted": "Region not eligible",
    "w2_only": "W2 only",
    "onsite": "Office / hybrid",
    "rate_below_min": "Rate below minimum",
    "region_eastern_europe": "Eastern Europe only",
    "region_excluded": "Asia / Australia / Saudi Arabia",
    "latam_only": "Hiring in Latin America only",
    "region_uk": "UK only, no contractor / outside IR35",
    "unwanted_role": "Unwanted role",
    "unwanted_stack": "Spark / streaming in the requirements",
    "language": "Job language",
    "low_relevance": "Low relevance",
    "blocked_company": "Blocked company",
}


def reason_labels(rules: RulesConfig | None = None) -> dict[str, str]:
    """REASON_LABELS with the rate minimum and the home country of rules.yaml."""
    labels = dict(REASON_LABELS)
    if rules is None:
        return labels
    labels["rate_below_min"] = f"Rate below ${rules.rate.min_hourly_usd:g}/h"
    home = rules.location.home
    if home:
        labels[f"{home.code}_no_rate"] = f"{home.label}, no rate stated"
    return labels


REC_LABELS = {
    "apply": "Apply",
    "apply_with_caveats": "Apply with caveats",
    "skip": "Skip",
}
REC_CLASS = {"apply": "rec-apply", "apply_with_caveats": "rec-caveats", "skip": "rec-skip"}
EVIDENCE_MAX = 120
PERIOD_LABELS = {"hour": "h", "day": "day", "month": "mo", "year": "yr"}


@dataclass(frozen=True)
class RuleReason:
    code: str
    label: str
    evidence: str


@dataclass(frozen=True)
class Why:
    rules_seen: bool = False  # False: no rules verdict stored yet
    rules_ok: bool = True
    relevance: int | None = None
    reasons: list[RuleReason] = field(default_factory=list)
    haiku: JobVerdict | None = None
    rec: str | None = None  # apply | apply_with_caveats | skip
    rec_summary: str | None = None

    @property
    def rec_label(self) -> str | None:
        return REC_LABELS.get(self.rec) if self.rec else None

    @property
    def rec_class(self) -> str:
        return REC_CLASS.get(self.rec or "", "")

    @property
    def empty(self) -> bool:
        return not self.rules_seen and self.haiku is None and self.rec is None


def short_evidence(code: str, text: str | None) -> str:
    if not text:
        return ""
    if code == "rate_below_min":
        try:
            d = json.loads(text)
            unit = PERIOD_LABELS.get(d.get("period"), d.get("period") or "?")
            return f"{float(d['max']):g} {d.get('currency') or ''}/{unit}".replace("  ", " ")
        except (ValueError, KeyError, TypeError):
            pass
    text = " ".join(str(text).split())
    return text if len(text) <= EVIDENCE_MAX else text[: EVIDENCE_MAX - 1] + "…"


def why_for(
    store: Store,
    canonical_ids: list[int],
    *,
    rule_version: str,
    threshold: float,
    prompt_version: str,
    labels: dict[str, str] | None = None,
) -> dict[int, Why]:
    """canonical id -> Why. Groups with nothing stored yet get an empty Why."""
    if not canonical_ids:
        return {}
    labels = labels or REASON_LABELS
    marks = ",".join("?" * len(canonical_ids))
    members: dict[int, list[int]] = {}
    for r in store.conn.execute(
        f"SELECT id, canonical_id FROM jobs WHERE canonical_id IN ({marks})", canonical_ids
    ):
        members.setdefault(r["canonical_id"], []).append(r["id"])
    verdicts = store.latest_verdicts(rule_version, [i for ids in members.values() for i in ids])
    haiku = {
        sg.canonical_id: sg.verdict for sg in store.llm_verdicts(prompt_version, canonical_ids)
    }
    cvs = store.cv_verdicts(canonical_ids)
    out: dict[int, Why] = {}
    for cid in canonical_ids:
        pairs: list[tuple[list[str], list[str]]] = []
        evidence: dict[str, str] = {}
        best: int | None = None
        seen = False
        for jid in members.get(cid, []):
            v = verdicts.get(jid)
            if v is None:
                continue
            seen = True
            pairs.append((json.loads(v["reasons_json"]), json.loads(v["flags_json"])))
            for k, t in (json.loads(v["details_json"]).get("evidence") or {}).items():
                evidence.setdefault(k, str(t))
            best = max(best, v["relevance"] or 0) if best is not None else (v["relevance"] or 0)
        shown = rule_outcome(group_reasons(pairs), best, threshold)
        cv = cvs.get(cid)
        out[cid] = Why(
            rules_seen=seen,
            rules_ok=not shown,
            relevance=best,
            reasons=[
                RuleReason(c, labels.get(c, c), short_evidence(c, evidence.get(c))) for c in shown
            ],
            haiku=haiku.get(cid),
            rec=cv["recommendation"] if cv else None,
            rec_summary=cv["fit_summary"] if cv else None,
        )
    return out
