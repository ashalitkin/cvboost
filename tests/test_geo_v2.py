from __future__ import annotations

import pytest

from tests.rules_helpers import ev


@pytest.mark.parametrize(
    "loc", ["Poland", "CEE", "Remote — Eastern Europe", "Ukraine", "Estonia", "Bulgaria"]
)
def test_eastern_europe_rejected(loc):
    r = ev(location_text=loc)
    assert "region_eastern_europe" in r.reasons and "eu_country_specific" not in r.flags


@pytest.mark.parametrize("loc", ["Europe (incl. Poland)", "Germany, Poland", "Worldwide"])
def test_eastern_europe_mixed_kept(loc):
    assert "region_eastern_europe" not in ev(location_text=loc).reasons


def test_germany_still_eu_specific():
    r = ev(location_text="Germany")
    assert "eu_country_specific" in r.flags and "region_eastern_europe" not in r.reasons


def test_ruritania_without_rate_rejected():
    assert "ruritania_no_rate" in ev(location_text="Strelsau, Ruritania").reasons
    assert (
        "rate_below_min"
        in ev(
            location_text="Ruritania",
            salary_min=20,
            salary_max=20,
            salary_currency="EUR",
            salary_period="hour",
        ).reasons
    )


def test_ruritania_with_rate_kept():
    r = ev(
        location_text="Strelsau, Ruritania",
        salary_min=55,
        salary_max=55,
        salary_currency="EUR",
        salary_period="hour",
    )
    assert "ruritania_no_rate" not in r.reasons


def test_ruritania_with_other_region_not_affected():
    assert "ruritania_no_rate" not in ev(location_text="Ruritania, Spain").reasons
    assert "ruritania_no_rate" not in ev(location_text="Ruritania or Europe").reasons


@pytest.mark.parametrize("loc", ["Hybrid, 2 days in office", "Strelsau (Hybrid)"])
def test_hybrid_rejected(loc):
    assert "onsite" in ev(location_text=loc).reasons


def test_optional_office_and_hybrid_option_kept():
    assert (
        "onsite"
        not in ev(
            location_text="Remote", description_text="Fully remote with optional office in Berlin."
        ).reasons
    )
    assert "onsite" not in ev(location_text="Remote-first with hybrid option").reasons
    assert "onsite" in ev(description_text="This is a hybrid role in Berlin.").reasons


def test_us_remote_contractors_worldwide_not_rejected():
    r = ev(location_text="Remote", description_text="US remote, contractors worldwide welcome.")
    assert "us_only" not in r.reasons


def test_part_time_remote_eu_contract_passes():
    r = ev(
        location_text="Europe",
        employment_type="part_time",
        salary_min=50,
        salary_max=50,
        salary_currency="EUR",
        salary_period="hour",
        description_text="Fractional contract, 20 hours/week, fully remote.",
    )
    assert r.decision == "keep" and "part_time_ok" in r.flags


@pytest.mark.parametrize(
    "loc",
    [
        "Worldwide (excl. Russia, Belarus)",
        "Europe (except Russia)",
        "Europe (excluding Poland)",
        "Europe (Poland preferred)",
        "Germany ( excluding Poland)",
    ],
)
def test_exclusion_parentheses_do_not_narrow(loc):
    r = ev(location_text=loc)
    assert not {"region_eastern_europe", "region_restricted"} & set(r.reasons)


def test_exclusion_parentheses_in_description():
    r = ev(
        location_text="Remote",
        description_text="You must be located in Europe (excluding Russia and Belarus).",
    )
    assert "region_eastern_europe" not in r.reasons


def test_ruritania_rate_in_description_prevents_reject():
    r = ev(location_text="Strelsau", description_text="Rate: 60 EUR per hour, B2B contract.")
    assert r.decision == "keep" and "ruritania_no_rate" not in r.reasons
    assert (
        "ruritania_no_rate" in ev(location_text="Strelsau", description_text="Great team").reasons
    )


@pytest.mark.parametrize("loc", ["Strelsau or Remote", "Oporto / Remote"])
def test_ruritania_or_remote_not_ruritania_only(loc):
    assert "ruritania_no_rate" not in ev(location_text=loc).reasons


def test_ruritania_broad_description():
    r = ev(
        location_text="Ruritania",
        description_text="You can work remotely from anywhere in the EU.",
    )
    assert "ruritania_no_rate" not in r.reasons
    assert "ruritania_no_rate" in ev(location_text="Festenburg").reasons


@pytest.mark.parametrize(
    "loc", ["Remote or Hybrid", "Remote/Hybrid", "Hybrid (optional)", "Fully remote, no hybrid"]
)
def test_optional_hybrid_kept(loc):
    assert "onsite" not in ev(location_text=loc).reasons


@pytest.mark.parametrize(
    "title",
    [
        "Remote Senior Media Buyer",
        "Paid Media Specialist",
        "PPC Manager (Google Ads)",
        "Senior Campaign Manager",
        "Growth Marketing Manager",
    ],
)
def test_campaign_operation_titles_rejected(title):
    assert "unwanted_role" in ev(title=title).reasons


@pytest.mark.parametrize(
    "title",
    [
        "Performance Marketing Analyst",
        "Marketing Analytics Manager",
        "Paid Media Analytics Manager",
        "Paid Media Measurement Specialist",
        "Attribution Analyst, Campaign Manager data",
    ],
)
def test_analytics_titles_not_unwanted(title):
    assert "unwanted_role" not in ev(title=title).reasons


def test_data_scientist_still_rejected_even_with_analytics():
    assert "unwanted_role" in ev(title="Data Scientist, Marketing Analytics").reasons


@pytest.mark.parametrize(
    "text",
    [
        "Salary: €12k per month",
        "150.000€ per year",
        "Contract, 60 EUR/hour",
        "Pay 10 EUR/hour",
        "$50,000 monthly ad spend",
    ],
)
def test_ruritania_any_money_is_kept_with_check_flag(text):
    r = ev(location_text="Strelsau", description_text=text)
    assert "ruritania_no_rate" not in r.reasons and "ruritania_rate_check" in r.flags


def test_ruritania_no_money_is_rejected():
    r = ev(location_text="Strelsau", description_text="Great team, 20 hours per week.")
    assert "ruritania_no_rate" in r.reasons and "ruritania_rate_check" not in r.flags


@pytest.mark.parametrize(
    "text",
    [
        "We are a worldwide leader.",
        "Our clients are worldwide.",
        "Not open to worldwide applicants.",
    ],
)
def test_worldwide_outside_hiring_context_does_not_lift(text):
    assert "ruritania_no_rate" in ev(location_text="Strelsau", description_text=text).reasons


@pytest.mark.parametrize("text", ["Remote worldwide.", "We are hiring worldwide."])
def test_worldwide_hiring_lifts_ruritania(text):
    assert "ruritania_no_rate" not in ev(location_text="Strelsau", description_text=text).reasons


@pytest.mark.parametrize(
    "title",
    [
        "Head of Paid Media",
        "Paid Media Lead",
        "Performance Marketing Lead",
        "Growth Marketer",
        "Media Buying Manager",
        "PPC Account Manager",
    ],
)
def test_campaign_operation_title_patterns(title):
    assert "unwanted_role" in ev(title=title).reasons


def test_performance_marketing_analyst_not_unwanted():
    assert "unwanted_role" not in ev(title="Performance Marketing Analyst").reasons


def test_lisbon_or_madrid_not_ruritania_only():
    assert "ruritania_no_rate" not in ev(location_text="Strelsau or Madrid").reasons
    assert "ruritania_no_rate" not in ev(location_text="Barcelona, Strelsau").reasons
