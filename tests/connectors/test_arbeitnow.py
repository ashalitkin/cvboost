from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from cvboost.connectors.arbeitnow import ArbeitnowConnector
from cvboost.http import HttpClient
from cvboost.models import RawPosting
from tests.conftest import FIXTURES, PUBLIC_RESOLVER

BODY = (FIXTURES / "arbeitnow" / "response.json").read_bytes()
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def test_first_record_exact_values():
    c = ArbeitnowConnector()
    raws = c.parse(BODY, "x")
    assert len(raws) == 8
    job = c.normalise(raws[2], NOW)
    assert job.source_id == "senior-it-security-engineer-berlin-242684"
    assert job.url == (
        "https://www.arbeitnow.com/jobs/companies/noris-network-ag/"
        "senior-it-security-engineer-berlin-242684"
    )
    assert (
        job.company == "noris network AG" and job.title == "(Senior) IT Security Engineer (m/w/d)"
    )
    assert job.location_text == "Berlin" and job.remote is True
    assert job.employment_type == "full_time"  # 'Unbefristeter arbeitsvertrag' is permanent
    assert job.tags == ["Remote", "IT Security"]
    assert job.posted_at == datetime(2026, 11, 6, 16, 30, 49, tzinfo=UTC)


def test_remote_false_is_kept_as_false():
    c = ArbeitnowConnector()
    assert c.normalise(c.parse(BODY, "x")[0], NOW).remote is False


@pytest.mark.parametrize(
    ("types", "expected"),
    [
        (["Freelance"], "freelance"),
        (["Freiberuflich / selbstständig"], "freelance"),
        (["Indépendant"], "freelance"),
        (["Contract"], "contract"),
        (["Befristet"], "contract"),
        (["Interim"], "contract"),
        (["Praktikum"], "internship"),
        (["Werkstudent"], "internship"),
        (["Part-time"], "part_time"),
        (["Teilzeit"], "part_time"),
        (["Minijob"], "part_time"),
        (["Vollzeit"], "full_time"),
        (["Full-time permanent", "mid"], "full_time"),
        (["Unbefristeter arbeitsvertrag"], "full_time"),
        (["Permanent contract"], "full_time"),
        (["CDI"], "full_time"),
        (["Vollzeit", "Werkstudent"], "internship"),  # rule order: internship before full time
        (["Befristet", "Vollzeit"], "contract"),
        (["Ehrenamt"], "other"),
        ([], None),
    ],
)
def test_job_types_table(types, expected):
    raw = RawPosting(
        source="arbeitnow",
        source_id="s",
        payload={"slug": "s", "url": "https://www.arbeitnow.com/jobs/s", "company_name": "A",
                 "title": "T", "job_types": types},
    )  # fmt: skip
    assert ArbeitnowConnector().normalise(raw, NOW).employment_type == expected


def test_only_one_request_even_when_links_next_is_present():
    assert json.loads(BODY)["links"]["next"]  # the fixture really has a next page
    seen = []

    def handler(req):
        seen.append(req.url)
        return httpx.Response(200, content=BODY)

    http = HttpClient(httpx.MockTransport(handler), sleep=lambda s: None, resolver=PUBLIC_RESOLVER)
    ArbeitnowConnector().fetch(http)
    assert [str(u) for u in seen] == ["https://www.arbeitnow.com/api/job-board-api"]
