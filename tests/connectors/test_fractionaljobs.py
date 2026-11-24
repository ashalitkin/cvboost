from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from cvboost.connectors.fractionaljobs import FractionalJobsConnector, parse_rate
from cvboost.http import HttpClient
from cvboost.models import RawPosting
from tests.conftest import FIXTURES, PUBLIC_RESOLVER
from tests.rules_helpers import ev

HOME = (FIXTURES / "fractionaljobs" / "home.html").read_bytes()
DETAIL = (FIXTURES / "fractionaljobs" / "detail.html").read_bytes()
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def test_list_parses_every_card():
    raws = FractionalJobsConnector().parse(HOME, "x")
    assert len(raws) == 8
    assert raws[0].source_id == "vp-group-director-integrated-strategy-at-syneos-health"


def test_first_card_exact_values():
    c = FractionalJobsConnector()
    job = c.normalise(c.parse(HOME, "x")[0], NOW)
    assert job.company == "Syneos Health"
    assert job.title == "VP Group Director, Integrated Strategy"
    assert job.url == (
        "https://www.fractionaljobs.io/jobs/vp-group-director-integrated-strategy-at-syneos-health"
    )
    assert job.location_text == "Remote (USA only)" and job.remote is True
    assert job.employment_type == "part_time" and "fractional" in job.tags
    assert (job.salary_min, job.salary_max, job.salary_period) == (135.0, 150.0, "hour")
    assert job.salary_currency == "USD"
    assert job.posted_at == datetime(2026, 11, 16, tzinfo=UTC)
    assert "10 - 20 hrs" in job.description_text


def test_unknown_rate_and_hybrid_location():
    c = FractionalJobsConnector()
    jobs = {r.payload["company"]: c.normalise(r, NOW) for r in c.parse(HOME, "x")}
    assert jobs["Healthybaby"].salary_min is None and jobs["Healthybaby"].salary_text is None
    assert jobs["Dash Bio"].remote is None  # "Hybrid (Boston only)"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$150 - $250 / hr", (150.0, 250.0, "hour")),
        ("$10K - $20K / mo", (10000.0, 20000.0, "month")),
        ("$200 / hr", (200.0, 200.0, "hour")),
        ("Unknown", (None, None, None)),
        ("", (None, None, None)),
        ("$2K / wk", (None, None, None)),
    ],
)
def test_rate_table(text, expected):
    assert parse_rate(text) == expected


def test_detail_description_is_extracted():
    html = FractionalJobsConnector.parse_detail(DETAIL)
    assert "strategic" in html and "<script" not in html


def test_fetch_reads_list_then_details_of_relevant_functions_only():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(200, content=HOME if req.url.path == "/" else DETAIL)

    http = HttpClient(httpx.MockTransport(handler), sleep=lambda s: None, resolver=PUBLIC_RESOLVER)
    raws = FractionalJobsConnector().fetch(http)
    assert seen[0] == "/" and len(raws) == 8
    wanted = sum(1 for r in raws if r.payload["function"] in {"Marketing", "Other", "Engineering"})
    assert len(seen) == 1 + wanted  # no detail request for other functions
    assert all(p.startswith("/jobs/") for p in seen[1:])
    assert raws[0].payload["description_html"]


def test_failed_detail_keeps_the_list_entry():
    def handler(req):
        return httpx.Response(200, content=HOME) if req.url.path == "/" else httpx.Response(404)

    http = HttpClient(httpx.MockTransport(handler), sleep=lambda s: None, resolver=PUBLIC_RESOLVER)
    raws = FractionalJobsConnector().fetch(http)
    assert len(raws) == 8 and raws[0].payload["description_html"] == ""


def test_malformed_card_is_skipped():
    body = b'<div class="job-item"><h3>Only</h3></div>'
    assert FractionalJobsConnector().parse(body, "x") == []


def _raw(**payload) -> RawPosting:
    base = {
        "slug": "analytics-engineer-at-acme",
        "company": "Acme",
        "title": "Analytics Engineer (dbt, BigQuery)",
        "hours": "10 - 20 hrs",
        "rate": "$100 - $150 / hr",
        "location": "Remote",
        "function": "Analytics",
        "date": "November 17, 2026",
        "description_html": "<p>Own our dbt models on BigQuery.</p>",
    }
    return RawPosting(source="fractionaljobs", source_id="x", payload=base | payload)


def _rule_input(job):
    return dict(
        company=job.company,
        title=job.title,
        description_text=job.description_text,
        location_text=job.location_text,
        tags=tuple(job.tags),
        salary_min=job.salary_min,
        salary_max=job.salary_max,
        salary_currency=job.salary_currency,
        salary_period=job.salary_period,
        salary_text=job.salary_text,
        employment_type=job.employment_type,
    )


def test_rules_keep_fractional_analytics_job_with_matching_stack():
    job = FractionalJobsConnector().normalise(_raw(), NOW)
    r = ev(**_rule_input(job))
    assert r.decision == "keep" and "part_time_ok" in r.flags


def test_rules_keep_fractional_job_without_stated_rate():
    job = FractionalJobsConnector().normalise(_raw(rate="Unknown"), NOW)
    r = ev(**_rule_input(job))
    assert r.decision == "keep" and "part_time_ok" in r.flags
