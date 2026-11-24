from __future__ import annotations

from datetime import UTC, datetime

from cvboost.connectors.workingnomads import WorkingNomadsConnector
from cvboost.models import RawPosting
from tests.conftest import FIXTURES

BODY = (FIXTURES / "workingnomads" / "response.json").read_bytes()
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def test_first_record_exact_values():
    c = WorkingNomadsConnector()
    raws = c.parse(BODY, "x")
    assert len(raws) == 8
    job = c.normalise(raws[1], NOW)
    assert job.source_id == "1994915"  # from /job/go/<id>/
    assert job.url == "https://www.workingnomads.com/job/go/1994915/"
    assert job.company == "Lemon.io" and job.title == "Technical Operations Lead"
    assert job.location_text == "Europe" and job.remote is True
    assert job.tags[:3] == ["Management", "python", "project management"]
    # -05:00 offset converted to UTC
    assert job.posted_at == datetime(2026, 11, 4, 13, 35, 22, tzinfo=UTC)
    assert job.employment_type is None and job.salary_min is None


def test_source_id_falls_back_to_url():
    c = WorkingNomadsConnector()
    body = b'[{"url": "https://www.workingnomads.com/jobs/odd-link", "title": "T"}]'
    (raw,) = c.parse(body, "x")
    assert raw.source_id == "https://www.workingnomads.com/jobs/odd-link"


def test_items_without_url_are_skipped():
    assert WorkingNomadsConnector().parse(b'[{"title": "T"}, {"url": ""}]', "x") == []


def test_payload_is_kept_raw():
    raw = WorkingNomadsConnector().parse(BODY, "x")[0]
    assert isinstance(raw, RawPosting) and "pub_date" in raw.payload
