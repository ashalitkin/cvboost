from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from cvboost.connectors.jobicy import JobicyConnector
from cvboost.http import HttpClient
from cvboost.models import RawPosting
from tests.conftest import FIXTURES, PUBLIC_RESOLVER

BODY = (FIXTURES / "jobicy" / "response.json").read_bytes()
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def test_first_record_exact_values():
    c = JobicyConnector()
    raws = c.parse(BODY, "x")
    assert len(raws) == 8
    job = c.normalise(raws[1], NOW)
    assert job.source_id == "159311"
    assert job.url == "https://jobicy.com/jobs/159311-senior-product-analyst-data-scientist"
    assert job.company == "Nebius" and job.title == "Senior Product Analyst / Data Scientist"
    assert job.location_text == "Europe" and job.remote is True
    assert job.employment_type == "full_time"
    assert job.tags == ["Data Science & Analytics"]
    assert job.posted_at == datetime(2026, 11, 6, 14, 6, 45, tzinfo=UTC)
    assert job.apply_url is None


def test_location_whitespace_is_collapsed():
    job = JobicyConnector().normalise(JobicyConnector().parse(BODY, "x")[3], NOW)
    assert job.location_text == "UK, USA"


def _raw(**payload):
    base = {"id": 1, "url": "https://jobicy.com/jobs/1-x", "jobTitle": "T", "companyName": "A"}
    return RawPosting(source="jobicy", source_id="1", payload=base | payload)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (["Full-Time"], "full_time"),
        (["Part-Time"], "part_time"),
        (["Contract"], "contract"),
        (["Internship"], "internship"),
        (["Freelance"], "freelance"),
        (["Seasonal", "Contract"], "contract"),
        (["Seasonal"], "other"),
        ([], None),
    ],
)
def test_job_type_table(value, expected):
    assert JobicyConnector().normalise(_raw(jobType=value), NOW).employment_type == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("yearly", "year"), ("monthly", "month"), ("daily", "day"), ("hourly", "hour"), ("x", None)],
)
def test_salary_period_table(value, expected):
    job = JobicyConnector().normalise(_raw(salaryMin=10, salaryMax=20, salaryPeriod=value), NOW)
    assert job.salary_period == expected and (job.salary_min, job.salary_max) == (10.0, 20.0)


def test_empty_company_makes_the_posting_invalid():
    with pytest.raises(ValueError):
        JobicyConnector().normalise(_raw(companyName=""), NOW)


def test_next_url_url_encodes_the_cursor_and_keeps_count():
    c = JobicyConnector()
    body = json.dumps({"jobs": [], "nextCursor": "ab+/=c d"}).encode()
    assert (
        c.next_url(body, c.base_url, 1)
        == "https://jobicy.com/api/jobs?count=100&cursor=ab%2B%2F%3Dc%20d"
    )


def test_next_url_none_when_cursor_is_null():
    c = JobicyConnector()
    assert c.next_url(json.dumps({"jobs": [], "nextCursor": None}).encode(), c.base_url, 1) is None


def test_fixture_cursor_round_trips():
    c = JobicyConnector()
    cursor = json.loads(BODY)["nextCursor"]
    nxt = c.next_url(BODY, c.base_url, 1)
    assert nxt.startswith("https://jobicy.com/api/jobs?count=100&cursor=")
    assert httpx.URL(nxt).params["cursor"] == cursor


def test_fetch_makes_at_most_two_requests():
    seen = []

    def handler(req):
        seen.append(req.url)
        return httpx.Response(200, content=BODY)  # always has a nextCursor

    http = HttpClient(httpx.MockTransport(handler), sleep=lambda s: None, resolver=PUBLIC_RESOLVER)
    JobicyConnector().fetch(http)
    assert len(seen) == 2 and {u.host for u in seen} == {"jobicy.com"}
    assert "cursor" in seen[1].params and seen[1].params["count"] == "100"


def test_currency_dropped_without_salary():
    assert JobicyConnector().normalise(_raw(salaryCurrency="USD"), NOW).salary_currency is None
    job = JobicyConnector().normalise(_raw(salaryCurrency="USD", salaryMax=5), NOW)
    assert job.salary_currency == "USD"
