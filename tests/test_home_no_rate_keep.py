"""Home country without a stated rate (owner 2026-11-23): from `no_rate_keep_since` the posting is
kept with the flag `<code>_no_rate` and takes the normal LLM path; earlier ones stay rejected.
A remote + Europe title is not home-only."""

from __future__ import annotations

from datetime import date

from cvboost.core.rules import RuleInput, RulesConfig, compile_rules, evaluate, rule_version
from cvboost.score import score
from cvboost.triage import triage
from tests.conftest import NOW
from tests.llm.helpers import DESC, FakeClient, make_cfg
from tests.rules_helpers import CFG, add, sync

SINCE = date(2026, 11, 5)  # NOW = 2026-11-06


def cfg_since(since: date | None) -> RulesConfig:
    data = CFG.model_dump()
    data["location"]["home"]["no_rate_keep_since"] = since
    return RulesConfig.model_validate(data)


def run(cfg: RulesConfig, first_seen_at=None, **kw):
    data = dict(
        company="Acme", title="Analytics Engineer (dbt, BigQuery)", description_text="",
        location_text="Strelsau, Ruritania", tags=(), salary_min=None, salary_max=None,
        salary_currency=None, salary_period=None, salary_text=None, employment_type=None,
    )  # fmt: skip
    data.update(kw)
    return evaluate(RuleInput(**data), compile_rules(cfg), first_seen_at=first_seen_at)


def test_without_the_date_home_only_is_rejected_as_before():
    r = run(cfg_since(None), first_seen_at="2026-11-06T10:00:00+00:00")
    assert r.decision == "reject" and "ruritania_no_rate" in r.reasons


def test_new_postings_are_kept_with_a_flag_old_ones_rejected():
    cfg = cfg_since(SINCE)
    new = run(cfg, first_seen_at="2026-11-05T00:00:00+00:00")
    assert new.decision == "keep" and "ruritania_no_rate" in new.flags
    assert "ruritania_no_rate" not in new.details["evidence"]
    old = run(cfg, first_seen_at="2026-11-04T23:59:00+00:00")
    assert old.decision == "reject" and old.reasons == ["ruritania_no_rate"]
    assert run(cfg).decision == "keep"  # date unknown = a new posting


def test_other_hard_reasons_still_reject():
    cfg = cfg_since(SINCE)
    r = run(cfg, title="Senior Data Scientist")
    assert r.decision == "reject" and "unwanted_role" in r.reasons
    low = run(cfg, salary_min=10, salary_max=12, salary_currency="USD", salary_period="hour")
    assert "rate_below_min" in low.reasons and "ruritania_no_rate" not in low.flags


def test_remote_europe_in_the_title_is_not_home_only():
    cfg = cfg_since(None)
    for title in ("Data Engineer (Remote | Europe)", "Remote EU Analytics Engineer",
                  "Analytics Engineer - EMEA (Remote)"):  # fmt: skip
        r = run(cfg, title=title, location_text="Ruritania")
        assert "ruritania_no_rate" not in r.reasons, title
    assert "ruritania_no_rate" in run(cfg, title="Data Engineer (REMOTE)").reasons


def test_flagged_groups_go_to_the_llm(store):
    cfg = cfg_since(SINCE)
    sync(store, "fake")
    add(store, "fake", "1", company="Home Co", title="Senior Analytics Engineer (dbt)",
        description_text=DESC.replace("anywhere in the world", "in Strelsau"),
        location_text="Strelsau, Ruritania", tags=["dbt", "sql"])  # fmt: skip
    triage(store, cfg, NOW)
    rv = rule_version(cfg)
    client = FakeClient()
    st = score(store, client, make_cfg(), rv, cfg.relevance.threshold, lambda: NOW)
    assert st.scored == 1
    assert "Home Co" in "".join(r.stdin_text for r in client.requests)
