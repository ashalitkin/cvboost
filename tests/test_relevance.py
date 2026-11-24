from __future__ import annotations

from tests.rules_helpers import CFG, ev


def test_analytics_engineer_above_threshold():
    r = ev(title="Analytics Engineer (dbt, BigQuery)")
    assert r.relevance >= CFG.relevance.threshold
    assert r.profile == "data_engineer"
    assert "low_relevance" not in r.reasons


def test_mortgage_low_relevance():
    assert "low_relevance" in ev(title="Mortgage Loan Officer").reasons


def test_negative_title_multiplier():
    plain = ev(title="Data Analyst")
    clinical = ev(title="Data Analyst, Clinical")
    assert clinical.relevance < plain.relevance


def test_marketing_profile():
    r = ev(
        title="Marketing Analyst",
        description_text="GA4, GTM, Conversions API, attribution, UTM, ROAS reporting.",
    )
    assert r.profile == "marketing_analytics"


# ---- #65: tech_lead profile ------------------------------------------------------------------

DESC = "Hands-on technical leadership in TypeScript, Node, Postgres and AWS for a small startup."


def test_cto_titles_get_the_tech_lead_profile():
    for title in [
        "CTO",
        "Fractional CTO",
        "Technical Co-founder",
        "Founding CTO",
        "Chief Technology Officer",
        "Co-founder & CTO",
    ]:
        r = ev(title=title, description_text=DESC)
        assert r.profile == "tech_lead", title
        assert "low_relevance" not in r.reasons, title


def test_founding_engineer_passes_with_lower_weight():
    cto = ev(title="CTO", description_text=DESC)
    fe = ev(title="Founding Engineer", description_text=DESC)
    assert fe.profile == "tech_lead" and "low_relevance" not in fe.reasons
    assert fe.relevance < cto.relevance


def test_tech_lead_needs_skills_beyond_the_title():
    bare = ev(title="Tech Lead", description_text="We build things. " * 10)
    assert "low_relevance" in bare.reasons
    rich = ev(title="Tech Lead", description_text=DESC)
    assert rich.profile == "tech_lead" and "low_relevance" not in rich.reasons


def test_negative_words_do_not_kill_cto_or_founding_engineer():
    assert "low_relevance" not in ev(title="CTO (Full-stack)", description_text=DESC).reasons
    assert (
        "low_relevance"
        not in ev(title="Fractional CTO - Backend & DevOps", description_text=DESC).reasons
    )
    assert (
        "low_relevance"
        not in ev(title="Founding Engineer (Full-Stack, React)", description_text=DESC).reasons
    )


def test_negative_words_still_penalise_plain_tech_lead():
    plain = ev(title="Tech Lead", description_text=DESC)
    fe = ev(title="Tech Lead (Frontend)", description_text=DESC)
    assert fe.relevance < plain.relevance


def test_people_management_titles_are_penalised_not_rejected_by_rule():
    for title in ["VP Engineering", "Head of Engineering", "Engineering Manager"]:
        r = ev(title=title, description_text=DESC)
        assert r.profile != "tech_lead"  # require_title: no skills-only match
        assert "unwanted_role" not in r.reasons
    cto_vp = ev(title="Tech Lead", description_text=DESC).relevance
    eng_mgr_tl = ev(title="Tech Lead, Engineering Manager", description_text=DESC).relevance
    assert eng_mgr_tl < cto_vp


def test_tech_lead_does_not_change_data_and_marketing_profiles():
    de = ev(title="Analytics Engineer (dbt, BigQuery)", description_text=DESC)
    assert de.profile == "data_engineer"
    assert (
        ev(
            title="Marketing Analyst",
            description_text="GA4, GTM, attribution, UTM, ROAS, TypeScript, Node, Postgres, AWS",
        ).profile
        == "marketing_analytics"
    )


# ---- #65 round 2: profile-level negative_title / exclude_title ---------------------------------

DATA_DESC = "SQL, dbt, BigQuery, Snowflake, Looker, data warehouse, python, airflow."
# (title, profile, relevance) measured on main (config before #65)
MAIN_SCORES = [
    ("Data Engineering Manager", "data_engineer", 94),
    ("Engineering Manager, Data Platform", "data_engineer", 82),
    ("Analytics Engineering Manager", "data_engineer", 82),
    ("Engineering Manager, Marketing Technology", "marketing_analytics", 67),
    ("Head of Data Engineering", "data_engineer", 100),
]


def test_manager_titles_keep_main_scores_for_data_and_marketing():
    for title, profile, rel in MAIN_SCORES:
        r = ev(title=title, description_text=DATA_DESC)
        assert (r.profile, r.relevance) == (profile, rel), title


def test_global_negative_title_has_no_manager_terms():
    assert not {"engineering manager", "vp engineering", "head of engineering"} & set(
        CFG.relevance.negative_title
    )


def test_manager_penalty_is_profile_level_for_tech_lead():
    tl = CFG.relevance.profiles["tech_lead"]
    assert "engineering manager" in tl.negative_title
    plain = ev(title="Tech Lead", description_text=DESC).relevance
    assert ev(title="Tech Lead, Engineering Manager", description_text=DESC).relevance < plain


def test_cto_lookalikes_are_not_tech_lead():
    for title in [
        "Senior Executive Assistant to the CTO",
        "Executive Assistant - Chief Product Officer & Chief Technology Officer",
        "[Interim] Strategy & Transformation Lead (CTO office)",
        "Field CTO, London",
        "Account CTO",
        "Chief of Staff to the CTO",
        "Tech Lead Manager",
        "Technical Lead Manager",
    ]:
        r = ev(title=title, description_text=DESC)
        assert r.profile != "tech_lead" or r.relevance == 0, title
        assert r.details["relevance"]["tech_lead"]["score"] == 0, title
        assert "low_relevance" in r.reasons, title
