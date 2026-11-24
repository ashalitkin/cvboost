from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from cvboost.collect import collect_source
from cvboost.connectors.hn_hiring import HNHiringConnector, first_line, parse_first_line
from cvboost.http import HttpClient
from cvboost.models import RawPosting
from tests.conftest import FIXTURES, NOW, PUBLIC_RESOLVER

SEARCH = (FIXTURES / "hn_hiring" / "search.json").read_bytes()
COMMENTS = (FIXTURES / "hn_hiring" / "comments.json").read_bytes()


def conn(now=NOW):
    return HNHiringConnector(now=lambda: now)


def test_thread_pick_ignores_who_wants_to_be_hired():
    assert conn().pick_thread(SEARCH) == "50338728"


def test_thread_older_than_40_days_gives_nothing():
    assert conn(NOW + timedelta(days=60)).pick_thread(SEARCH) is None


def test_no_matching_thread_gives_nothing():
    body = json.dumps({"hits": [{"title": "Ask HN: Who wants to be hired?"}]}).encode()
    assert conn().pick_thread(body) is None


def test_replies_and_deleted_comments_are_skipped_in_parse():
    raws = conn().parse(COMMENTS, "x")
    total = len(json.loads(COMMENTS)["hits"])
    assert len(raws) == 15 and total == 17  # 2 replies in the fixture
    extra = {
        "objectID": "1", "story_id": 5, "parent_id": 5, "comment_text": None, "created_at_i": 1
    }  # fmt: skip
    body = json.dumps({"hits": [extra]}).encode()
    assert conn().parse(body, "x") == []


def test_entities_are_decoded_and_apply_url_is_never_taken():
    c = conn()
    job = c.normalise(c.parse(COMMENTS, "x")[1], NOW)
    assert "&#x2F;" not in job.description_text and "netbird.io" in job.description_text
    assert job.apply_url is None
    assert job.url == "https://news.ycombinator.com/item?id=50392398"


def test_posted_at_is_the_comment_time_not_the_thread_time():
    c = conn()
    raw = c.parse(COMMENTS, "x")[0]
    job = c.normalise(raw, NOW)
    assert job.posted_at == datetime.fromtimestamp(raw.payload["created_at_i"], UTC)
    assert job.posted_at == datetime(2026, 11, 6, 15, 8, 7, tzinfo=UTC)


FIRST_LINES = [
    (
        "Acme | Data Engineer | Remote (International)",
        ("Acme", "Data Engineer", "Remote (International)", True, None, None),
    ),
    (
        "Acme | Victoria, BC | Senior Engineer",
        ("Acme", "Senior Engineer", "Victoria, BC", None, None, None),
    ),
    (
        "Machine Age | COO — founder-level role | Full-time | UK time zone | Salary post-raise",
        (
            "Machine Age",
            "COO — founder-level role",
            "UK time zone",
            None,
            "full_time",
            "Salary post-raise",
        ),
    ),
    (
        "NetBird | Berlin, Germany (or remote, CET overlap) | Full-time",
        (
            "NetBird",
            "NetBird | Berlin, Germany (or remote, CET overlap) | Full-time",
            "Berlin, Germany (or remote, CET overlap)",
            True,
            "full_time",
            None,
        ),
    ),
    (
        "Odette Technologies | Senior Integration Engineer, Data/ML Engineer, Product Lead | "
        "HYBRID (NC) / REMOTE | Full-time | US citizens, clearable",
        (
            "Odette Technologies",
            "Senior Integration Engineer, Data/ML Engineer, Product Lead",
            "HYBRID (NC) / REMOTE | US citizens, clearable",
            True,
            "full_time",
            None,
        ),
    ),
    (
        "Piramidal (YC W24) | Senior Backend Engineer | New York, NY | $200K-$300K + equity | "
        "www.piramidal.ai",
        (
            "Piramidal (YC W24)",
            "Senior Backend Engineer",
            "New York, NY",
            None,
            None,
            "$200K-$300K + equity",
        ),
    ),
    (
        "Acme | Data Engineer | Onsite, Berlin | Contract",
        ("Acme", "Data Engineer", "Onsite, Berlin", False, "contract", None),
    ),
    (
        "Acme | Analyst | Remote (no remote in the US) | Part-time",
        ("Acme", "Analyst", "Remote (no remote in the US)", None, "part_time", None),
    ),
    (
        "Acme | Analyst | NOT REMOTE, Austin, TX | Internship",
        ("Acme", "Analyst", "NOT REMOTE, Austin, TX", None, "internship", None),
    ),
    (
        "Acme | Platform | Worldwide | 120k-150k | visa sponsorship",
        ("Acme", "Platform", "Worldwide", None, None, "120k-150k"),
    ),
    (
        "Acme | https://acme.example.com/jobs",
        ("Acme", "Acme | https://acme.example.com/jobs", None, None, None, None),
    ),
]


