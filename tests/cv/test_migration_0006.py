from __future__ import annotations

import sqlite3

import pytest

from cvboost.storage import db as D
from cvboost.storage.db import Store, _migration_files, connect, migrate
from tests.conftest import NOW

FILES = dict(_migration_files())
LATEST = max(FILES)  # grows with every migration


def v5_db(tmp_path):
    """A database as it is before 0006: migrations 1-5 applied."""
    conn = connect(tmp_path / "db.sqlite")
    conn.executescript(
        "BEGIN;\n"
        + "".join(FILES[v] for v in (1, 2, 3, 4, 5))
        + "\nCREATE TABLE schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);\n"
        "INSERT INTO schema_migrations VALUES (1,'x'),(2,'x'),(3,'x'),(4,'x'),(5,'x');\nCOMMIT;"
    )
    return conn


def seed_old(conn):
    conn.execute("INSERT INTO sources (name, created_at) VALUES ('s', 'x')")
    conn.execute(
        "INSERT INTO jobs (id, canonical_id, source, source_id, url, company, title, "
        "description_html, description_text, tags_json, fetched_at, raw_json, content_hash, "
        "first_seen_at, last_seen_at) VALUES (1,1,'s','1','https://e.com','C','T','','','[]',"
        "'x','{}','h','x','x')"
    )
    conn.execute(
        "INSERT INTO llm_calls (id, kind, model_alias, n_items, started_at, local_day, status, "
        "total_cost_usd, pause_until) VALUES (7,'score','haiku',3,'x','2026-11-06','usage_limit', "
        "0.5, '2026-11-06T13:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO llm_cache (item_hash, model_alias, prompt_version, output_json, call_id, "
        "created_at) VALUES ('h','haiku','p','{}',7,'x')"
    )
    conn.execute(
        "INSERT INTO llm_item_errors (job_id, item_hash, model_alias, prompt_version, call_id, "
        "error, at) VALUES (1,'h','haiku','p',7,'error','x')"
    )
    conn.execute(
        "INSERT INTO decisions (id, canonical_id, job_id, action, channel, at) "
        "VALUES (1,1,1,'cv_requested','telegram','x')"
    )
    conn.execute(
        "INSERT INTO cv_requests (id, canonical_id, job_id, decision_id, status, kit_dir, "
        "requested_at, error) VALUES (1,1,1,1,'done','1','x',NULL)"
    )


def test_upgrade_keeps_data_and_foreign_keys(tmp_path):
    conn = v5_db(tmp_path)
    seed_old(conn)
    assert migrate(conn) == LATEST
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not conn.in_transaction
    call = conn.execute("SELECT * FROM llm_calls WHERE id=7").fetchone()
    assert (call["kind"], call["total_cost_usd"], call["status"]) == ("score", 0.5, "usage_limit")
    assert conn.execute("SELECT call_id FROM llm_cache").fetchone()[0] == 7
    assert conn.execute("SELECT call_id FROM llm_item_errors").fetchone()[0] == 7
    req = conn.execute("SELECT * FROM cv_requests").fetchone()
    assert (req["status"], req["kit_dir"], req["attempts"]) == ("done", "1", 0)
    idx = {r[1] for r in conn.execute("PRAGMA index_list(llm_calls)")}
    assert {"idx_llm_calls_day", "idx_llm_calls_pause"} <= idx
    assert "uq_cv_requests_active" in {r[1] for r in conn.execute("PRAGMA index_list(cv_requests)")}


def test_new_values_are_accepted_and_old_ones_still_rejected(tmp_path):
    conn = v5_db(tmp_path)
    seed_old(conn)
    migrate(conn)
    ins = (
        "INSERT INTO llm_calls (kind, model_alias, n_items, started_at, local_day, status) "
        "VALUES (?, 'opus', 1, 'x', 'd', 'started')"
    )
    conn.execute(ins, ("cv",))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("Other Kind",))  # since 0028 only the format is checked
    conn.execute("UPDATE cv_requests SET status='needs_review', pdf_status='ok'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE cv_requests SET status='weird'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE cv_requests SET pdf_status='maybe'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE cv_requests SET feedback=?", ("x" * 2001,))
    # still one active request per job
    q = (
        "INSERT INTO cv_requests (canonical_id, job_id, decision_id, status, requested_at) "
        "VALUES (1,1,1,'queued','x')"
    )
    conn.execute(q)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(q)


def test_counting_rules_include_cv_calls(tmp_path):
    conn = v5_db(tmp_path)
    migrate(conn)
    store = Store(conn)
    for kind, status in (("score", "ok"), ("cv", "ok"), ("preflight", "ok"), ("cv", "usage_limit")):
        store.insert_llm_call(
            kind=kind, model_alias="opus", prompt_version=None, n_items=1, attempt=1, now=NOW,
            local_day="2026-11-06", status=status,
        )  # fmt: skip
    assert store.calls_today("opus", "2026-11-06") == 3  # preflight excluded
    assert store.consecutive_limit_hits() == 1


def test_old_code_paths_work_on_the_new_schema(tmp_path):
    conn = v5_db(tmp_path)
    seed_old(conn)
    migrate(conn)
    conn.execute("UPDATE cv_requests SET status='cancelled'")
    did, rid, created = Store(conn).request_cv(1, channel="panel", now=NOW)  # the #5 method
    assert created and rid > 1


def test_broken_script_rolls_back_and_restores_foreign_keys(tmp_path, monkeypatch):
    conn = v5_db(tmp_path)
    seed_old(conn)
    migrate(conn)
    files = _migration_files() + [
        (
            LATEST + 1,
            "-- cvboost: foreign_keys=off\nCREATE TABLE t7 (a);\nINSERT INTO nope VALUES (1);\n",
        )
    ]
    monkeypatch.setattr(D, "_migration_files", lambda: files)
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn)
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == LATEST
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='t7'").fetchone() is None
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1 and not conn.in_transaction


def test_foreign_key_violation_rolls_the_migration_back(tmp_path, monkeypatch):
    conn = v5_db(tmp_path)
    migrate(conn)
    bad = (
        "-- cvboost: foreign_keys=off\n"
        "INSERT INTO decisions (canonical_id, job_id, action, channel, at) "
        "VALUES (99,99,'interested','panel','x');\n"
    )
    monkeypatch.setattr(D, "_migration_files", lambda: [*_migration_files(), (LATEST + 1, bad)])
    with pytest.raises(sqlite3.IntegrityError, match="foreign_key_check"):
        migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == LATEST
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_a_reserved_earlier_number_is_still_applied_later(tmp_path, monkeypatch):
    """A number that was reserved and merged later (here 9 after 10) must not be skipped.
    (5 can no longer play it: 0011 rebuilds `applications`.)"""
    real = _migration_files()
    without_5 = [f for f in real if f[0] != 9]
    ddl = "SELECT sql FROM sqlite_master WHERE name='ats_companies'"
    conn = connect(tmp_path / "db.sqlite")
    monkeypatch.setattr(D, "_migration_files", lambda: without_5)
    assert migrate(conn) == LATEST
    assert "workable" not in conn.execute(ddl).fetchone()[0]
    monkeypatch.setattr(D, "_migration_files", lambda: real)
    assert migrate(conn) == LATEST
    assert "workable" in conn.execute(ddl).fetchone()[0]
    assert [
        r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")
    ] == sorted(FILES)
