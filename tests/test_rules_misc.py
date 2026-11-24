from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from cvboost.connectors.remoteok import RemoteOKConnector
from cvboost.connectors.remotive import RemotiveConnector
from cvboost.connectors.wwr import WWRConnector
from cvboost.core.language import detect_language
from cvboost.core.rules import RuleInput, compile_rules, evaluate, load_config
from tests.conftest import FIXTURES
from tests.rules_helpers import CFG, RULES_PATH, ev

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "text", ["W2 only", "No C2C please", "Engagement: W2 contract", "no contractors"]
)
def test_w2_only(text):
    assert "w2_only" in ev(description_text=text).reasons


@pytest.mark.parametrize(
    "text",
    [
        "W2 or C2C",
        "C2C/W2",
        "1099/Contract Providers are ineligible for these benefits",
    ],
)
def test_w2_not_triggered(text):
    assert "w2_only" not in ev(description_text=text).reasons


GERMAN = (
    "Wir suchen einen Entwickler für unser Team und Sie werden mit der Analyse der Daten "
    "arbeiten. Das ist eine Stelle bei uns und wir bieten eine gute Bezahlung. Die Aufgaben "
    "sind vielseitig und der Standort ist flexibel, auf Wunsch auch mit Home Office. Sie "
    "sind nicht allein und wir helfen bei der Einarbeitung oder bei Fragen."
)
PORT = (
    "Procuramos um engenheiro para a nossa equipa. Você vai trabalhar com os dados e não "
    "estará sozinho, também terá suporte. As tarefas são variadas e os dias são flexíveis "
    "para os nossos colegas, com sua autonomia e seu espaço em casa ou em escritório."
)
RUSSIAN = "Мы ищем аналитика данных в нашу команду. " * 8


def test_language_german_rejected():
    r = ev(description_text=GERMAN)
    assert "language" in r.reasons


def test_language_pt_ru_short_ok():
    assert "language" not in ev(description_text=PORT).reasons
    assert "language" not in ev(description_text=RUSSIAN).reasons
    assert "language" not in ev(description_text="Das ist kurz und nicht lang").reasons
    assert detect_language("hello", ("en",), 30) is None


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"title": "Data Engineer (Hybrid)"}, True),
        ({"description_text": "It is a hybrid role with expectations of office days."}, True),
        ({"description_text": "This is a hybrid sales and customer success role."}, False),
        (
            {"description_text": "Remote or hybrid working from our NYC office 3 days per week."},
            False,
        ),
    ],
)
def test_onsite(kw, expected):
    assert ("onsite" in ev(**kw).reasons) is expected


def test_blocked_company_via_normalisation():
    cfg = CFG.model_copy(update={"blocked_companies": ["ACME Inc."]})
    r = evaluate(
        RuleInput(
            company="acme",
            title="Data Engineer",
            description_text="",
            location_text=None,
            tags=(),
            salary_min=None,
            salary_max=None,
            salary_currency=None,
            salary_period=None,
            salary_text=None,
            employment_type=None,
        ),
        compile_rules(cfg),
    )
    assert "blocked_company" in r.reasons


def test_redos_timing():
    text = ("1 2 3 4 5 $ " * 2500)[:30000] + " authorized to work in " + "a " * 100
    t = time.perf_counter()
    ev(description_text=text, location_text=text[:200])
    assert time.perf_counter() - t < 2.0
    spaced = " " * 30000
    t = time.perf_counter()
    ev(description_text=spaced + "$" + "9" * 100)
    assert time.perf_counter() - t < 2.0


def _fixture_jobs():
    cases = [
        ("remotive", RemotiveConnector(), ["response.json"]),
        ("remoteok", RemoteOKConnector(), ["response.json"]),
        (
            "wwr",
            WWRConnector(),
            ["remote-back-end-programming-jobs.xml", "remote-sales-and-marketing-jobs.xml"],
        ),
    ]
    for name, conn, files in cases:
        for f in files:
            for raw in conn.parse((FIXTURES / name / f).read_bytes(), "x"):
                yield conn.normalise(raw, NOW)


def test_fixtures_no_exceptions_and_location_reasons():
    n = 0
    us = lat = 0
    for job in _fixture_jobs():
        r = evaluate(RuleInput.from_job(job), compile_rules(CFG))
        n += 1
        if job.source == "wwr" and (job.location_text or "").startswith("USA Only"):
            assert "us_only" in r.reasons
            us += 1
        if job.source == "wwr" and (job.location_text or "").startswith("Latin America Only"):
            assert "latam_only" in r.reasons
            lat += 1
    assert n >= 10 and us >= 1 and lat >= 0


def test_shipped_config_loads_and_bad_config_fails(tmp_path):
    assert load_config(RULES_PATH) == CFG
    bad = tmp_path / "bad.yaml"
    bad.write_text("rate: {nope: 1}\n")
    with pytest.raises(ValueError):
        load_config(bad)
    extra = tmp_path / "extra.yaml"
    extra.write_text(RULES_PATH.read_text() + "typo_key: 1\n")
    with pytest.raises(ValueError):
        load_config(extra)
