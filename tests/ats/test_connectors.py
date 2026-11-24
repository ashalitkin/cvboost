"""Parse + normalise on the saved responses of real companies (one live call each)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cvboost.connectors.ashby import AshbyConnector
from cvboost.connectors.ats import ATSConnector, title_matches
from cvboost.connectors.greenhouse import GreenhouseConnector
from cvboost.connectors.lever import LeverConnector
from tests.conftest import FIXTURES

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)

CASES = [
    (GreenhouseConnector, "greenhouse", "circleci", 6, 3),
    (LeverConnector, "lever", "findem", 7, 3),
    (AshbyConnector, "ashby", "linear", 6, 3),
    (AshbyConnector, "ashby", "luxor", 4, 0),
]


def body(ats: str, slug: str) -> bytes:
    return (FIXTURES / ats / f"{slug}.json").read_bytes()


@pytest.mark.parametrize(("cls", "ats", "slug", "total", "kept"), CASES)
def test_every_fixture_record_normalises(cls, ats, slug, total, kept):
    c: ATSConnector = cls()
    url = c.url_for(slug)
    raws = c.parse_all(body(ats, slug), url)
    assert len(raws) == total
    for raw in raws:
        job = c.normalise(raw, NOW)
        assert job.source == ats and job.source_id and job.title
        assert job.url.startswith(("http://", "https://")) and job.company
        assert job.posted_at is not None and job.posted_at.tzinfo is not None
        assert job.description_text and "<" not in job.description_text[:50]
        assert job.apply_url is None or job.apply_url.startswith(("http://", "https://"))
    # the prefilter keeps exactly the matching titles
    kept_ids = {r.source_id for r in c.parse(body(ats, slug), url)}
    expected = {r.source_id for r in raws if title_matches(c.posting_title(r.payload))}
    assert kept_ids == expected and len(kept_ids) == kept


def test_greenhouse_fields():
    c = GreenhouseConnector()
    [job] = [
        c.normalise(r, NOW)
        for r in c.parse(body("greenhouse", "circleci"), c.url_for("circleci"))
        if r.source_id == "8801094002"
    ]
    assert job.company == "CircleCI" and job.title == "Agentic Operator, Growth Marketing"
    assert job.location_text == "Remote (USA)" and job.remote is True
    assert job.url == "http://www.circleci.com/careers/jobs/8801094002/?gh_jid=8801094002"
    assert job.posted_at == datetime(2026, 10, 6, 20, 30, 43, tzinfo=UTC)
    assert "&lt;" not in job.description_html  # the escaped HTML is unescaped
    assert job.description_text.startswith("About the Role")
    assert "Marketing" in job.tags


def test_lever_fields_and_salary():
    c = LeverConnector()
    jobs = {
        r.payload["text"]: c.normalise(r, NOW)
        for r in c.parse_all(body("lever", "findem"), c.url_for("findem"))
    }
    j = jobs["Contract Technical Recruiter"]
    assert j.url == "https://jobs.lever.co/findem/5a9e3fae-86b6-4a83-9da3-aa60f864d71a"
    assert j.apply_url.endswith("/apply") and j.company == "findem"
    assert j.employment_type == "contract" and j.remote is False
    assert (j.salary_min, j.salary_max, j.salary_currency, j.salary_period) == (
        6000, 6000, "USD", "month",
    )  # fmt: skip
    assert j.posted_at == datetime.fromtimestamp(1792788085.592, UTC)
    assert "Findem" in j.description_text and "What You'll Do" in j.description_text
    assert jobs["Senior Accountant"].salary_min is None


def test_ashby_compensation_becomes_salary():
    c = AshbyConnector()
    jobs = {
        r.payload["title"]: c.normalise(r, NOW)
        for r in c.parse_all(body("ashby", "linear"), c.url_for("linear"))
    }
    j = jobs["Analytics Engineer"]
    assert (j.salary_min, j.salary_max, j.salary_currency, j.salary_period) == (
        180000, 220000, "USD", "year",
    )  # fmt: skip
    assert j.salary_text == "$180K – $220K • Offers Equity"
    assert j.employment_type == "full_time" and j.url.startswith("https://jobs.ashbyhq.com/linear/")
    deal = jobs["Deal Desk"]
    assert deal.salary_min is None and deal.salary_period is None and deal.salary_text is None


def test_ashby_skips_unlisted():
    c = AshbyConnector()
    import json

    data = json.loads(body("ashby", "luxor"))
    data["jobs"][0]["isListed"] = False
    raws = c.parse_all(json.dumps(data).encode(), c.url_for("luxor"))
    assert len(raws) == 3


def test_company_name_comes_from_the_list_else_the_slug():
    c = LeverConnector()
    c._names = {"findem": "Findem Inc"}
    raws = c.parse_all(body("lever", "findem"), c.url_for("findem"))
    assert c.normalise(raws[0], NOW).company == "Findem Inc"
    c._names = {}
    raws = c.parse_all(body("lever", "findem"), c.url_for("findem"))
    assert c.normalise(raws[0], NOW).company == "findem"


@pytest.mark.parametrize("cls", [GreenhouseConnector, LeverConnector, AshbyConnector])
def test_urls_use_the_fixed_host_and_slug_only_in_the_path(cls):
    c = cls()
    url = c.url_for("acme-1")
    from urllib.parse import urlsplit

    p = urlsplit(url)
    assert p.hostname in c.allowed_hosts and p.scheme == "https"
    assert c.slug_of(url) == "acme-1"
    assert c.kind == "ats" and c.default_enabled


@pytest.mark.parametrize("cls", [GreenhouseConnector, LeverConnector, AshbyConnector])
@pytest.mark.parametrize(
    "slug",
    [
        "../x",
        "a/b",
        "A",
        "acme\n",
        "a b",
        "",
        "x" * 65,
        "acme?x=1",
        "acme#",
        "é",
        "a@evil.example.com",
    ],
)
def test_bad_slug_is_rejected(cls, slug):
    with pytest.raises(ValueError):
        cls().url_for(slug)


@pytest.mark.parametrize(
    ("title", "ok"),
    [
        ("Senior Data Engineer", True),
        ("Analytics Engineer", True),
        ("BI Developer", True),
        ("Head of Growth", True),
        ("ML/AI Engineer", True),
        ("Performance Marketing Manager", True),
        ("Looker Developer", True),
        ("Tracking & Attribution Lead", True),
        ("Machine Learning Researcher", True),
        ("dbt Developer", True),
        ("Accountant", False),
        ("Maintenance Technician", False),  # "ai" inside a word
        ("Database Administrator", False),  # "data" inside a word
        ("Trainer", False),
        ("Senior Software Engineer", False),
        ("", False),
        (None, False),
    ],
)
def test_title_prefilter(title, ok):
    assert title_matches(title) is ok
