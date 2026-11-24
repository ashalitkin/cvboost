"""Issue 23: countries outside EU/US (A) and full-time foreign roles (B2B only)."""

from __future__ import annotations

import pytest

from tests.rules_helpers import ev

FAR = [
    "Singapore", "Remote - India", "Tokyo, Japan", "Hong Kong", "Seoul, South Korea",
    "Australia", "Sydney", "New Zealand", "Saudi Arabia", "Riyadh", "APAC", "Shanghai, China",
]  # fmt: skip


@pytest.mark.parametrize("loc", FAR)
def test_far_regions_rejected_with_own_reason(loc):
    r = ev(location_text=loc)
    assert "region_excluded" in r.reasons and r.decision == "reject"


@pytest.mark.parametrize("loc", ["Worldwide", "EMEA", "Europe", "Singapore, Worldwide"])
def test_far_with_worldwide_location_kept(loc):
    assert "region_excluded" not in ev(location_text=loc).reasons


@pytest.mark.parametrize(
    "desc",
    [
        "Our HQ is in Singapore. We hire remote candidates worldwide.",
        "Based in Sydney. Open to candidates anywhere in Europe.",
        "Company in Bangalore, fully remote EMEA role.",
    ],
)
def test_far_company_with_worldwide_or_emea_text_kept(desc):
    r = ev(location_text="Singapore", description_text=desc)
    assert "region_excluded" not in r.reasons and r.decision == "keep"


def test_far_title_tail_and_description():
    assert "region_excluded" in ev(title="Analytics Engineer - APAC").reasons
    assert "region_excluded" in ev(description_text="We are only hiring in Singapore.").reasons
    assert (
        "region_excluded" in ev(description_text="You must be authorized to work in India.").reasons
    )


def test_far_negated_worldwide_does_not_rescue():
    r = ev(location_text="Singapore", description_text="We do not hire worldwide.")
    assert "region_excluded" in r.reasons


def test_europe_stays_kept():
    for loc in ["Germany", "Spain", "Remote - Europe", "Poland"]:
        assert "region_excluded" not in ev(location_text=loc).reasons


# --- UK ---
@pytest.mark.parametrize("loc", ["UK", "London", "United Kingdom, Remote", "Remote (UK)"])
def test_uk_only_without_contractor_rejected(loc):
    r = ev(location_text=loc)
    assert "region_uk" in r.reasons and r.decision == "reject"


@pytest.mark.parametrize(
    "desc",
    [
        "Contract is inside IR35. Contractor needed.",
        "You must have the right to work in the UK. Contractor role.",
        "Paid via umbrella company.",
    ],
)
def test_uk_inside_ir35_or_right_to_work_rejected(desc):
    r = ev(location_text="UK", description_text=desc)
    assert r.decision == "reject" and {"region_uk", "region_restricted"} & set(r.reasons)


@pytest.mark.parametrize(
    "desc",
    [
        "Remote contractor role, outside IR35.",
        "Non-UK residents welcome, fully remote.",
        "B2B contract via your own company.",
        "We engage freelancers.",
    ],
)
def test_uk_with_contractor_conditions_kept_with_flag(desc):
    r = ev(location_text="UK", description_text=desc)
    assert "region_uk" not in r.reasons and "uk_conditional" in r.flags


def test_uk_outside_ir35_beats_umbrella_mention():
    r = ev(location_text="UK, remote", description_text="Outside IR35, no umbrella needed.")
    assert "region_uk" not in r.reasons


def test_uk_employment_type_contract_kept():
    r = ev(location_text="London", employment_type="Contract")
    assert "region_uk" not in r.reasons and "uk_conditional" in r.flags


def test_uk_worldwide_text_not_rejected():
    r = ev(location_text="UK", description_text="We hire remote candidates worldwide.")
    assert "region_uk" not in r.reasons


@pytest.mark.parametrize(
    "desc",
    [
        "Remote: UK or EU.",
        "We hire in the UK, Germany, Spain and Portugal.",
        "Remote from any EU country is fine.",
        "Candidates in the UK or EU time zones welcome.",
        "You can work remotely anywhere in Germany or London, UK.",
    ],
)
def test_uk_posting_allowing_eu_or_portugal_not_region_uk(desc):
    r = ev(location_text="UK", description_text=desc)
    assert "region_uk" not in r.reasons and "uk_conditional" in r.flags


def test_uk_posting_with_negated_eu_still_rejected():
    r = ev(location_text="UK", description_text="Sorry, no EU candidates.")
    assert "region_uk" in r.reasons


