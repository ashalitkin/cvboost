"""companies.yaml, slug validation, discovery (DB + remoteintech archive), the CLI command."""

from __future__ import annotations

import io
import sqlite3
import tarfile
import time

import httpx
import pytest
import yaml

from cvboost import cli
from cvboost import companies as co
from cvboost.companies import Company
from cvboost.http import HttpClient
from tests.ats.helpers import write_companies
from tests.conftest import NOW, PUBLIC_RESOLVER, make_job


@pytest.mark.parametrize("slug", ["acme", "a-b-1", "0", "x" * 64])
def test_valid_slugs(slug):
    assert Company(ats="lever", slug=slug).slug == slug


@pytest.mark.parametrize(
    "slug", ["../x", "a/b", "A", "acme\n", "a b", "", "x" * 65, "a.b", "a_b", "%2e%2e", "é"]
)
def test_invalid_slugs_are_rejected(slug):
    with pytest.raises(ValueError):
        Company(ats="lever", slug=slug)


def test_unknown_ats_is_rejected():
    with pytest.raises(ValueError):
        Company(ats="workday", slug="acme")


def test_load_skips_and_reports_bad_entries(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "companies:\n"
        "  - {ats: lever, slug: '../etc/passwd', name: Evil, source: t}\n"
        "  - {ats: lever, slug: ok-co, name: Ok, source: t}\n"
        "  - {ats: lever, slug: ok-co, name: Dup, source: t}\n"
        "  - {ats: nope, slug: x}\n"
        "  - not-a-mapping\n"
    )
    good, rejected = co.load_companies(p)
    assert [(c.ats, c.slug, c.name) for c in good] == [("lever", "ok-co", "Ok")]
    assert len(rejected) == 3


def test_load_missing_file_is_empty_and_garbage_is_an_error(tmp_path):
    assert co.load_companies(tmp_path / "none.yaml") == ([], [])
    p = tmp_path / "bad.yaml"
    p.write_text("companies: 5\n")
    with pytest.raises(ValueError):
        co.load_companies(p)
    p.write_text(": : :\n\t-")
    with pytest.raises(ValueError):
        co.load_companies(p)


def test_write_load_roundtrip_sorted(tmp_path):
    p = tmp_path / "c.yaml"
    rows = [
        Company(ats="lever", slug="b", name="B"),
        Company(ats="ashby", slug="a", name="Ä, x: y"),
    ]
    co.write_companies(p, rows)
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# ATS companies")
    back, rejected = co.load_companies(p)
    assert rejected == [] and [(c.ats, c.slug) for c in back] == [("ashby", "a"), ("lever", "b")]
    assert back[0].name == "Ä, x: y"
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/airbyte", ("greenhouse", "airbyte")),
        ("https://boards.greenhouse.io/consensys/jobs/1990589", ("greenhouse", "consensys")),
        ("https://job-boards.greenhouse.io/Acme-Co/jobs/1", ("greenhouse", "acme-co")),
        ("https://boards.greenhouse.io/embed/job_app?for=Acme&token=1", ("greenhouse", "acme")),
        ("https://boards.greenhouse.io/embed/job_board?x=1", None),
        ("https://jobs.lever.co/Mycelium/", ("lever", "mycelium")),
        ("https://jobs.lever.co/iterative?lever-origin=applied", ("lever", "iterative")),
        ("https://jobs.ashbyhq.com/Deel", ("ashby", "deel")),
        ("https://jobs.ashbyhq.com/sketch/3cd3f073-2fae", ("ashby", "sketch")),
        ("http://jobs.lever.co/acme", ("lever", "acme")),
        ("https://www.ashbyhq.com/team", None),
        ("https://app.ashbyhq.com/api/images/x", None),
        ("https://evil.example.com/jobs.lever.co/acme", None),
        ("https://jobs.lever.co.evil.com/acme", None),
        ("https://jobs.lever.co/../etc", None),
        ("https://jobs.lever.co/a_b", None),
        ("https://jobs.lever.co/", None),
        ("", None),
        (None, None),
        ("not a url", None),
    ],
)
def test_ats_slug_from_url(url, expected):
    assert co.ats_slug_from_url(url) == expected


def test_find_ats_urls_in_text():
    text = (
        'Apply <a href="https://jobs.ashbyhq.com/1password/automation-notice">here</a> or '
        "https://jobs.lever.co/acme/123, again https://jobs.lever.co/acme and "
        "https://www.ashbyhq.com/team and https://boards.greenhouse.io/embed/x"
    )
    assert co.find_ats_urls(text) == [("ashby", "1password"), ("lever", "acme")]
    assert co.find_ats_urls(None) == []


