"""Workable, Recruitee, Teamtailor, Personio, Gem (#24): every fixture record, URL policy, runs."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from cvboost import connectors
from cvboost.collect import collect
from cvboost.companies import ATS_NAMES, load_companies
from cvboost.connectors.ats import ATSConnector, title_matches
from cvboost.connectors.gem import GemConnector
from cvboost.connectors.personio import PersonioConnector
from cvboost.connectors.recruitee import RecruiteeConnector, employment_type
from cvboost.connectors.teamtailor import TeamtailorConnector
from cvboost.connectors.workable import WorkableConnector
from cvboost.models import RawPosting
from tests.ats.helpers import file_response, host_http, write_companies
from tests.conftest import FIXTURES, NOW

CASES = [
    # cls, ats, slug, extension, records in the fixture, kept by the title prefilter
    (WorkableConnector, "workable", "pricelabs", "json", 5, 2),
    (RecruiteeConnector, "recruitee", "impactbrands", "json", 5, 2),
    (TeamtailorConnector, "teamtailor", "merlindigitalpartner-1613753675", "xml", 8, 4),
    (PersonioConnector, "personio", "studyflix-gmbh", "xml", 8, 4),
    (GemConnector, "gem", "productboard", "json", 6, 3),
]
HOST_OF = {
    "workable": "apply.workable.com",
    "recruitee": "impactbrands.recruitee.com",
    "teamtailor": "merlindigitalpartner-1613753675.teamtailor.com",
    "personio": "studyflix-gmbh.jobs.personio.de",
    "gem": "api.gem.com",
}


def body(ats: str, slug: str, ext: str) -> bytes:
    return (FIXTURES / ats / f"{slug}.{ext}").read_bytes()


def jobs(cls, ats, slug, ext):
    c = cls()
    return c, [c.normalise(r, NOW) for r in c.parse_all(body(ats, slug, ext), c.url_for(slug))]


@pytest.mark.parametrize(("cls", "ats", "slug", "ext", "total", "kept"), CASES)
def test_every_fixture_record_normalises(cls, ats, slug, ext, total, kept):
    c: ATSConnector = cls()
    url = c.url_for(slug)
    raws = c.parse_all(body(ats, slug, ext), url)
    assert len(raws) == total
    assert len({r.source_id for r in raws}) == total  # one posting per id
    for raw in raws:
        job = c.normalise(raw, NOW)
        assert job.source == ats and job.source_id and job.title and job.company
        assert job.url.startswith("https://")
        assert job.posted_at is not None and job.posted_at.tzinfo is not None
        assert job.description_text and "<" not in job.description_text[:50]
        assert job.apply_url is None or job.apply_url.startswith(("http://", "https://"))
    # the shared title prefilter keeps exactly the matching titles
    kept_ids = {r.source_id for r in c.parse(body(ats, slug, ext), url)}
    expected = {r.source_id for r in raws if title_matches(c.posting_title(r.payload))}
    assert kept_ids == expected and len(kept_ids) == kept


@pytest.mark.parametrize(("cls", "ats", "slug", "ext", "total", "kept"), CASES)
def test_the_fixtures_are_small_and_the_connector_metadata_is_complete(
    cls, ats, slug, ext, total, kept
):
    assert len(body(ats, slug, ext)) <= 150 * 1024
    assert cls.name == ats and cls.kind == "ats" and cls.base_url.startswith("https://")
    assert cls.tos_note and cls.attribution and cls.min_delay_s >= 1.0
    assert cls.allowed_hosts and all(h == h.lower() and "/" not in h for h in cls.allowed_hosts)


@pytest.mark.parametrize(("cls", "ats", "slug", "ext", "total", "kept"), CASES)
def test_fetch_hosts_are_exactly_the_tenant_host(cls, ats, slug, ext, total, kept):
    c = cls()
    assert c.allowed_hosts_for(slug) == frozenset({HOST_OF[ats]})
    assert c.slug_of(c.url_for(slug)) == slug


@pytest.mark.parametrize("cls", [c[0] for c in CASES])
@pytest.mark.parametrize(
    "bad", ["", "A", "a.b", "a/b", "a b", "a@b", "x" * 65, "a\n", "é", "a:443"]
)
def test_a_bad_slug_never_reaches_a_url(cls, bad):
    with pytest.raises(ValueError):
        cls().url_for(bad)


def test_a_sub_domain_connector_refuses_a_foreign_url():
    for cls, url in (
        (RecruiteeConnector, "https://x.example.com/api/offers/"),
        (TeamtailorConnector, "https://teamtailor.com/jobs.rss"),
        (PersonioConnector, "https://jobs.personio.de/xml"),
        (WorkableConnector, "https://apply.workable.com/api/v3/accounts/x/jobs"),
    ):
        with pytest.raises(ValueError):
            cls().slug_of(url)


# ---- field mapping ---------------------------------------------------------------------------


def test_workable_merges_the_places_of_one_job():
    c = WorkableConnector()
    raws = c.parse_all(
        b'{"jobs": ['
        b'{"shortcode": "A1", "title": "Data Analyst", "city": "Porto", "country": "Portugal",'
        b' "telecommuting": true, "employment_type": "Contract", "published_on": "2026-11-01",'
        b' "description": "<p>x</p>"},'
        b'{"shortcode": "A1", "title": "Data Analyst", "city": "Lisbon", "country": "Portugal"},'
        b'{"title": "no id"}]}',
        c.url_for("acme"),
    )
    assert [r.source_id for r in raws] == ["A1"]
    job = c.normalise(raws[0], NOW)
    assert job.location_text == "Porto, Portugal; Lisbon, Portugal"
    assert job.remote is True and job.employment_type == "contract"
    assert job.url == "https://apply.workable.com/j/A1"
    assert job.posted_at == datetime(2026, 11, 1, tzinfo=UTC)


def test_recruitee_fields_salary_and_unpublished_offers():
    c = RecruiteeConnector()
    offers = (
        b'{"offers": ['
        b'{"id": 7, "status": "published", "title": "Data Engineer", "slug": "de",'
        b' "company_name": "Acme", "remote": true, "location": "Porto, Portugal",'
        b' "employment_type_code": "freelance", "description": "<p>Build</p>",'
        b' "requirements": "<ul><li>SQL</li></ul>", "published_at": "2026-10-30 16:32:58 UTC",'
        b' "salary": {"min": "5000", "max": 7000, "period": "month", "currency": "EUR"},'
        b' "translations": {"en": {"title": "x"}}},'
        b'{"id": 8, "status": "draft", "title": "Hidden data analyst"}]}'
    )
    [raw] = c.parse_all(offers, c.url_for("acme"))
    assert "translations" not in raw.payload  # bulky copies are dropped
    job = c.normalise(raw, NOW)
    assert job.url == "https://acme.recruitee.com/o/de" and job.company == "Acme"
    assert job.remote is True and job.employment_type == "freelance"
    assert (job.salary_min, job.salary_max, job.salary_currency, job.salary_period) == (
        5000.0, 7000.0, "EUR", "month",
    )  # fmt: skip
    assert "Requirements" in job.description_text and "SQL" in job.description_text
    assert job.posted_at == datetime(2026, 10, 30, 16, 32, 58, tzinfo=UTC)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("fulltime_permanent", "full_time"),
        ("parttime_temporary", "part_time"),
        ("freelance", "freelance"),
        ("fulltime_contractor", "contract"),
        ("internship", "internship"),
        ("volunteer", "other"),
        (None, None),
    ],
)
def test_recruitee_employment_types(code, expected):
    assert employment_type(code) == expected


def test_teamtailor_fields():
    _, all_jobs = jobs(TeamtailorConnector, "teamtailor", CASES[2][2], "xml")
    by_title = {j.title: j for j in all_jobs}
    j = by_title["Technical Manager (AI & Data)"]
    assert j.location_text == "Barcelona, Spain" and j.remote is False and j.tags == ["IA"]
    assert j.url == (
        "https://merlindigitalpartner-1613753675.teamtailor.com/jobs/8591241-technical-manager-ai-data"
    )
    assert j.posted_at == datetime(2026, 10, 22, 12, 50, 19, tzinfo=UTC)
    two = by_title["Data Strategy & Governance Manager(Consultoría Global)"]
    assert two.location_text == "Madrid, Spain; Barcelona, Spain"
    assert all(x.description_html.startswith("<") for x in all_jobs)  # the escaped HTML is decoded


def test_teamtailor_remote_status_and_places():
    c = TeamtailorConnector()
    rss = (
        b'<?xml version="1.0"?><rss version="2.0" xmlns:tt="https://teamtailor.com/locations">'
        b"<channel><item><title>Data Analyst</title>"
        b"<description>&lt;p&gt;Hi&lt;/p&gt;</description>"
        b"<pubDate>Tue, 03 Nov 2026 19:37:57 +0100</pubDate>"
        b"<link>https://a.teamtailor.com/jobs/1-x</link>"
        b"<remoteStatus>fully</remoteStatus><guid>g1</guid><tt:locations><tt:location>"
        b"<tt:name>HQ</tt:name><tt:city>Oslo</tt:city><tt:country>Norway</tt:country></tt:location>"
        b"</tt:locations><tt:department>Data</tt:department></item></channel></rss>"
    )
    [raw] = c.parse_all(rss, c.url_for("a"))
    job = c.normalise(raw, NOW)
    assert job.remote is True and job.location_text == "Oslo, Norway" and job.tags == ["Data"]
    assert job.description_text == "Hi"
    assert job.posted_at == datetime(2026, 11, 3, 18, 37, 57, tzinfo=UTC)


def test_personio_fields():
    _, all_jobs = jobs(PersonioConnector, "personio", CASES[3][2], "xml")
    j = next(x for x in all_jobs if x.title.startswith("Praktikum Data Analytics"))
    assert j.employment_type == "internship" and j.remote is True and j.tags == ["Product"]
    assert j.location_text == "Mannheim; remote; München; Regensburg"
    assert j.url == "https://studyflix-gmbh.jobs.personio.de/job/1118678"
    assert j.posted_at == datetime(2023, 5, 15, 14, 0, 50, tzinfo=UTC)
    assert j.description_html.startswith("<h3>Dein neuer Traumjob bei Studyflix</h3>")
    no_remote = next(x for x in all_jobs if x.title.startswith("Praktikum Marketing &"))
    assert no_remote.remote is None  # an office list without "remote" says nothing


def test_gem_fields():
    c, all_jobs = jobs(GemConnector, "gem", CASES[4][2], "json")
    j = next(x for x in all_jobs if x.title == "Staff AI Product Engineer")
    assert j.employment_type == "full_time" and j.remote is False
    assert j.location_text == "Prague, Czechia"
    assert j.url == "https://jobs.gem.com/productboard/8080046002"
    assert j.posted_at is not None and j.posted_at.year == 2025


# ---- wrong shapes and hostile bodies ----------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "data"),
    [
        (WorkableConnector, b'{"jobs": "x"}'),
        (WorkableConnector, b"[]"),
        (RecruiteeConnector, b'{"offers": {}}'),
        (GemConnector, b'{"jobs": []}'),
        (TeamtailorConnector, b"<html/>"),
        (PersonioConnector, b"<rss/>"),
        (PersonioConnector, b"not xml at all"),
        (
            TeamtailorConnector,
            b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss>&a;</rss>',
        ),
        (PersonioConnector, b'<!DOCTYPE x SYSTEM "http://127.0.0.1/evil.dtd"><workzag-jobs/>'),
    ],
)
def test_a_wrong_shape_or_a_hostile_xml_is_an_error_not_a_crash(cls, data):
    c = cls()
    with pytest.raises((ValueError, TypeError, KeyError, AttributeError, SyntaxError)):
        c.parse_all(data, c.url_for("acme"))


def test_billion_laughs_is_refused_and_counted_as_a_failed_company(store, tmp_path):
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
        b"<workzag-jobs>&b;</workzag-jobs>"
    )
    path = write_companies(tmp_path / "c.yaml", [("personio", "bomb-co", "Bomb")])
    http = host_http({"bomb-co.jobs.personio.de": httpx.Response(200, content=bomb)})
    res = run(store, PersonioConnector(path), http)
    assert res.status == "error"
    assert state(store, "personio", "bomb-co")["consecutive_failures"] == 1


# ---- whole runs -----------------------------------------------------------------------------


def state(store, ats, slug):
    return store.conn.execute(
        "SELECT * FROM ats_companies WHERE ats=? AND slug=?", (ats, slug)
    ).fetchone()


def run(store, connector, http, *, at=NOW, force=False):
    store.sync_sources({connector.name: type(connector)}, at)
    [res] = collect(store, http, [connector], limit=None, force=force, now=at)
    return res


@pytest.mark.parametrize(("cls", "ats", "slug", "ext", "total", "kept"), CASES)
def test_a_run_stores_the_matching_postings_under_the_yaml_name(
    store, tmp_path, cls, ats, slug, ext, total, kept
):
    path = write_companies(tmp_path / "c.yaml", [(ats, slug, "Acme Co")])
    seen: list[str] = []
    http = host_http({HOST_OF[ats]: file_response(ats, slug, ext)}, seen)
    res = run(store, cls(path), http)
    assert res.status == "ok" and res.found == kept and res.new == kept
    assert len(seen) == 1 and seen[0].startswith("GET " + HOST_OF[ats])
    rows = store.conn.execute("SELECT company, source FROM jobs").fetchall()
    assert len(rows) == kept and {r["source"] for r in rows} == {ats}
    s = state(store, ats, slug)
    assert s["last_ok_at"] and s["last_found"] == kept and s["active"] == 1


@pytest.mark.parametrize(("cls", "ats", "slug", "ext", "total", "kept"), CASES)
def test_404_twice_makes_the_company_inactive(store, tmp_path, cls, ats, slug, ext, total, kept):
    path = write_companies(tmp_path / "c.yaml", [(ats, slug, "Gone")])
    http = host_http({})
    run(store, cls(path), http, force=True)
    assert state(store, ats, slug)["gone_strikes"] == 1
    run(store, cls(path), http, at=NOW + timedelta(hours=6, minutes=1), force=True)
    assert state(store, ats, slug)["active"] == 0


def test_a_redirect_to_another_host_is_refused_and_backs_off(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("recruitee", "bunq", "bunq")])
    seen: list[str] = []
    http = host_http(
        {
            "bunq.recruitee.com": httpx.Response(
                302, headers={"Location": "https://careers.bunq.com/api/offers/"}
            ),
        },
        seen,
    )
    res = run(store, RecruiteeConnector(path), http)
    assert res.status == "error"
    assert seen == ["GET bunq.recruitee.com/api/offers/"]  # the other host was never contacted
    assert state(store, "recruitee", "bunq")["consecutive_failures"] == 1


def test_a_redirect_inside_the_tenant_host_is_followed(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("gem", "productboard", "Productboard")])
    calls = [0]

    def answer():
        calls[0] += 1
        if calls[0] == 1:
            return httpx.Response(
                302, headers={"Location": "https://api.gem.com/job_board/v0/productboard/job_posts"}
            )
        return file_response("gem", "productboard", "json")

    res = run(store, GemConnector(path), host_http({"api.gem.com": answer}))
    assert res.status == "ok" and calls[0] == 2


def test_the_user_agent_is_generic(store, tmp_path):
    path = write_companies(tmp_path / "c.yaml", [("workable", "pricelabs", "P")])
    agents: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        agents.append(req.headers["user-agent"])
        return file_response("workable", "pricelabs", "json")

    from cvboost.http import HttpClient
    from tests.conftest import PUBLIC_RESOLVER

    http = HttpClient(httpx.MockTransport(handler), resolver=PUBLIC_RESOLVER, sleep=lambda s: None)
    run(store, WorkableConnector(path), http)
    assert agents == ["cvboost/0.1"]


# ---- the company list, the registry, the schema ---------------------------------------------


def test_the_shipped_list_has_5_to_15_companies_per_new_platform_and_valid_slugs():
    # the public export ships the list as config.example/companies.yaml
    path = (
        "config/companies.yaml"
        if Path("config/companies.yaml").exists()
        else "config.example/companies.yaml"
    )
    companies, rejected = load_companies(path)
    assert rejected == []
    per = {a: sum(1 for c in companies if c.ats == a) for a in ATS_NAMES}
    for ats in ("workable", "recruitee", "teamtailor", "personio", "gem"):
        assert 5 <= per[ats] <= 15, per
    assert len({(c.ats, c.slug) for c in companies}) == len(companies)


def test_every_platform_is_registered_and_gets_the_companies_path(tmp_path):
    got = connectors.get(
        ["workable", "recruitee", "teamtailor", "personio", "gem"],
        companies_path=tmp_path / "x.yaml",
    )
    assert [type(c).__name__ for c in got] == [
        "WorkableConnector", "RecruiteeConnector", "TeamtailorConnector", "PersonioConnector",
        "GemConnector",
    ]  # fmt: skip
    assert all(isinstance(c, ATSConnector) and c.companies_path == tmp_path / "x.yaml" for c in got)
    assert {c.name for c in connectors.REGISTRY.values() if issubclass(c, ATSConnector)} == set(
        ATS_NAMES
    )


def test_migration_0009_accepts_the_new_platforms_and_keeps_the_constraints(store):
    for ats in ATS_NAMES:
        store.conn.execute(
            "INSERT INTO ats_companies (ats, slug, added_at) VALUES (?, 'y', 'now')", (ats,)
        )
    for bad in (("workday", "x"), ("gem", ""), ("gem", "x" * 65)):
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute(
                "INSERT INTO ats_companies (ats, slug, added_at) VALUES (?, ?, 'x')", bad
            )
    idx = {r[1] for r in store.conn.execute("PRAGMA index_list(ats_companies)")}
    assert "idx_ats_companies_due" in idx


def test_migration_0009_copies_the_rows_of_a_db_at_0008(tmp_path, monkeypatch):
    from cvboost.storage import db

    conn = db.connect(tmp_path / "old.sqlite")
    every = db._migration_files()
    monkeypatch.setattr(db, "_migration_files", lambda: [m for m in every if m[0] <= 8])
    assert db.migrate(conn) == 8
    conn.execute(
        "INSERT INTO ats_companies (ats, slug, name, added_at, active, gone_strikes, next_due_at) "
        "VALUES ('lever', 'findem', 'Findem', '2026-11-01', 0, 2, '2026-12-01')"
    )
    monkeypatch.setattr(db, "_migration_files", lambda: every)
    assert db.migrate(conn) >= 9
    row = conn.execute("SELECT * FROM ats_companies").fetchone()
    assert (row["ats"], row["name"], row["active"], row["gone_strikes"], row["next_due_at"]) == (
        "lever", "Findem", 0, 2, "2026-12-01",
    )  # fmt: skip
    assert conn.execute("SELECT COUNT(*) FROM ats_companies").fetchone()[0] == 1


def test_reactivate_choices_cover_every_platform():
    import argparse

    from cvboost import cli

    parser = cli.build_parser()
    actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    companies = actions[0].choices["companies"]
    reactivate = next(
        a for a in companies._actions if isinstance(a, argparse._SubParsersAction)
    ).choices["reactivate"]
    ats_arg = next(a for a in reactivate._actions if a.dest == "ats")
    assert set(ats_arg.choices) == set(ATS_NAMES)


# ---- fair polling across all the ATS connectors --------------------------------------------


class _Slow(ATSConnector):
    """An ATS whose run spends 20 s of fake time and finds nothing."""

    kind = "ats"
    base_url = "https://example.com/x"
    allowed_hosts = frozenset({"example.com"})
    tos_note = ""
    attribution = ""

    def __init__(self, name, clock, ran):
        super().__init__()
        self.name = name  # type: ignore[misc]
        self.clock, self.ran = clock, ran

    def company_url(self, slug):
        return ""

    def postings(self, data):
        return []

    def posting_id(self, item):
        return None

    def posting_title(self, item):
        return None

    def normalise(self, raw: RawPosting, fetched_at):
        raise NotImplementedError

    def before_run(self, store, now):
        self._batch = ["x"]

    def after_run(self, store, now):
        pass

    def fetch_partial(self, http, limit=None):
        from cvboost.connectors.base import FetchOutcome

        self.ran.append(self.name)
        self.clock.advance(seconds=20)
        return FetchOutcome([])


def test_eight_ats_connectors_take_turns_when_the_60s_budget_serves_only_three(tmp_path):
    from cvboost.storage.db import Store, connect, migrate
    from tests.conftest import NOW as T0

    class Clock:
        def __init__(self):
            self.t = T0

        def __call__(self):
            return self.t

        def advance(self, seconds):
            self.t += timedelta(seconds=seconds)

    store = Store(connect(tmp_path / "f.sqlite"))
    migrate(store.conn)
    clock, ran = Clock(), []
    conns = [_Slow(f"ats_{n}", clock, ran) for n in "abcdefgh"]
    store.sync_sources({c.name: type(c) for c in conns}, T0)
    served: list[list[str]] = []
    for _ in range(4):
        ran.clear()
        collect(store, None, conns, limit=None, force=True, clock=clock,
                deadline=clock() + timedelta(seconds=540))  # fmt: skip
        served.append(list(ran))
        clock.advance(seconds=600)
    assert all(len(r) == 3 for r in served)  # 60 s / 20 s
    # nobody starves: every connector ran within the first three cycles, and the ones deferred
    # in a cycle run first in the next one
    assert {n for r in served[:3] for n in r} == {c.name for c in conns}
    for prev, nxt in zip(served, served[1:], strict=False):
        skipped = [c.name for c in conns if c.name not in prev]
        assert set(nxt[:3]) <= set(skipped) or len(skipped) < 3
