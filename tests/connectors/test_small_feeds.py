from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from defusedxml.common import DefusedXmlException

from cvboost.connectors._util import rss_items
from cvboost.connectors.hn_jobs import HNJobsConnector
from cvboost.connectors.jobindex_dk import JobindexDKConnector
from cvboost.connectors.jobtech_se import JobTechSEConnector
from cvboost.connectors.mastodon import MastodonConnector, looks_like_job_post
from cvboost.connectors.realworkfromanywhere import RealWorkFromAnywhereConnector
from cvboost.connectors.remotefirstjobs import RemoteFirstJobsConnector
from cvboost.http import HttpClient
from tests.conftest import FIXTURES, PUBLIC_RESOLVER

NOW = datetime(2026, 11, 8, 18, 0, tzinfo=UTC)


def _read(src: str, name: str) -> bytes:
    return (FIXTURES / src / name).read_bytes()


def _jobs(c, body):
    return [c.normalise(r, NOW) for r in c.parse(body, "x")]


def test_remotefirstjobs_every_item_parses():
    c, body = RemoteFirstJobsConnector(), _read("remotefirstjobs", "all.xml")
    jobs = _jobs(c, body)
    assert len(jobs) == len(rss_items(body)) >= 10
    j = jobs[0]
    assert j.source_id.endswith("-880872") or j.source_id
    assert j.url.startswith("https://remotefirstjobs.com/companies/")
    assert j.company and j.title and " at " not in j.title[-len(j.company) - 5 :]
    assert j.remote is True and j.posted_at.tzinfo is not None
    assert any(x.location_text for x in jobs) and any(x.employment_type for x in jobs)


def test_remotefirstjobs_title_without_company_is_rejected():
    c = RemoteFirstJobsConnector()
    raw = c.parse(_read("remotefirstjobs", "all.xml"), "x")[0]
    with pytest.raises(ValueError):
        c.normalise(raw.model_copy(update={"payload": {"title": "No company"}}), NOW)


def test_remotefirstjobs_requests_stay_on_host():
    assert all(
        u.startswith("https://remotefirstjobs.com/rss/")
        for u in RemoteFirstJobsConnector().requests()
    )


def test_realworkfromanywhere_every_item_parses():
    c, body = RealWorkFromAnywhereConnector(), _read("realworkfromanywhere", "rss.xml")
    jobs = _jobs(c, body)
    assert len(jobs) == body.count(b"<item>") >= 10  # the raw feed is not well-formed XML
    j = jobs[0]
    assert j.title == "Marketing Operations Manager (fixed term)"
    assert j.company == "SafetyWing"
    assert j.source_id == "marketing-operations-manager-fixed-term-safetywing-1054"
    assert j.remote is True and "<" not in j.description_text


def test_hn_jobs_every_item_parses_and_filters_dead():
    c = HNJobsConnector()
    items = json.loads(_read("hn_jobs", "items.json"))
    assert len(items) == 12
    kept = 0
    for item in items:
        for raw in c.parse(json.dumps(item).encode(), "x"):
            j = c.normalise(raw, NOW)
            kept += 1
            assert j.url == f"https://news.ycombinator.com/item?id={j.source_id}"
            assert j.company and j.title and j.posted_at is not None
    assert kept == 12
    first = c.normalise(c.parse(json.dumps(items[0]).encode(), "x")[0], NOW)
    assert first.company == "UpCodes" and first.apply_url.startswith("https://up.codes/")
    assert c.parse(json.dumps({**items[0], "dead": True}).encode(), "x") == []
    assert c.parse(json.dumps({**items[0], "type": "story"}).encode(), "x") == []
    assert c.parse(b"null", "x") == []


def test_hn_jobs_title_without_hiring_uses_url_host_and_javascript_url_is_dropped():
    c = HNJobsConnector()
    base = {"id": 1, "type": "job", "time": 1793984613, "title": "Founding Engineer"}
    raw = c.parse(json.dumps({**base, "url": "https://www.example.com/jobs"}).encode(), "x")[0]
    assert c.normalise(raw, NOW).company == "example.com"
    raw = c.parse(json.dumps({**base, "url": "javascript:alert(1)"}).encode(), "x")[0]
    with pytest.raises(ValueError):
        c.normalise(raw, NOW)


def test_hn_jobs_fetch_follows_ids():
    c = HNJobsConnector()
    items = json.loads(_read("hn_jobs", "items.json"))
    by_id = {i["id"]: i for i in items}
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path.endswith("jobstories.json"):
            return httpx.Response(200, json=list(by_id) + ["junk"])
        return httpx.Response(200, json=by_id[int(req.url.path.rsplit("/", 1)[-1][:-5])])

    http = HttpClient(httpx.MockTransport(handler), sleep=lambda s: None, resolver=PUBLIC_RESOLVER)
    assert len(c.fetch(http)) == 12
    assert len(c.fetch(http, limit=3)) == 3
    assert all(p.startswith("/v0/") for p in seen)


