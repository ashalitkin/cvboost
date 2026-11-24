from __future__ import annotations

import sqlite3

import pytest

from cvboost.storage.db import _migration_files, connect, migrate

FILES = dict(_migration_files())


def v14_db(tmp_path):
    """A database as before 0015: migrations 1-14, with llm_calls, cache, errors and cv_requests."""
    conn = connect(tmp_path / "db.sqlite")
    versions = range(1, 15)
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
    for cid, kind, status in ((11, "score", "ok"), (12, "cv", "ok"), (13, "preflight", "ok")):
        conn.execute(
            "INSERT INTO llm_calls (id, kind, model_alias, n_items, started_at, local_day, status,"
            " total_cost_usd) VALUES (?, ?, 'haiku', 2, 't', 'd', ?, 0.01)",
            (cid, kind, status),
        )
    conn.execute(
        "INSERT INTO llm_cache (item_hash, model_alias, prompt_version, output_json, call_id, "
        "created_at) VALUES ('h', 'haiku', 'p', '{}', 11, 't')"
    )
    conn.execute(
        "INSERT INTO llm_item_errors (job_id, item_hash, model_alias, prompt_version, call_id, "
        "error, at) VALUES (1, 'h', 'haiku', 'p', 11, 'timeout', 't')"
    )
    conn.execute(
        "INSERT INTO cv_requests (id, canonical_id, job_id, decision_id, status, requested_at, "
        "last_call_id) VALUES (1,1,1,1,'done','x',12)"
    )
    return conn


def test_0015_keeps_ids_rows_and_references_and_allows_email_kind(tmp_path):
    conn = v14_db(tmp_path)
    q = {
        "calls": "SELECT * FROM llm_calls ORDER BY id",
        "cache": "SELECT * FROM llm_cache",
        "errors": "SELECT * FROM llm_item_errors",
        "cv": "SELECT * FROM cv_requests",
    }
    before = {k: [tuple(r) for r in conn.execute(v)] for k, v in q.items()}
    assert migrate(conn) == max(FILES)
    assert migrate(conn) == max(FILES)  # repeatable
    assert {k: [tuple(r) for r in conn.execute(v)] for k, v in q.items()} == before
    assert [r[0] for r in conn.execute("SELECT id FROM llm_calls ORDER BY id")] == [11, 12, 13]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.execute(
        "INSERT INTO llm_calls (kind, model_alias, n_items, started_at, local_day, status) "
        "VALUES ('email', 'haiku', 1, 't', 'd', 'ok')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO llm_calls (kind, model_alias, n_items, started_at, local_day, status) "
            "VALUES ('Other Kind', 'haiku', 1, 't', 'd', 'ok')"
        )
    with pytest.raises(sqlite3.IntegrityError):  # FKs still enforced after the rebuild
        conn.execute(
            "INSERT INTO llm_cache (item_hash, model_alias, prompt_version, output_json, call_id,"
            " created_at) VALUES ('h2', 'haiku', 'p', '{}', 9999, 't')"
        )
    idx = {r[1] for r in conn.execute("PRAGMA index_list(llm_calls)")}
    assert {"idx_llm_calls_day", "idx_llm_calls_pause"} <= idx
