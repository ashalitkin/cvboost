from __future__ import annotations

from datetime import UTC, datetime

from cvboost.connectors.remotive import RemotiveConnector
from tests.conftest import FIXTURES

BODY = (FIXTURES / "remotive" / "response.json").read_bytes()
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def test_enabled_by_owner_decision():
    assert RemotiveConnector.default_enabled is True


def test_parse_and_normalise_first():
    c = RemotiveConnector()
    raws = c.parse(BODY, c.base_url)
    assert len(raws) == 5
    job = c.normalise(raws[0], NOW)
    assert job.source == "remotive" and job.source_id == "1185979"
    assert job.url.startswith("https://remotive.com/remote-jobs/")
    assert job.title == "Freelance Writer" and job.company == "IAPWE"
    assert job.posted_at == datetime(2026, 11, 4, 16, 53, 35, tzinfo=UTC)
    assert job.employment_type == "freelance" and job.remote is True
    assert job.salary_text == "$50-$75 /hour"
    assert job.description_text and "<" not in job.description_text


def test_all_normalise_and_tags_stripped():
    c = RemotiveConnector()
    jobs = [c.normalise(r, NOW) for r in c.parse(BODY, c.base_url)]
    assert {j.employment_type for j in jobs} >= {"freelance", "contract", "full_time"}
    assert all(t == t.strip() for j in jobs for t in j.tags)
