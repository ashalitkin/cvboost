"""What the owner sees in the panel Inbox: pure functions over the Store.

Composition of existing data: rule-kept groups (#3), LLM verdicts (#4), the owner's latest
decision (#5) and per-member freshness. No table of its own.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from cvboost import connectors
from cvboost.core.freshness import closed_reason
from cvboost.explain import Why, why_for
from cvboost.llm.schema import JobVerdict
from cvboost.storage.db import KeptGroup, Store, rate_display

EligibleFilter = Literal["default", "yes", "no", "unscored", "any"]
DecisionFilter = Literal["default", "none", "interested", "all"]
ELIGIBLE_VALUES = ("default", "yes", "no", "unscored", "any", "rejected")
ELIGIBLE_LABELS = {"rejected": "Screened out (rules or Haiku)"}
DECISION_VALUES = ("default", "none", "interested", "all")
TYPE_VALUES = ("full_time", "contract", "part_time", "freelance", "unknown")
TYPE_LABELS = {
    "full_time": "Full-time",
    "contract": "Contract",
    "part_time": "Part-time",
    "freelance": "Freelance",
    "unknown": "Not stated",
}
SORT_VALUES = ("fit", "new", "posted", "rate")
SORT_LABELS = {
    "fit": "by fit",
    "new": "new first",
    "posted": "by posted date",
    "rate": "by rate",
}
_TYPE_ALIASES = {
    "full_time": "full_time",
    "full-time": "full_time",
    "fulltime": "full_time",
    "contract": "contract",
    "part_time": "part_time",
    "part-time": "part_time",
    "parttime": "part_time",
    "fractional": "part_time",
    "freelance": "freelance",
}
_HOURS = {"hour": 1.0, "day": 8.0, "month": 160.0, "year": 1920.0}


def normalise_type(value: str | None) -> str | None:
    """A source's or Haiku's employment word -> one of TYPE_VALUES (None: says nothing)."""
    return _TYPE_ALIASES.get((value or "").strip().lower().replace(" ", "_"))


def employment_of(alive, verdict: JobVerdict | None) -> str:
    """The job's own employment_type (first live member that has a known one), else Haiku's."""
    for m in alive:
        t = normalise_type(m["employment_type"])
        if t:
            return t
    return (normalise_type(verdict.employment) if verdict else None) or "unknown"


def hourly_rate(alive) -> float | None:
    """Best stated rate as a rough per-hour number, for sorting only (currencies are not mixed
    up in a meaningful way; the owner reads the real text on the card)."""
    best: float | None = None
    for m in alive:
        amount = m["salary_max"] or m["salary_min"]
        if not amount:
            continue
        per = _HOURS.get(m["salary_period"] or "")
        if per is None:
            per = 1920.0 if amount > 1000 else 1.0
        value = amount / per
        best = value if best is None else max(best, value)
    return best


@dataclass(frozen=True)
class InboxFilters:
    profile: str | None = None
    fit_min: int | None = None
    rate_unknown: bool | None = None
    source: str | None = None
    since_days: int | None = None
    eligible: str = "default"
    decision: str = "default"
    types: tuple[str, ...] = ()  # empty = any type
    unseen_only: bool = False
    sort: str = "fit"


@dataclass(frozen=True)
class Sighting:
    source: str
    url: str
    attribution: str  # "via Remote OK": required by the source's ToS


@dataclass(frozen=True)
class InboxItem:
    group: KeptGroup
    job_id: int  # the representative: first live member
    title: str
    company: str
    llm: JobVerdict | None
    decision: str | None  # interested | rejected | cv_requested | None (also after 'undo')
    sightings: list[Sighting]
    fit: int | None
    why: Why = Why()
    override_note: str | None = None  # the owner returned this job from "Screened out by AI"
    override_top: bool = False  # ...and that is still his newest decision
    emp_type: str = "unknown"  # one of TYPE_VALUES
    unseen: bool = False
    first_seen: datetime | None = None  # when the group first appeared (sort "new first")
    rate_value: float | None = None

    @property
    def canonical_id(self) -> int:
        return self.group.canonical_id


def attribution_for(source: str) -> str:
    cls = connectors.REGISTRY.get(source)
    if cls is None and source.startswith("email:"):
        return f"via {source[6:].capitalize()} e-mail alert"
    return cls.attribution if cls else f"via {source}"


def closed_members(rows, now: datetime, max_age_days: int) -> dict[int, str | None]:
    """job id -> closed reason (None = alive) for rows with source/posted_at/first_seen_at/raw."""
    out: dict[int, str | None] = {}
    for m in rows:
        try:
            raw = json.loads(m["raw_json"] or "{}")
        except ValueError:
            raw = {}
        out[m["id"]] = closed_reason(
            m["source"],
            datetime.fromisoformat(m["posted_at"]) if m["posted_at"] else None,
            datetime.fromisoformat(m["first_seen_at"]),
            raw,
            now,
            max_age_days,
        )
    return out


def current_decision(row) -> str | None:
    """The newest decision's action; 'undo' means: no decision. 'override_in' (the owner returned
    the job to the Inbox) is not a verdict on the job yet: the job is still waiting for one."""
    if row is None or row["action"] in ("undo", "override_in"):
        return None
    return row["action"]


@dataclass(frozen=True)
class InboxResult:
    items: list[InboxItem]
    total: int
    capped: bool
    unseen: int  # unseen groups among all that pass the filters (not only the shown ones)


def inbox(*args, **kwargs) -> tuple[list[InboxItem], int, bool]:
    r = inbox_result(*args, **kwargs)
    return r.items, r.total, r.capped


def inbox_result(
    store: Store,
    *,
    rule_version: str,
    threshold: float,
    prompt_version: str,
    max_age_days: int,
    filters: InboxFilters,
    now: datetime,
    limit: int = 200,
    only_cid: int | None = None,
    reason_labels: dict[str, str] | None = None,
) -> InboxResult:
    """Items, total, rejected_capped, unseen count. `only_cid`: just that group (HTMX row)."""
    rejected_mode = filters.eligible == "rejected"  # "screened" works on the kept groups
    since = now - timedelta(days=filters.since_days) if filters.since_days else None
    kept, rejected, capped = store.classified_groups(
        rule_version,
        threshold,
        rejected_since=since if rejected_mode else None,
        rejected_limit=10**9 if only_cid is not None else 500,
    )
    groups = kept + rejected if rejected_mode else kept
    rejected_ids = {g.canonical_id for g in rejected} if rejected_mode else set()
    if only_cid is not None:
        groups = [g for g in groups if g.canonical_id == only_cid]
    if not groups:
        return InboxResult([], 0, False, 0)
    cids = [g.canonical_id for g in groups]
    llm = {sg.canonical_id: sg.verdict for sg in store.llm_verdicts(prompt_version, cids)}
    members = store.conn.execute(
        "SELECT id, canonical_id, source, url, posted_at, first_seen_at, raw_json, title, company, "
        "salary_text, salary_min, salary_max, salary_currency, salary_period, employment_type "
        f"FROM jobs WHERE canonical_id IN ({','.join('?' * len(cids))}) ORDER BY id",
        cids,
    ).fetchall()
    by_group: dict[int, list] = {}
    for m in members:
        by_group.setdefault(m["canonical_id"], []).append(m)
    closed = closed_members(members, now, max_age_days)
    overrides = store.owner_overrides()
    override_tops = store.owner_override_tops()
    seen = store.seen_ids()

    items: list[InboxItem] = []
    for g in groups:
        alive = [m for m in by_group.get(g.canonical_id, []) if closed[m["id"]] is None]
        if not alive:
            continue  # every member is closed: the group is gone
        verdict = llm.get(g.canonical_id)
        # everything below comes from live members only: a closed member's data is hidden too
        g = dataclasses.replace(
            g,
            rate_display=rate_display(alive),
            posted_at=max(
                (datetime.fromisoformat(m["posted_at"]) for m in alive if m["posted_at"]),
                default=None,
            ),
        )
        override = overrides.get(g.canonical_id)
        if not _passes(
            g,
            verdict,
            alive,
            filters,
            since,
            rule_reject=g.canonical_id in rejected_ids,
            overridden=override is not None,
        ):
            continue
        rep = next((m for m in alive if m["id"] == g.representative_id), alive[0])
        decision = current_decision(store.latest_decisions([rep["id"]]).get(rep["id"]))
        if not _decision_ok(decision, filters.decision):
            continue
        emp = employment_of(alive, verdict)
        if filters.types and emp not in filters.types:
            continue
        unseen = g.canonical_id not in seen
        if filters.unseen_only and not unseen:
            continue
        title, company = rep["title"], rep["company"]
        items.append(
            InboxItem(
                group=g,
                job_id=rep["id"],
                title=title,
                company=company,
                llm=verdict,
                decision=decision,
                sightings=[
                    Sighting(m["source"], m["url"], attribution_for(m["source"])) for m in alive
                ],
                fit=verdict.fit if verdict else None,
                override_note=override["note"] if override else None,
                override_top=g.canonical_id in override_tops,
                emp_type=emp,
                unseen=unseen,
                first_seen=min(datetime.fromisoformat(m["first_seen_at"]) for m in alive),
                rate_value=hourly_rate(alive),
            )
        )
    items.sort(key=_sort_key(filters.sort))
    unseen_total = sum(1 for i in items if i.unseen)
    shown = items[:limit]
    whys = why_for(
        store,
        [i.canonical_id for i in shown],
        rule_version=rule_version,
        threshold=threshold,
        prompt_version=prompt_version,
        labels=reason_labels,
    )
    out = [dataclasses.replace(i, why=whys.get(i.canonical_id, Why())) for i in shown]
    return InboxResult(out, len(items), capped and rejected_mode, unseen_total)


def _fit_key(i: InboxItem):
    return (
        i.llm is None,
        -(i.fit if i.fit is not None else i.group.relevance),
        -(i.group.posted_at.timestamp() if i.group.posted_at else 0),
        i.canonical_id,
    )


def _sort_key(mode: str):
    """Applied after the filters. Missing values (no date, no rate) go last; ties by fit."""
    if mode == "new":
        return lambda i: (-(i.first_seen.timestamp() if i.first_seen else 0), _fit_key(i))
    if mode == "posted":
        return lambda i: (
            i.group.posted_at is None,
            -(i.group.posted_at.timestamp() if i.group.posted_at else 0),
            _fit_key(i),
        )
    if mode == "rate":
        return lambda i: (i.rate_value is None, -(i.rate_value or 0), _fit_key(i))
    return _fit_key


def _decision_ok(decision: str | None, mode: str) -> bool:
    if mode == "all":
        return True
    if mode == "none":
        return decision is None
    if mode == "interested":
        return decision == "interested"
    return decision in (None, "interested")  # default: rejected and CV-requested leave the list


def _passes(
    g: KeptGroup,
    v: JobVerdict | None,
    alive,
    f: InboxFilters,
    since,
    rule_reject: bool = False,
    overridden: bool = False,
) -> bool:
    if f.profile and (v.profile if v else g.profile) != f.profile:
        return False
    if f.fit_min is not None and (v is None or v.fit < f.fit_min):
        return False
    if f.rate_unknown is not None and ("rate_unknown" in g.flags) != f.rate_unknown:
        return False
    if f.source and f.source not in {m["source"] for m in alive}:
        return False
    if since is not None:
        born = g.posted_at or min(datetime.fromisoformat(m["first_seen_at"]) for m in alive)
        if born < since:
            return False
    if f.eligible == "rejected":  # rules said no, or Haiku said not eligible
        return rule_reject or (v is not None and not v.eligible)
    if f.eligible == "screened":  # "Screened out by AI": rules passed, Haiku said no, owner has not
        return v is not None and not v.eligible and not overridden
    if f.eligible == "yes":
        return v is not None and (v.eligible or overridden)
    if f.eligible == "no":
        return v is not None and not v.eligible and not overridden
    if f.eligible == "unscored":
        return v is None
    if f.eligible == "default":
        return v is None or v.eligible or overridden
    return True
