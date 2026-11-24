"""Issue 67: hiring only in Latin America is a hard reject (`latam_only`)."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from cvboost.core.rules import rule_version
from cvboost.storage.db import Store, group_reasons
from cvboost.triage import triage
from tests.conftest import NOW
from tests.rules_helpers import CFG, add, ev, sync

POWER_DIGITAL = (
    "Who We Are: Power Digital is an AI-Native Growth Firm. We have offices in Buenos Aires and "
    "Medellin. Compensation in USD, healthcare stipend $50. Build dbt and BigQuery pipelines."
)


@pytest.mark.parametrize(
    "loc", ["Colombia", "Mexico", "Argentina", "Brazil", "LATAM", "Mexico City"]
)
def test_latam_location_only(loc):
    r = ev(location_text=loc)
    assert "latam_only" in r.reasons and "region_excluded" not in r.reasons
    assert r.decision == "reject"


@pytest.mark.parametrize(
    "loc",
    ["Colombia, Ruritania", "Colombia, Germany", "Brazil, Worldwide", "Europe", "Remote - EU"],
)
def test_latam_with_portugal_eu_worldwide_not_latam_only(loc):
    assert "latam_only" not in ev(location_text=loc).reasons


@pytest.mark.parametrize(
    "text",
    [
        "This role is LATAM only.",
        "Candidates must be based in Latin America.",
        "You must reside in LATAM.",
        "We are only hiring in Latin America for this role.",
    ],
)
def test_latam_only_text(text):
    r = ev(description_text=text)
    assert "latam_only" in r.reasons and r.decision == "reject"


def test_latam_office_next_to_us_offices_is_no_signal():
    r = ev(
        location_text="Remote",
        description_text="Offices in New York, Austin and Buenos Aires. Remote friendly team.",
    )
    assert "latam_only" not in r.reasons


def test_negated_and_list_mentions_do_not_trigger():
    assert "latam_only" not in ev(description_text="Based in Latin America, US and Europe.").reasons


def test_worldwide_lifts_location_derived_but_not_explicit_text():
    d = "We hire remote candidates worldwide."
    assert "latam_only" not in ev(location_text="Brazil", description_text=d).reasons
    assert "latam_only" in ev(description_text=d + " This role is LATAM only.").reasons


def test_power_digital_group_all_latam_is_rejected(store: Store):
    sync(store, "remotefirstjobs", "himalayas")
    add(store, "remotefirstjobs", "pd0", company="Power Digital", description_text=POWER_DIGITAL)
    for i, country in enumerate(["Colombia", "Mexico", "Argentina", "Brazil"]):
        add(
            store,
            "himalayas",
            f"pd{i + 1}",
            company="Power Digital",
            location_text=country,
            description_text=POWER_DIGITAL,
        )
    triage(store, CFG, NOW)
    version = rule_version(CFG)
    assert store.kept_canonicals(version, 0) == []
    _, rejected, _ = store.classified_groups(version, 0)
    assert len(rejected) == 1
    codes = {
        c for r in store.conn.execute("SELECT reasons_json FROM verdicts") for c in json.loads(r[0])
    }
    assert "latam_only" in codes


def test_group_with_portugal_sighting_is_not_latam_only(store: Store):
    sync(store, "a", "b")
    add(store, "a", "1", company="Beta", location_text="Colombia", description_text="same text")
    add(store, "b", "2", company="Beta", location_text="Ruritania", description_text="same text")
    triage(store, CFG, NOW)
    rows = store.conn.execute("SELECT reasons_json, flags_json FROM verdicts").fetchall()
    pairs = [(json.loads(r[0]), json.loads(r[1])) for r in rows]
    assert any("latam_only" in c for c, _ in pairs)  # the Colombia member alone says so
    assert "latam_only" not in group_reasons(pairs)
    # an explicit description phrase still stands next to a home-country sighting
    assert "latam_only" in group_reasons([(["latam_only"], []), ([], ["loc_not_latam"])])


def test_rule_version_is_engine_6():
    assert rule_version(CFG).startswith("r6-")


def test_verdict_is_not_duplicated_on_rerun(store: Store):
    sync(store, "fake")
    add(store, "fake", "1", location_text="Colombia")
    triage(store, CFG, NOW)
    triage(store, CFG, NOW + timedelta(hours=1))
    assert store.conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 1