def make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    from cvboost.storage.db import Store, migrate

    migrate(conn)
    store = Store(conn)
    store.sync_sources({}, NOW)
    conn.execute("INSERT INTO sources (name, created_at) VALUES ('fake', 'x')")
    for i, kw in enumerate(rows):
        store.upsert_job(make_job(str(i), **kw), store.start_run("fake", NOW), NOW)
    conn.commit()
    conn.close()


def test_discover_from_db_uses_url_apply_url_and_description(tmp_path):
    db = tmp_path / "jobs.sqlite"
    make_db(
        db,
        [
            {"url": "https://boards.greenhouse.io/acme/jobs/1", "company": "Acme Inc"},
            {
                "url": "https://example.com/j/2",
                "apply_url": "https://jobs.lever.co/beta?x=1",
                "company": "Beta",
            },
            {
                "url": "https://example.com/j/3",
                "description_html": "<a href='https://jobs.ashbyhq.com/Gamma/xyz'>apply</a>",
                "company": "Gamma Ltd",
            },
            {"url": "https://example.com/j/4", "company": "Nobody"},
            {"url": "https://boards.greenhouse.io/acme/jobs/9", "company": "Acme again"},
        ],
    )
    before = db.read_bytes()
    found = co.discover_from_db(db)
    assert {(c.ats, c.slug, c.name, c.source) for c in found} == {
        ("greenhouse", "acme", "Acme Inc", "db:url"),
        ("lever", "beta", "Beta", "db:apply_url"),
        ("ashby", "gamma", "Gamma Ltd", "db:description"),
    }
    assert db.read_bytes() == before  # opened read-only: not a byte written


def test_discover_from_db_cannot_write(tmp_path):
    db = tmp_path / "jobs.sqlite"
    make_db(db, [])
    uri = f"file:{db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE t (x)")
    conn.close()


def archive(profiles: dict[str, str], extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        files = {f"remote-jobs-main/src/companies/{k}.md": v.encode() for k, v in profiles.items()}
        files.update(extra or {})
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


PROFILE = (
    '---\ntitle: "{t}"\nslug: x\nwebsite: https://x.com\ncareers_url: {u}\n---\n\n## How to apply\n'
)


def test_parse_remote_jobs_archive():
    data = archive(
        {
            "airbyte": PROFILE.format(t="Airbyte", u="https://boards.greenhouse.io/airbyte"),
            "alan": PROFILE.format(t="Alan", u="https://jobs.lever.co/alan?team=x"),
            "plain": PROFILE.format(t="Plain Co", u="https://plain.example.com/careers"),
            "body": "---\ntitle: Body Co\n---\nApply: https://jobs.ashbyhq.com/Body-Co/abc\n",
            "bad": "---\n: : [\n---\nhttps://jobs.lever.co/bad-yaml\n",
        },
        extra={
            "remote-jobs-main/README.md": b"https://jobs.lever.co/readme-only",
            "remote-jobs-main/src/blog/x.md": b"https://jobs.lever.co/blog-only",
            "../evil/src/companies/zz.md": b"https://jobs.lever.co/evil-path",
        },
    )
    got = {(c.ats, c.slug, c.name, c.source) for c in co.parse_remote_jobs_archive(data)}
    assert got == {
        ("greenhouse", "airbyte", "Airbyte", "remoteintech"),
        ("lever", "alan", "Alan", "remoteintech"),
        ("ashby", "body-co", "Body Co", "remoteintech"),
        ("lever", "bad-yaml", "bad", "remoteintech"),
    }


def test_archive_size_caps():
    big = "https://jobs.lever.co/huge " + "x" * (co.MAX_PROFILE_BYTES + 10)
    data = archive({"huge": big, "ok": "https://jobs.lever.co/ok-co"})
    assert [c.slug for c in co.parse_remote_jobs_archive(data)] == ["ok-co"]


def test_fetch_is_one_request_to_the_github_download_host():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=b"tar")

    http = HttpClient(httpx.MockTransport(handler), resolver=PUBLIC_RESOLVER, sleep=lambda s: None)
    assert co.fetch_remote_jobs_archive(http) == b"tar"
    assert [str(r.url) for r in seen] == [co.REMOTE_JOBS_ARCHIVE]
    assert seen[0].url.host == "codeload.github.com"


def test_merge_keeps_existing_and_dedupes():
    old = [Company(ats="lever", slug="a", name="Mine", source="manual")]
    new = [
        Company(ats="lever", slug="a", name="Theirs", source="db:url"),
        Company(ats="lever", slug="b", name="B", source="db:url"),
        Company(ats="ashby", slug="a", name="A", source="db:url"),
    ]
    out = {(c.ats, c.slug): c for c in co.merge(old, new)}
    assert len(out) == 3 and out[("lever", "a")].name == "Mine"
    shown = {a: n for a, n in co.per_ats(list(out.values())).items() if n or a in co.ATS_NAMES[:3]}
    assert shown == {"greenhouse": 0, "lever": 2, "ashby": 1}


