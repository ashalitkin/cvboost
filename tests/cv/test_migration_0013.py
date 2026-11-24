from __future__ import annotations

import sqlite3

import pytest

from cvboost.storage.db import _migration_files, connect, migrate
from tests.conftest import NOW

FILES = dict(_migration_files())


def v12_db(tmp_path):
    """A database as it is before 0013: migrations 1-12 applied, with CV requests in it."""
    conn = connect(tmp_path / "db.sqlite")
    versions = range(1, 13)
    conn.executescript(
        "BEGIN;\n" + "".join(FILES[v] for v in versions) + "\nCREATE TABLE schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);\n"
        + "INSERT INTO schema_migrations VALUES "
        + ",".join(f"({v},'x')" for v in versions)
        + ";\nCOMMIT;"
    )
    conn.execute("INSERT INTO sources (name, created_at) VALUES ('s', 'x')")
    for i in (1, 2):
        conn.execute(
            "INSERT INTO jobs (id, canonical_id, source, source_id, url, company, title, "
            "description_html, description_text, tags_json, fetched_at, raw_json, content_hash, "
            f"first_seen_at, last_seen_at) VALUES ({i},{i},'s','{i}','https://e.com/{i}','C','T',"
            "'','','[]','x','{}','h" + str(i) + "','x','x')"
        )
        conn.execute(
            "INSERT INTO decisions (id, canonical_id, job_id, action, channel, at) "
            f"VALUES ({i},{i},{i},'cv_requested','telegram','x')"
        )
    conn.execute(
        "INSERT INTO cv_requests (id, canonical_id, job_id, decision_id, status, kit_dir, "
        "requested_at) VALUES (1,1,1,1,'done','1','x')"
    )
    conn.execute(
        "INSERT INTO cv_requests (id, canonical_id, job_id, decision_id, status, requested_at) "
        "VALUES (2,2,2,2,'queued','x')"
    )
    return conn


def test_existing_requests_become_kind_cv_and_data_is_kept(tmp_path):
    conn = v12_db(tmp_path)
    migrate(conn)
    rows = conn.execute("SELECT id, kind, status, kit_dir FROM cv_requests ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(1, "cv", "done", "1"), (2, "cv", "queued", None)]
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(cv_requests)")}
    assert cols["kind"][4] == "'cv'" and cols["kind"][3] == 1  # default, NOT NULL
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_kind_check_and_one_active_request_per_job_and_kind(tmp_path):
    conn = v12_db(tmp_path)
    migrate(conn)
    ins = (
        "INSERT INTO cv_requests (canonical_id, job_id, decision_id, status, requested_at, kind) "
        "VALUES (1,1,1,?, 'x', ?)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("done", "x"))
    conn.execute(ins, ("queued", "cover"))  # a letter next to a finished kit
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("queued", "cover"))  # a second active letter
    conn.execute(ins, ("queued", "cv"))  # but "Prepare CV" is a different kind
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("queued", "cv"))


def test_runs_twice_safely(tmp_path):
    conn = v12_db(tmp_path)
    first = migrate(conn)
    assert migrate(conn) == first
    assert conn.execute("SELECT COUNT(*) FROM cv_requests WHERE kind='cv'").fetchone()[0] == 2
    assert (
        conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=13").fetchone()[0] == 1
    )


def test_request_cv_is_not_absorbed_by_a_queued_letter(tmp_path):
    from cvboost.storage.db import Store
    from tests.llm.helpers import seed

    conn = connect(tmp_path / "x.sqlite")
    migrate(conn)
    store = Store(conn)
    (jid,) = seed(store, 1)
    _, rid, _ = store.request_cv(jid, channel="panel", now=NOW)
    conn.execute("UPDATE cv_requests SET status='done', kit_dir=canonical_id WHERE id=?", (rid,))
    cid = conn.execute("SELECT canonical_id FROM jobs WHERE id=?", (jid,)).fetchone()[0]
    cover = store.request_cover(cid, now=NOW)
    assert cover is not None
    _, again, created = store.request_cv(jid, channel="panel", now=NOW)
    assert created and again not in (rid, cover)
    row = store.cv_request(again)
    assert row["kind"] == "cv" and row["status"] == "queued"
