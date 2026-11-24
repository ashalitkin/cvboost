from __future__ import annotations

from datetime import timedelta

import pytest

from cvboost.core import rules
from cvboost.core.geo import classify_location
from cvboost.core.rules import ENGINE_VERSION, rule_version
from cvboost.triage import triage
from tests.conftest import NOW, make_job
from tests.rules_helpers import CFG, add, ev, sync

ALLOW = tuple(CFG.location.allow)


def test_engine_version_bumped():
    assert ENGINE_VERSION == 6 and rule_version(CFG).startswith("r6-")


@pytest.mark.parametrize(
    "loc", ["Berlin, DE", "Portugal, Spain, DE", "Bangalore, IN", "Bogotá, CO"]
)
def test_state_codes_that_are_country_codes_are_not_us(loc):
    assert classify_location(loc, ALLOW)[0] != "us_only"


@pytest.mark.parametrize("loc", ["Columbus, IN", "Scotch Plains, NJ", "Boston, MA", "Reno, NV"])
def test_real_us_city_state_is_us_only(loc):
    assert classify_location(loc, ALLOW)[0] == "us_only"


@pytest.mark.parametrize(
    "text",
    [
        "Applicants must be a U.S. citizen to apply.",
        "This role is open to US citizens only.",
        "Candidates are required to be United States citizens.",
    ],
)
def test_citizen_requirement(text):
    assert "us_only" in ev(description_text=text).reasons


@pytest.mark.parametrize(
    "text",
    [
        "We do not discriminate on the basis of race, religion, citizenship status or age.",
        "Equal opportunity employer regardless of U.S. citizenship status.",
        "Our team includes US citizens and people from 30 countries.",
    ],
)
def test_eeo_boilerplate_is_not_a_citizen_requirement(text):
    assert "us_only" not in ev(description_text=text).reasons


def test_reside_with_timezone_is_flag_not_reject():
    r = ev(description_text="You must be based in a time zone within 4 hours of CET overlap.")
    assert "tz_overlap" in r.flags
    assert not {"us_only", "region_restricted"} & set(r.reasons)


def test_reside_with_country_still_rejects():
    r = ev(description_text="You must be based in the United States.")
    assert "us_only" in r.reasons


def test_description_rate_without_context_is_unconfirmed_not_reject():
    r = ev(description_text="Our perks include $30/hr volunteering credit for events.")
    assert "rate_below_min" not in r.reasons and "rate_low_unconfirmed" in r.flags
    r = ev(description_text="Compensation: $30/hr for this contract.")
    assert "rate_below_min" in r.reasons


def test_unknown_currency_is_unknown_not_usd():
    r = ev(salary_min=30, salary_max=30, salary_currency="BRL", salary_period="hour")
    assert "rate_below_min" not in r.reasons and "rate_unknown" in r.flags
    r = ev(salary_text="30 BRL/hr")
    assert "rate_below_min" not in r.reasons and "rate_unknown" in r.flags


def test_kept_canonicals_threshold_is_required(store):
    with pytest.raises(TypeError):
        store.kept_canonicals(rule_version(CFG))  # type: ignore[call-arg]


@pytest.mark.parametrize("loc", ["Germany", "Spain only", "Berlin, Germany"])
def test_eu_country_specific_keeps_with_flag(loc):
    r = ev(location_text=loc)
    assert r.decision == "keep" and "eu_country_specific" in r.flags


def test_eu_with_usa_is_not_us_only():
    r = ev(location_text="Germany, USA")
    assert "us_only" not in r.reasons and "eu_country_specific" in r.flags


def test_uk_and_switzerland_are_conditional_not_restricted():
    # issue 23: UK-only is decided by the contractor / IR35 text, CH goes to the LLM with a flag
    assert "region_restricted" not in ev(location_text="UK").reasons
    assert "region_restricted" not in ev(location_text="Switzerland").reasons
    r = ev(location_text="Germany, UK")
    assert r.decision == "keep" and "uk_conditional" in r.flags


