from __future__ import annotations

import sqlite3

import pytest

from cvboost.storage.db import _migration_files, connect, migrate

FILES = dict(_migration_files())


def v17_db(tmp_path):
    """A database as it is before 0018, with a contact and a finished draft in it."""
    conn = connect(tmp_path / "db.sqlite")
    versions = [v for v in FILES if v <= 17]
    conn.executescript(
        "BEGIN;\n" + "".join(FILES[v] for v in versions) + "\nCREATE TABLE schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);\n"
        + "INSERT INTO schema_migrations VALUES "
        + ",".join(f"({v},'x')" for v in versions)
        + ";\nCOMMIT;"
    )
    conn.execute(
        "INSERT INTO crm_contacts (name, name_key, note, created_at, updated_at) "
        "VALUES ('Ann','ann','keep me','x','x')"
    )
    conn.execute(
        "INSERT INTO crm_reply_requests (contact_id, status, requested_at, draft) "
        "VALUES (1,'done','x','old draft')"
    )
    return conn


def test_existing_rows_untouched_new_columns_null(tmp_path):
    conn = v17_db(tmp_path)
    migrate(conn)
    assert tuple(conn.execute("SELECT note, owner_context FROM crm_contacts").fetchone()) == (
        "keep me",
        None,
    )
    row = conn.execute(
        "SELECT draft, email_to, email_subject, email_body FROM crm_reply_requests"
    ).fetchone()
    assert tuple(row) == ("old draft", None, None, None)
    assert 18 in {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_length_checks_and_idempotent(tmp_path):
    conn = v17_db(tmp_path)
    migrate(conn)
    conn.execute("UPDATE crm_contacts SET owner_context = ?", ("x" * 4000,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE crm_contacts SET owner_context = ?", ("x" * 4001,))
    before = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == before
