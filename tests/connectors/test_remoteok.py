from __future__ import annotations

from datetime import UTC, datetime

from cvboost.connectors.remoteok import RemoteOKConnector
from tests.conftest import FIXTURES

BODY = (FIXTURES / "remoteok" / "response.json").read_bytes()
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def test_parse_skips_legal_element():
    c = RemoteOKConnector()
    raws = c.parse(BODY, c.base_url)
    assert len(raws) == 5
    assert all(r.source_id.isdigit() for r in raws)


def test_normalise_first():
    c = RemoteOKConnector()
    job = c.normalise(c.parse(BODY, c.base_url)[0], NOW)
    assert job.source == "remoteok" and job.source_id == "1137691"
    assert job.title == "Feltsense AI Engineer" and job.company == "Silver.dev"
    assert (
        job.url
        == "https://remoteok.com/remote-jobs/remote-feltsense-ai-engineer-silver-dev-1137691"
    )
    assert job.posted_at == datetime.fromtimestamp(1793894407, UTC)
    assert (job.salary_min, job.salary_max) == (60000, 110000)
    assert job.salary_currency == "USD" and job.salary_period == "year"
    assert job.location_text is None  # empty string in the feed


def test_zero_salary_is_none():
    c = RemoteOKConnector()
    raws = {r.source_id: r for r in c.parse(BODY, c.base_url)}
    job = c.normalise(raws["1137690"], NOW)
    assert job.salary_min is None and job.salary_max is None
    assert job.salary_currency is None and job.salary_period is None
    assert job.location_text == "Anywhere in the World"


def test_non_http_apply_url_is_dropped_but_job_kept():
    c = RemoteOKConnector()
    raw = c.parse(BODY, c.base_url)[0]
    raw = raw.model_copy(update={"payload": {**raw.payload, "apply_url": "javascript:alert(1)"}})
    job = c.normalise(raw, NOW)
    assert job.apply_url is None and job.title == "Feltsense AI Engineer"


def test_mojibake_fixed_and_clean_strings_kept():
    from cvboost.connectors.remoteok import fix_mojibake

    assert fix_mojibake("CafÃ©") == "Café"
    assert fix_mojibake("Weâ€™re hiring") == "We’re hiring"  # cp1252 variant
    assert fix_mojibake("Plain title") == "Plain title"
    assert fix_mojibake("Café") == "Café"  # already correct
    assert fix_mojibake("Ãsmund, Ã") == "Ãsmund, Ã"  # real Ã, not valid utf-8 pair: kept
    assert fix_mojibake("") == ""


def test_normalise_fixes_all_text_fields():
    c = RemoteOKConnector()
    raw = c.parse(BODY, c.base_url)[0]
    raw.payload.update(
        position="CafÃ© Engineer",
        company="CafÃ© Inc",
        location="ZÃ¼rich",
        description="<p>Weâ€™re hiring</p>",
        tags=["cafÃ©"],
    )
    job = c.normalise(raw, NOW)
    assert (job.title, job.company, job.location_text) == ("Café Engineer", "Café Inc", "Zürich")
    assert job.description_html == "<p>We’re hiring</p>"
    assert "We’re hiring" in job.description_text and job.tags == ["café"]


def test_fixture_rows_normalise_without_errors_and_stay_clean():
    c = RemoteOKConnector()
    for raw in c.parse(BODY, c.base_url):
        before = raw.payload["position"]
        title = c.normalise(raw, NOW).title
        assert title == before or "\u2013" in title  # clean rows untouched, mojibake row fixed
    dash = next(r for r in c.parse(BODY, c.base_url) if r.source_id == "1137689")
    assert "Inference Engineer \u2013 Terminal-Bench" in c.normalise(dash, NOW).title