def test_allow_beats_eu_country_flag():
    assert "eu_country_specific" not in ev(location_text="Europe, Germany").flags


def test_data_scientist_is_unwanted_role():
    assert "unwanted_role" in ev(title="Lead Data Scientist").reasons
    assert "unwanted_role" in ev(title="Data Science Manager").reasons
    r = ev(title="Senior Data Scientist - Measurement, Experimentation & Causal Inference")
    assert r.decision == "reject" and r.reasons[0] == "unwanted_role"
    assert "unwanted_role" not in ev(title="Data Engineer").reasons


def test_fx_has_extra_currencies():
    assert CFG.rate.fx_to_usd["CAD"] == 0.73 and CFG.rate.fx_to_usd["DKK"] == 0.145
    r = ev(salary_min=60, salary_max=60, salary_currency="CHF", salary_period="hour")
    assert "rate_below_min" not in r.reasons and r.details["rate"]["usd_equiv"] == 67.8


@pytest.mark.parametrize("title", ["Paid Media Manager", "PPC Specialist", "Google Ads Expert"])
def test_campaign_operation_titles_are_not_relevant(title):
    # owner decision (#14): running ad campaigns is not the candidate's field
    assert "low_relevance" in ev(title=title).reasons


@pytest.mark.parametrize("title", ["Marketing Analytics Manager", "Attribution Analyst"])
def test_marketing_analytics_titles_are_relevant(title):
    assert "low_relevance" not in ev(title=title).reasons


PORTUGUESE = (
    "Estamos à procura de um engenheiro de dados para se juntar à nossa equipa em Lisboa, "
    "trabalhando de forma remota. Você vai desenhar e manter pipelines de dados, construir "
    "modelos analíticos e trabalhar com as equipas de produto e de marketing para responder "
    "a perguntas de negócio. Os candidatos devem ter experiência com SQL e Python, e é um "
    "plus conhecer ferramentas de orquestração. Oferecemos um ambiente flexível, uma equipa "
    "colaborativa, formação contínua e a possibilidade de crescer connosco. Se gosta de "
    "resolver problemas difíceis e quer fazer parte de um projeto ambicioso, esperamos por si. "
    "Também valorizamos a sua autonomia e o seu sentido de responsabilidade, tal como os "
    "nossos colegas, para que todos os dias sejam produtivos e estimulantes para os clientes."
)


def test_real_portuguese_paragraph_is_not_rejected():
    assert len(PORTUGUESE.split()) >= 80
    assert "language" not in ev(description_text=PORTUGUESE).reasons


def test_a_b_a_is_idempotent(store):
    sync(store, "fake")
    add(store, "fake", "1", description_text="text A")
    triage(store, CFG, NOW)

    def edit(text, when):
        run = store.start_run("fake", when)
        with store.transaction():
            store.upsert_job(make_job("1", source="fake", description_text=text), run, when)

    edit("text B", NOW + timedelta(hours=1))
    triage(store, CFG, NOW + timedelta(hours=2))
    edit("text A", NOW + timedelta(hours=3))
    st = triage(store, CFG, NOW + timedelta(hours=4))
    assert st.evaluated == 1
    assert store.conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 2
    again = triage(store, CFG, NOW + timedelta(hours=5))
    assert again.evaluated == 0
    latest = store.latest_verdict(1, rule_version(CFG))
    assert (
        latest is not None
        and latest["input_hash"]
        == rules.RuleInput.from_job(
            make_job("1", source="fake", description_text="text A")
        ).input_hash()
    )


@pytest.mark.parametrize(
    "text",
    [
        "Applicants are required to be considered without regard to U.S. citizenship status.",
        "You do not need to be a US citizen to apply.",
        "US citizenship is not required.",
    ],
)
def test_citizen_negation_and_status_backtracking(text):
    assert "us_only" not in ev(description_text=text).reasons


def test_must_be_citizen_still_us_only():
    assert "us_only" in ev(description_text="Must be a U.S. citizen.").reasons
