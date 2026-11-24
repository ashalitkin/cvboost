from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from cvboost.connectors.himalayas import QUERIES, HimalayasConnector
from cvboost.http import HttpClient
from cvboost.models import RawPosting
from tests.conftest import FIXTURES, PUBLIC_RESOLVER

BODY = (FIXTURES / "himalayas" / "response.json").read_bytes()
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
GUID = (
    "https://himalayas.app/companies/general-dynamics-information-technology"
    "/jobs/cybersecurity-engineer-11199720"
)


def test_first_record_exact_values():
    c = HimalayasConnector()
    raws = c.parse(BODY, "x")
    assert len(raws) == 8
    job = c.normalise(raws[0], NOW)
    assert job.source_id == GUID and job.url == GUID
    assert job.company == "General Dynamics Information Technology"
    assert job.title == "Cybersecurity Engineer"
    assert job.location_text == "United States"
    assert job.remote is True and job.employment_type == "full_time"
    assert (job.salary_min, job.salary_max) == (111155.0, 150385.0)
    assert (job.salary_currency, job.salary_period) == ("USD", "year")
    assert job.tags[:2] == ["Cybersecurity-Engineer", "Security-Data-Science"]
    assert job.posted_at == datetime(2026, 11, 6, 17, 23, 7, tzinfo=UTC)
    assert "expiryDate" in job.raw and "seniority" in job.raw


def test_apply_url_is_kept_exactly_with_utm_source():
    c = HimalayasConnector()
    for raw in c.parse(BODY, "x"):
        job = c.normalise(raw, NOW)
        assert job.apply_url == raw.payload["applicationLink"]
        assert job.apply_url.endswith("?utm_source=api")


def _raw(**payload):
    base = {"guid": GUID, "companyName": "Acme", "title": "T"}
    return RawPosting(source="himalayas", source_id=GUID, payload=base | payload)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Full Time", "full_time"),
        ("Part Time", "part_time"),
        ("Contractor", "contract"),
        ("Intern", "internship"),
        ("Temporary", "other"),
        (None, None),
    ],
)
def test_employment_type_table(value, expected):
    job = HimalayasConnector().normalise(_raw(employmentType=value), NOW)
    assert job.employment_type == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("annual", "year"), ("monthly", "month"), ("hourly", "hour"), ("daily", "day"), ("x", None)],
)
def test_salary_period_table(value, expected):
    assert HimalayasConnector().normalise(_raw(salaryPeriod=value), NOW).salary_period == expected


def test_apply_link_that_is_not_http_becomes_none():
    job = HimalayasConnector().normalise(_raw(applicationLink="javascript:alert(1)"), NOW)
    assert job.apply_url is None


def test_empty_location_list_is_none():
    assert HimalayasConnector().normalise(_raw(locationRestrictions=[]), NOW).location_text is None


def test_requests_one_per_query_and_next_url_increments_page():
    c = HimalayasConnector()
    urls = c.requests()
    assert len(urls) == len(QUERIES) == 8
    assert urls[0] == (
        "https://himalayas.app/jobs/api/search?q=analytics%20engineer&sort=recent&page=1"
    )
    nxt = c.next_url(_full_page(1), urls[0], 1)
    assert nxt == "https://himalayas.app/jobs/api/search?q=analytics%20engineer&sort=recent&page=2"


def test_next_url_none_on_empty_jobs():
    assert HimalayasConnector().next_url(json.dumps({"jobs": []}).encode(), "x", 1) is None


def _http(handler):
    return HttpClient(httpx.MockTransport(handler), sleep=lambda s: None, resolver=PUBLIC_RESOLVER)


def _full_page(page: int) -> bytes:
    """20 distinct jobs (a full page) built from the fixture."""
    base = json.loads(BODY)["jobs"]
    jobs = []
    for i in range(20):
        job = dict(base[i % len(base)])
        job["guid"] = f"{job['guid']}-p{page}-{i}"
        jobs.append(job)
    return json.dumps({"jobs": jobs}).encode()


def test_fetch_two_pages_per_query_all_on_himalayas_host():
    seen = []

    def handler(req):
        seen.append(req.url)
        return httpx.Response(200, content=_full_page(int(req.url.params["page"])))

    raws = HimalayasConnector().fetch(_http(handler))
    assert len(seen) == 16 and {u.host for u in seen} == {"himalayas.app"}
    assert [u.params["page"] for u in seen[:2]] == ["1", "2"]
    assert len(raws) == 40  # 2 pages x 20 guids, repeated across queries: deduplicated


def test_fetch_stops_after_empty_page():
    seen = []

    def handler(req):
        seen.append(req.url)
        return httpx.Response(200, content=b'{"jobs": []}')

    HimalayasConnector().fetch(_http(handler))
    assert len(seen) == 8 and all(u.params["page"] == "1" for u in seen)


def test_fetch_stops_when_page_is_not_full():
    seen = []

    def handler(req):
        seen.append(req.url)
        return httpx.Response(200, content=BODY)  # 8 jobs < 20: last page

    HimalayasConnector().fetch(_http(handler))
    assert len(seen) == 8


def test_fetch_limit_stops_after_first_request():
    seen = []

    def handler(req):
        seen.append(req.url)
        return httpx.Response(200, content=_full_page(1))

    raws = HimalayasConnector().fetch(_http(handler), limit=3)
    assert len(seen) == 1 and len(raws) == 3


def test_next_url_is_not_called_beyond_max_pages(monkeypatch):
    c = HimalayasConnector()
    calls = []
    real = c.next_url
    monkeypatch.setattr(c, "next_url", lambda *a: calls.append(a[2]) or real(*a))
    c.fetch(_http(lambda req: httpx.Response(200, content=_full_page(1))))
    assert calls and set(calls) == {1}  # asked after page 1 only; page 2 == max_pages


def test_fetch_one_page_when_max_pages_is_one():
    seen = []
    c = HimalayasConnector()
    c.max_pages = 1
    c.fetch(_http(lambda req: seen.append(1) or httpx.Response(200, content=BODY)))
    assert len(seen) == 8


def test_currency_dropped_without_salary():
    job = HimalayasConnector().normalise(_raw(currency="USD"), NOW)
    assert job.salary_currency is None
    job = HimalayasConnector().normalise(_raw(currency="USD", minSalary=10), NOW)
    assert job.salary_currency == "USD"