def test_jobtech_every_hit_parses():
    c = JobTechSEConnector()
    total = 0
    for f in ("data-analyst.json", "data-engineer.json"):
        body = _read("jobtech_se", f)
        jobs = _jobs(c, body)
        total += len(jobs)
        assert len(jobs) == len(json.loads(body)["hits"])
    assert total >= 8
    j = _jobs(c, _read("jobtech_se", "data-analyst.json"))[0]
    assert j.source_id == "31699947" and j.company == "Avaron AB" and j.title == "Data Analyst"
    assert j.url == "https://arbetsformedlingen.se/platsbanken/annonser/31699947"
    assert j.employment_type == "full_time" and "Solna" in j.location_text
    assert j.apply_url.startswith("https://jobs.avaron.se/")
    assert j.posted_at == datetime(2026, 11, 7, 8, 5, 58, tzinfo=UTC)


def test_jobtech_requests_are_encoded_and_removed_hits_skipped():
    c = JobTechSEConnector()
    assert c.requests()[0].endswith("?q=data%20analyst&limit=100")
    body = json.loads(_read("jobtech_se", "data-analyst.json"))
    body["hits"][0]["removed"] = True
    assert len(c.parse(json.dumps(body).encode(), "x")) == len(body["hits"]) - 1


def test_jobindex_every_item_parses():
    c = JobindexDKConnector()
    for f in ("analyst.xml", "engineer.xml"):
        body = _read("jobindex_dk", f)
        jobs = _jobs(c, body)
        assert len(jobs) == len(rss_items(body)) == 20
    j = _jobs(c, _read("jobindex_dk", "analyst.xml"))[0]
    assert j.source_id == "1710189" and j.company == "Sorø Kommune"
    assert j.url == "https://www.jobindex.dk/vis-job/h1710189"
    assert j.location_text == "Stenlille" and j.posted_at is not None


def test_jobindex_queries_respect_robots_plus_rule():
    for u in JobindexDKConnector().requests():
        query = u.split("?", 1)[1]
        assert query.count("+") <= 1 and "&" not in query


FEED_FILES = ["fedihire.xml", "hiring.xml", "dataanalytics.xml", "dataengineering.xml", "dbt.xml"]


def test_mastodon_every_item_is_kept_or_filtered_and_kept_ones_parse():
    c = MastodonConnector()
    kept = dropped = 0
    for f in FEED_FILES:
        body = _read("mastodon", f)
        items = rss_items(body)
        jobs = _jobs(c, body)
        kept += len(jobs)
        dropped += len(items) - len(jobs)
        for j in jobs:
            assert looks_like_job_post(j.description_text)
            assert j.company.startswith("@") and j.company.count("@") == 2
            assert j.url.startswith("https://") and j.title and j.posted_at is not None
    assert kept >= 10 and dropped >= 20  # links no longer count as job words


def test_mastodon_filter_examples():
    assert looks_like_job_post("We're hiring a Data Engineer, remote. #hiring")
    assert looks_like_job_post("Freelance dbt contractor wanted")
    assert not looks_like_job_post("Love this meme #hiring #jobs")  # tags alone prove nothing
    assert not looks_like_job_post("I'm looking for a job as data engineer #fedihire")
    assert not looks_like_job_post("Available for hire: analyst. #opentowork")
    assert not looks_like_job_post("My weekly newsletter on Iceberg")


def test_mastodon_types_and_remote():
    c = MastodonConnector()
    raw = c.parse(_read("mastodon", "hiring.xml"), "x")[0]
    html = "<p>Hiring: freelance analyst, remote</p>"
    j = c.normalise(raw.model_copy(update={"payload": {**raw.payload, "description": html}}), NOW)
    assert j.employment_type == "freelance" and j.remote is True


def test_mastodon_rejects_non_https_and_odd_links():
    c = MastodonConnector()
    body = _read("mastodon", "hiring.xml")
    bad = body.replace(b"https://mastodon.social/@", b"http://mastodon.social/@")
    bad = bad.replace(b"https://social.podnews.net/@", b"https://social.podnews.net/users/")
    assert len(c.parse(bad, "x")) < len(c.parse(body, "x"))


def test_same_post_in_two_feeds_is_one_posting():
    c = MastodonConnector()
    body = _read("mastodon", "hiring.xml")
    http = HttpClient(
        httpx.MockTransport(lambda r: httpx.Response(200, content=body)),
        sleep=lambda s: None,
        resolver=PUBLIC_RESOLVER,
    )
    assert len(c.fetch(http)) == len(c.parse(body, "x"))


@pytest.mark.parametrize(
    "c",
    [
        RemoteFirstJobsConnector(),
        RealWorkFromAnywhereConnector(),
        JobindexDKConnector(),
        MastodonConnector(),
    ],
)
def test_xml_entities_are_not_expanded(c):
    evil = b"""<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "boom">]><rss><channel>
    <item><title>&a;</title></item></channel></rss>"""
    with pytest.raises(DefusedXmlException):
        c.parse(evil, "x")
