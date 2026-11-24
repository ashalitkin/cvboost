from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from cvboost.connectors.wwr import WWRConnector
from cvboost.http import HttpClient
from tests.conftest import FIXTURES, PUBLIC_RESOLVER

BACKEND = (FIXTURES / "wwr" / "remote-back-end-programming-jobs.xml").read_bytes()
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def test_parse_and_normalise_first():
    c = WWRConnector()
    raws = c.parse(BACKEND, "x")
    assert len(raws) == 5
    job = c.normalise(raws[0], NOW)
    assert job.source == "wwr"
    assert job.source_id == "vertican-senior-applied-ai-engineer-document-intelligence"
    assert job.company == "Vertican"
    assert job.title == "Senior Applied AI Engineer, Document Intelligence"
    assert job.url == "https://weworkremotely.com/remote-jobs/" + job.source_id
    assert job.employment_type == "full_time"
    assert job.location_text.startswith("Anywhere in the World")
    assert job.tags[:2] == ["Python", "Large Language Models (LLMs)"]
    assert "Agentic AI" in job.tags and not any(t.startswith("and ") for t in job.tags)
    assert job.posted_at is not None and job.posted_at.tzinfo is not None
    assert job.description_text and "<" not in job.description_text


def test_title_without_company_is_rejected():
    c = WWRConnector()
    raw = c.parse(BACKEND, "x")[0].model_copy(update={"payload": {"title": "No separator"}})
    with pytest.raises(ValueError):
        c.normalise(raw, NOW)


def test_requests_per_category():
    c = WWRConnector(["a-jobs", "b-jobs"])
    assert c.requests() == [
        "https://weworkremotely.com/categories/a-jobs.rss",
        "https://weworkremotely.com/categories/b-jobs.rss",
    ]


def test_same_guid_in_two_feeds_is_one_posting():
    c = WWRConnector(["x-jobs", "y-jobs"])
    http = HttpClient(
        httpx.MockTransport(lambda req: httpx.Response(200, content=BACKEND)),
        sleep=lambda s: None,
        resolver=PUBLIC_RESOLVER,
    )
    assert len(c.fetch(http)) == 5
    assert len(c.fetch(http, limit=2)) == 2


def test_xml_entities_are_not_expanded():
    evil = b"""<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "boom">]><rss><channel>
    <item><title>&a;</title></item></channel></rss>"""
    from defusedxml.common import DefusedXmlException

    with pytest.raises(DefusedXmlException):
        WWRConnector().parse(evil, "x")