# ---- CLI ---------------------------------------------------------------------------------------


def cli_env(tmp_path, monkeypatch, db):
    monkeypatch.setenv("CVBOOST_DB", str(db))
    monkeypatch.setenv("CVBOOST_COMPANIES", str(tmp_path / "companies.yaml"))
    return tmp_path / "companies.yaml"


def test_cli_discover_offline_writes_yaml_and_is_idempotent(tmp_path, monkeypatch, capsys):
    db = tmp_path / "jobs.sqlite"
    make_db(db, [{"url": "https://boards.greenhouse.io/acme/jobs/1", "company": "Acme"}])
    out = cli_env(tmp_path, monkeypatch, db)
    assert cli.main(["companies", "discover", "--no-fetch"]) == 0
    assert "+1 new, 1 total" in capsys.readouterr().out
    assert cli.main(["companies", "discover", "--no-fetch"]) == 0
    assert "+0 new, 1 total" in capsys.readouterr().out
    rows = yaml.safe_load(out.read_text())["companies"]
    assert rows == [{"ats": "greenhouse", "slug": "acme", "name": "Acme", "source": "db:url"}]


def test_cli_discover_with_the_github_list(tmp_path, monkeypatch, capsys):
    db = tmp_path / "jobs.sqlite"
    make_db(db, [{"url": "https://jobs.lever.co/beta", "company": "Beta"}])
    out = cli_env(tmp_path, monkeypatch, db)
    data = archive({"a": PROFILE.format(t="Alan", u="https://jobs.lever.co/alan")})
    monkeypatch.setattr("cvboost.companies.fetch_remote_jobs_archive", lambda http: data)
    assert cli.main(["companies", "discover"]) == 0
    text = capsys.readouterr().out
    assert "remoteintech/remote-jobs: 1 companies" in text and "lever: 2" in text
    assert {c["slug"] for c in yaml.safe_load(out.read_text())["companies"]} == {"alan", "beta"}


def test_cli_discover_dry_run_and_missing_db(tmp_path, monkeypatch, capsys):
    db = tmp_path / "jobs.sqlite"
    make_db(db, [{"url": "https://jobs.lever.co/beta", "company": "Beta"}])
    out = cli_env(tmp_path, monkeypatch, db)
    assert cli.main(["companies", "discover", "--no-fetch", "--dry-run"]) == 0
    assert not out.exists() and "nothing written" in capsys.readouterr().out
    assert (
        cli.main(["companies", "discover", "--no-fetch", "--db", str(tmp_path / "no.sqlite")]) == 2
    )


def test_cli_discover_keeps_manual_entries_and_reports_a_failed_download(
    tmp_path, monkeypatch, capsys
):
    db = tmp_path / "jobs.sqlite"
    make_db(db, [])
    out = cli_env(tmp_path, monkeypatch, db)
    write_companies(out, [("ashby", "mine", "Mine")])
    from cvboost.http import FetchError

    def boom(http):
        raise FetchError("HTTP 503", url="x", status=503, attempts=3)

    monkeypatch.setattr("cvboost.companies.fetch_remote_jobs_archive", boom)
    assert cli.main(["companies", "discover"]) == 1
    assert [c["slug"] for c in yaml.safe_load(out.read_text())["companies"]] == ["mine"]


# ---- ReDoS ---------------------------------------------------------------------------------------

N = 30000
HOSTILE = {
    "dashes": "https://jobs.lever.co/" + "-" * N,
    "a": "https://jobs.lever.co/" + "a" * N,
    "pct": "https://jobs.lever.co/" + "%" * N,
    "urls": "https://jobs.lever.co/a " * 5000,
    "spaces": "https://jobs.lever.co/" + " " * N + "x",
    "dot": "https://boards.greenhouse.io/" + "a." * 15000,
    "scheme": "http" * 8000,
}


@pytest.mark.parametrize("name", HOSTILE)
def test_page_url_regex_is_fast(name):
    t = time.perf_counter()
    co.find_ats_urls(HOSTILE[name])
    assert time.perf_counter() - t < 0.5


# ---- round 2: archive limits, description cap, CLI errors, reactivate, sources -----------------