@pytest.mark.parametrize(("line", "expected"), FIRST_LINES)
def test_first_line_table(line, expected):
    f = parse_first_line(line)
    got = (
        f["company"], f["title"], f["location_text"], f["remote"], f["employment_type"],
        f["salary_text"],
    )  # fmt: skip
    assert got == expected


def test_remote_flag_rules():
    assert parse_first_line("A | Engineer | Remote")["remote"] is True
    assert parse_first_line("A | Engineer | No remote, Berlin")["remote"] is None
    assert parse_first_line("A | Engineer | On-site Berlin")["remote"] is False
    assert parse_first_line("A | Engineer | Berlin")["remote"] is None


def test_comment_without_pipe_is_invalid():
    raw = RawPosting(
        source="hn_hiring",
        source_id="9",
        payload={
            "comment_text": "We are hiring a data engineer<p>apply by mail",
            "created_at_i": 1,
        },
    )
    with pytest.raises(ValueError):
        conn().normalise(raw, NOW)


@pytest.mark.parametrize(
    "text", ["I'm Sushant Gangurde, A Software Engineer", ".", "A |", "x" * 90 + " | Engineer"]
)
def test_other_invalid_shapes(text):
    with pytest.raises(ValueError):
        parse_first_line(first_line(text))


def test_every_fixture_record_is_valid_and_company_never_unknown():
    c = conn()
    for raw in c.parse(COMMENTS, "x"):
        job = c.normalise(raw, NOW)
        assert job.company and job.company != "Unknown"


def _http(handler):
    return HttpClient(httpx.MockTransport(handler), sleep=lambda s: None, resolver=PUBLIC_RESOLVER)


def _search_hit_body(created_at_i):
    return json.dumps(
        {"hits": [{"title": "Ask HN: Who is hiring? (X)", "objectID": "7",
                   "created_at_i": created_at_i}]}
    ).encode()  # fmt: skip


def _hit(oid, text, parent=7):
    return {"objectID": oid, "story_id": 7, "parent_id": parent, "comment_text": text,
            "created_at_i": int(NOW.timestamp())}  # fmt: skip


def test_fetch_makes_two_requests_both_on_algolia():
    seen = []

    def handler(req):
        seen.append(req.url)
        if "story_50338728" in str(req.url):
            return httpx.Response(200, content=COMMENTS)
        return httpx.Response(200, content=SEARCH)

    raws = conn().fetch(_http(handler))
    assert len(seen) == 2 and {u.host for u in seen} == {"hn.algolia.com"}
    assert "story_50338728" in str(seen[1]) and len(raws) == 15


def test_fetch_old_thread_is_empty_after_one_request():
    seen = []

    def handler(req):
        seen.append(1)
        return httpx.Response(200, content=SEARCH)

    assert conn(NOW + timedelta(days=90)).fetch(_http(handler)) == []
    assert len(seen) == 1


def test_fetch_reads_at_most_three_comment_pages():
    seen = []

    def handler(req):
        seen.append(req.url)
        if "story_7" in str(req.url):
            body = {"nbPages": 9, "hits": [_hit(str(len(seen)), "A | Engineer | Remote")]}
            return httpx.Response(200, content=json.dumps(body).encode())
        return httpx.Response(200, content=_search_hit_body(int(NOW.timestamp())))

    raws = conn().fetch(_http(handler))
    assert len(seen) == 1 + 3 and len(raws) == 3


def test_collect_two_valid_one_without_pipe(store):
    store.sync_sources({"hn_hiring": HNHiringConnector}, NOW)
    hits = [
        _hit("1", "Acme | Data Engineer | Remote | Full-time<p>text"),
        _hit("2", "We are hiring a data engineer<p>mail me"),
        _hit("3", "Beta | Analyst | Berlin"),
    ]

    def handler(req):
        if "story_7" in str(req.url):
            return httpx.Response(200, content=json.dumps({"nbPages": 1, "hits": hits}).encode())
        return httpx.Response(200, content=_search_hit_body(int(NOW.timestamp())))

    res = collect_source(store, _http(handler), conn(), limit=None, now=NOW)
    assert (res.status, res.found, res.invalid, res.new) == ("ok", 3, 1, 2)
    rows = store.conn.execute("SELECT company FROM jobs ORDER BY company").fetchall()
    assert [r[0] for r in rows] == ["Acme", "Beta"]


def test_title_decodes_html_entities():
    c = conn()
    jobs = {r.source_id: c.normalise(r, NOW) for r in c.parse(COMMENTS, "x")}
    assert "Data/ML Engineer" in jobs["50385049"].title
