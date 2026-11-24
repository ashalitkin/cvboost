from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cvboost.connectors.adzuna import AdzunaConnector
from cvboost.connectors.arbeitnow import ArbeitnowConnector
from cvboost.connectors.fractionaljobs import FractionalJobsConnector
from cvboost.connectors.himalayas import HimalayasConnector
from cvboost.connectors.hn_hiring import HNHiringConnector
from cvboost.connectors.itjobs import ITJobsConnector
from cvboost.connectors.jobicy import JobicyConnector
from cvboost.connectors.jobindex_dk import JobindexDKConnector
from cvboost.connectors.jobtech_se import JobTechSEConnector
from cvboost.connectors.mastodon import MastodonConnector
from cvboost.connectors.realworkfromanywhere import RealWorkFromAnywhereConnector
from cvboost.connectors.reed import ReedConnector
from cvboost.connectors.remotefirstjobs import RemoteFirstJobsConnector
from cvboost.connectors.remoteok import RemoteOKConnector
from cvboost.connectors.remotive import RemotiveConnector
from cvboost.connectors.workingnomads import WorkingNomadsConnector
from cvboost.connectors.wwr import WWRConnector
from tests.conftest import FIXTURES

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)

CASES = [
    ("adzuna", AdzunaConnector(), ["gb-search.json"]),
    ("reed", ReedConnector(), ["search.json"]),
    ("itjobs", ITJobsConnector(), ["search.json"]),
    ("remotive", RemotiveConnector(), ["response.json"]),
    ("remoteok", RemoteOKConnector(), ["response.json"]),
    ("himalayas", HimalayasConnector(), ["response.json"]),
    ("jobicy", JobicyConnector(), ["response.json"]),
    ("workingnomads", WorkingNomadsConnector(), ["response.json"]),
    ("fractionaljobs", FractionalJobsConnector(), ["home.html"]),
    ("arbeitnow", ArbeitnowConnector(), ["response.json"]),
    ("hn_hiring", HNHiringConnector(), ["comments.json"]),
    ("remotefirstjobs", RemoteFirstJobsConnector(), ["all.xml"]),
    ("realworkfromanywhere", RealWorkFromAnywhereConnector(), ["rss.xml"]),
    ("jobtech_se", JobTechSEConnector(), ["data-analyst.json", "data-engineer.json"]),
    ("jobindex_dk", JobindexDKConnector(), ["analyst.xml", "engineer.xml"]),
    (
        "mastodon",
        MastodonConnector(),
        ["fedihire.xml", "hiring.xml", "dataanalytics.xml", "dataengineering.xml", "dbt.xml"],
    ),
    (
        "wwr",
        WWRConnector(),
        ["remote-back-end-programming-jobs.xml", "remote-sales-and-marketing-jobs.xml"],
    ),
]


@pytest.mark.parametrize(("name", "connector", "files"), CASES, ids=[c[0] for c in CASES])
def test_every_fixture_record_has_required_fields(name, connector, files):
    total = 0
    for f in files:
        body = (FIXTURES / name / f).read_bytes()
        for raw in connector.parse(body, "x"):
            job = connector.normalise(raw, NOW)
            total += 1
            assert job.source == name
            assert job.source_id and job.url and job.title and job.company
            assert job.posted_at is not None and job.posted_at.tzinfo is not None
    assert total >= 5
