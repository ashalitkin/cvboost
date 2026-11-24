from __future__ import annotations

import sqlite3

import pytest

from cvboost.storage.db import _migration_files, connect, migrate

REPLY_INS = (
    "INSERT INTO crm_reply_requests (contact_id, status, requested_at) VALUES (1,'queued','x')"
)
CONTACT_INS = (
    "INSERT INTO crm_contacts (name, name_key, created_at, updated_at) VALUES ('a','a','x','x')"
)
FILES = dict(_migration_files())


def v13_db(tmp_path):
    """A database as it is before 0016 (0014/0015 belong to #17 and may be absent)."""
    conn = connect(tmp_path / "db.sqlite")
    versions = range(1, 14)
    conn.executescript(
        "BEGIN;\n" + "".join(FILES[v] for v in versions) + "\nCREATE TABLE schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);\n"
        + "INSERT INTO schema_migrations VALUES "
        + ",".join(f"({v},'x')" for v in versions)
        + ";\nCOMMIT;"
    )
    conn.execute("INSERT INTO sources (name, created_at) VALUES ('s', 'x')")
    conn.execute(
        "INSERT INTO jobs (id, canonical_id, source, source_id, url, company, title, "
        "description_html, description_text, tags_json, fetched_at, raw_json, content_hash, "
        "first_seen_at, last_seen_at) VALUES (1,1,'s','1','https://e.com/1','C','T',"
        "'','','[]','x','{}','h1','x','x')"
    )
    conn.execute(
        "INSERT INTO decisions (id, canonical_id, job_id, action, channel, at) "
        "VALUES (1,1,1,'cv_requested','telegram','x')"
    )
    conn.execute(
        "INSERT INTO cv_requests (id, canonical_id, job_id, decision_id, status, requested_at) "
        "VALUES (1,1,1,1,'queued','x')"
    )
    return conn


def test_existing_data_untouched_and_crm_tables_empty(tmp_path):
    conn = v13_db(tmp_path)
    migrate(conn)
    assert conn.execute("SELECT status, kind FROM cv_requests").fetchall()[0][:] == ("queued", "cv")
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    for t in ("crm_contacts", "crm_events", "crm_reply_requests"):
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert 16 in {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}


def test_checks_and_unique_indexes(tmp_path):
    conn = v13_db(tmp_path)
    migrate(conn)
    ins = (
        "INSERT INTO crm_contacts (name, name_key, created_at, updated_at, {c}) "
        "VALUES ('a', ?, 'x', 'x', ?)"
    )
    conn.execute(CONTACT_INS)
    conn.execute(CONTACT_INS)  # the same name twice is fine (two people); the slug is unique
    slug = "UPDATE crm_contacts SET profile_slug = 'x' WHERE id = ?"
    conn.execute(slug, (1,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(slug, (2,))
    for col, bad in [("col", "x"), ("kind", "boss"), ("due_at", "12.11.2026"),
                     ("profile_url", "http://www.linkedin.com/in/x"),
                     ("profile_url", "https://evil.example.com/in/x")]:  # fmt: skip
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(ins.format(c=col), (f"k{col}{bad}", bad))
    conn.execute(
        "INSERT INTO crm_events (contact_id, kind, received_at, dedupe_key, created_at) "
        "VALUES (1,'message','x','d','x')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO crm_events (contact_id, kind, received_at, dedupe_key, created_at) "
            "VALUES (1,'message','x','d','x')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO crm_events (contact_id, kind, received_at, view_url, dedupe_key, "
            "created_at) "
            "VALUES (1,'message','x','https://evil.example.com/x','d2','x')"
        )
    conn.execute(REPLY_INS)
    with pytest.raises(sqlite3.IntegrityError):  # one active request per contact
        conn.execute(REPLY_INS)
    conn.execute("UPDATE crm_reply_requests SET status='done'")
    conn.execute(REPLY_INS)


def test_works_without_14_and_15_and_is_idempotent(tmp_path):
    conn = v13_db(tmp_path)
    migrate(conn)
    before = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == before