def test_uk_do_not_accept_contractors_rejected():
    r = ev(location_text="UK", description_text="We do not accept contractors.")
    assert "region_uk" in r.reasons


def test_uk_with_europe_or_other_conditional_not_rejected():
    assert "region_uk" not in ev(location_text="UK, Germany").reasons
    assert "region_uk" not in ev(location_text="UK, Switzerland").reasons
    assert ev(location_text="UK or Europe").decision == "keep"


# --- Switzerland / Israel / UAE: kept with a flag ---
@pytest.mark.parametrize(
    "loc,flag",
    [
        ("Switzerland", "switzerland"),
        ("Zurich", "switzerland"),
        ("Israel", "israel"),
        ("Tel Aviv, Israel", "israel"),
        ("UAE", "uae"),
        ("Dubai", "uae"),
    ],
)
def test_ch_il_uae_kept_with_flag(loc, flag):
    r = ev(location_text=loc)
    assert r.decision == "keep" and flag in r.flags
    assert not {"region_restricted", "region_excluded", "region_uk"} & set(r.reasons)


def test_ch_with_authorization_in_description_rejected():
    r = ev(location_text="Remote", description_text="Must be authorized to work in Switzerland.")
    assert "region_restricted" in r.reasons


# --- Canada: like the US ---
@pytest.mark.parametrize("loc", ["Canada", "Toronto, Canada", "Remote - Canada", "Vancouver"])
def test_canada_only_is_us_only(loc):
    assert "us_only" in ev(location_text=loc).reasons


def test_canada_authorization_blocker():
    r = ev(description_text="Must be authorized to work in Canada.")
    assert "us_only" in r.reasons


def test_canada_with_worldwide_kept():
    assert ev(location_text="Canada, Worldwide").decision == "keep"


def test_emea_remote_in_description_counts_as_broad():
    r = ev(location_text="Sydney", description_text="This is a remote EMEA position.")
    assert "region_excluded" not in r.reasons


# --- full-time foreign roles (B): rules do not reject, W2/no-contractor blockers stay ---
@pytest.mark.parametrize("etype", ["Full-time", "full_time", "Full time"])
def test_full_time_worldwide_remote_kept(etype):
    r = ev(location_text="Worldwide", employment_type=etype)
    assert r.decision == "keep"


def test_full_time_emea_remote_kept():
    r = ev(
        location_text="EMEA",
        employment_type="Full-time",
        description_text="Full-time remote role for a US company.",
    )
    assert r.decision == "keep"


@pytest.mark.parametrize(
    "desc", ["W2 only position.", "This is a W2 employee role.", "No contractors please."]
)
def test_w2_and_no_contractors_stay_blockers(desc):
    r = ev(location_text="Worldwide", employment_type="Full-time", description_text=desc)
    assert "w2_only" in r.reasons


def test_us_work_authorization_stays_blocker_for_full_time():
    r = ev(
        location_text="Remote",
        employment_type="Full-time",
        description_text="Must be authorized to work in the US.",
    )
    assert "us_only" in r.reasons


# --- prompts ---
def test_toronto_ca_is_canada_not_california():
    assert "us_only" in ev(location_text="Toronto, CA").reasons


# --- round 2 ---
@pytest.mark.parametrize(
    "loc,desc",
    [
        ("Berlin, Tokyo", "We hire worldwide."),
        ("Remote, Germany or Singapore", "We hire remote candidates worldwide."),
        ("Remote - Worldwide (HQ Singapore)", ""),
        ("Worldwide (Tokyo HQ)", ""),
        ("Remote (Headquartered in Sydney)", "Open to candidates anywhere in Europe."),
    ],
)
def test_far_city_inside_worldwide_posting_kept(loc, desc):
    r = ev(location_text=loc, description_text=desc)
    assert not {"region_excluded", "region_restricted"} & set(r.reasons), r.reasons


def test_eu_plus_far_without_worldwide_still_rejected():
    assert "region_excluded" in ev(location_text="Berlin, Tokyo").reasons


def test_anywhere_in_world_with_india_stays_reject():
    r = ev(location_text="Anywhere in the World (🇮🇳 India)")
    assert "region_excluded" in r.reasons


def test_emea_branch_is_narrow():
    r = ev(location_text="Singapore", description_text="You will work with our EMEA team.")
    assert "region_excluded" in r.reasons
    r = ev(location_text="Singapore", description_text="Candidates based in EMEA preferred.")
    assert "region_excluded" not in r.reasons


def test_new_south_wales_is_excluded_not_uk():
    r = ev(location_text="New South Wales")
    assert "region_excluded" in r.reasons and "region_uk" not in r.reasons