def test_archive_counts_all_members_not_only_profiles(monkeypatch):
    monkeypatch.setattr(co, "MAX_ARCHIVE_MEMBERS", 5)
    extra = {f"remote-jobs-main/assets/{i}.bin": b"x" for i in range(10)}
    data = archive({"a": "https://jobs.lever.co/seen-first"}, extra=extra)
    # profiles come first in the tar, then 10 other members: reading stops, no crash
    assert [c.slug for c in co.parse_remote_jobs_archive(data)] == ["seen-first"]
    only_junk = archive({}, extra={f"remote-jobs-main/a/{i}": b"" for i in range(10)})
    only_junk2 = archive({"late": "https://jobs.lever.co/late"}, extra={})
    assert co.parse_remote_jobs_archive(only_junk) == []
    assert [c.slug for c in co.parse_remote_jobs_archive(only_junk2)] == ["late"]


def test_archive_sums_the_sizes_of_all_members(monkeypatch):
    monkeypatch.setattr(co, "MAX_ARCHIVE_UNPACKED", 1000)
    data = archive(
        {},
        extra={
            "remote-jobs-main/aaa-big.bin": b"x" * 2000,  # not a profile, still counted
            "remote-jobs-main/src/companies/zzz.md": b"https://jobs.lever.co/after-the-blob",
        },
    )
    assert co.parse_remote_jobs_archive(data) == []


def test_download_has_a_5mb_compressed_limit():
    assert co.MAX_ARCHIVE_COMPRESSED == 5 * 1024 * 1024

    def handler(req):
        return httpx.Response(200, content=b"x" * (co.MAX_ARCHIVE_COMPRESSED + 1))

    http = HttpClient(httpx.MockTransport(handler), resolver=PUBLIC_RESOLVER, sleep=lambda s: None)
    from cvboost.http import FetchError

    with pytest.raises(FetchError, match="larger than"):
        co.fetch_remote_jobs_archive(http)


def test_cap_description_finds():
    existing = [Company(ats="lever", slug="known", source="db:description")]
    found = [Company(ats="lever", slug="known", source="db:description")]
    found += [Company(ats="ashby", slug=f"n-{i}", source="db:description") for i in range(5)]
    found += [Company(ats="ashby", slug="from-url", source="db:url")]
    kept, new, dropped = co.cap_description_finds(found, existing, cap=3)
    assert (new, dropped) == (5, 2)
    assert {c.slug for c in kept} == {"known", "n-0", "n-1", "n-2", "from-url"}


def test_cli_caps_description_finds_and_prints_the_count(tmp_path, monkeypatch, capsys):
    db = tmp_path / "jobs.sqlite"
    rows = [
        {"url": f"https://example.com/{i}", "description_html": f"https://jobs.lever.co/co-{i}"}
        for i in range(30)
    ]
    make_db(db, rows)
    out = cli_env(tmp_path, monkeypatch, db)
    assert cli.main(["companies", "discover", "--no-fetch"]) == 0
    text = capsys.readouterr().out
    assert "30 new from job descriptions" in text and "kept 20, cap 20" in text
    assert len(yaml.safe_load(out.read_text())["companies"]) == 20


@pytest.mark.parametrize("data", [b"not gzip at all", b"\x1f\x8b\x08\x00garbage", b""])
def test_cli_survives_a_corrupt_archive(tmp_path, monkeypatch, capsys, data):
    db = tmp_path / "jobs.sqlite"
    make_db(db, [])
    cli_env(tmp_path, monkeypatch, db)
    monkeypatch.setattr("cvboost.companies.fetch_remote_jobs_archive", lambda http: data)
    assert cli.main(["companies", "discover"]) == 1
    assert "error: remoteintech/remote-jobs" in capsys.readouterr().err


def test_cli_reactivate_and_sources_counts(tmp_path, monkeypatch, capsys):
    db = tmp_path / "db.sqlite"
    monkeypatch.setenv("CVBOOST_DB", str(db))
    from cvboost import ats_state
    from cvboost.storage.db import Store, connect, migrate

    conn = connect(db)
    migrate(conn)
    store = Store(conn)
    ats_state.sync_companies(
        store, [Company(ats="lever", slug="dead"), Company(ats="lever", slug="ok")], NOW
    )
    conn.execute("UPDATE ats_companies SET active = 0 WHERE slug = 'dead'")
    conn.close()
    assert cli.main(["sources"]) == 0
    assert "lever companies: 1 active, 1 inactive" in capsys.readouterr().out
    assert cli.main(["companies", "reactivate", "lever", "dead"]) == 0
    assert cli.main(["sources"]) == 0
    assert "lever companies: 2 active, 0 inactive" in capsys.readouterr().out
    assert cli.main(["companies", "reactivate", "lever", "nobody"]) == 1
    assert cli.main(["companies", "reactivate", "lever", "../x"]) == 2
