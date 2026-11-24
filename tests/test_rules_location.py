from __future__ import annotations

import pytest

from tests.rules_helpers import ev


def _loc(r):
    return {x for x in r.reasons if x in ("us_only", "region_restricted")}


def test_authorized_to_work_in_us_description():
    r = ev(description_text="You must be authorized to work in the US without sponsorship.")
    assert "us_only" in r.reasons
    assert "authorized to work" in r.details["evidence"]["us_only"]


@pytest.mark.parametrize(
    "loc",
    [
        "USA Only",
        "Anywhere in the World (🇺🇸 United States of America)",
        "Columbus, IN",
        "Austin, Texas",
    ],
)
def test_us_locations(loc):
    assert "us_only" in ev(location_text=loc).reasons


def test_latin_america_only_is_latam_only():
    r = ev(location_text="Latin America Only")
    assert "latam_only" in r.reasons and "us_only" not in r.reasons


def test_title_tail_usa():
    assert "us_only" in ev(title="Partner Manager - USA").reasons


def test_us_citizen_clearance():
    r = ev(
        description_text="Applicants must be a U.S. citizen and hold an active secret clearance."
    )
    assert "us_only" in r.reasons


def test_must_be_located_in_states():
    r = ev(description_text="You must be located in one of the following states: Ohio, Texas.")
    assert "us_only" in r.reasons


@pytest.mark.parametrize(
    "loc",
    [
        "Remote — Europe",
        "Worldwide",
        "EMEA Only",
        "Anywhere in the World",
        "Europe, USA, Canada",
        "US, UK, Canada + 35 other countries",
        "",
        None,
        "Berlin",
    ],
)
def test_no_location_reason(loc):
    assert not _loc(ev(location_text=loc))


def test_wwr_long_list_with_portugal():
    countries = ", ".join(f"🇩🇪 Germany{i}" for i in range(60))
    loc = f"Anywhere in the World (🇵🇹 Portugal, 🇺🇸 United States of America, {countries})"
    assert not _loc(ev(location_text=loc))


def test_pronoun_us_not_matched():
    r = ev(description_text="Authorized to work in Spain, ask us anything.")
    assert "us_only" not in r.reasons
