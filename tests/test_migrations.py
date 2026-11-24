from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cvboost.storage.db import _migration_files, connect, migrate

VERSIONS = [v for v, _ in _migration_files()]  # grows with every migration
LATEST = VERSIONS[-1]


def test_migrate_is_idempotent(tmp_path):
    conn = connect(tmp_path / "sub" / "db.sqlite")
    assert migrate(conn) == LATEST
    assert migrate(conn) == LATEST
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert [r[0] for r in rows] == VERSIONS
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "sources",
        "source_runs",
        "jobs",
        "sightings",
        "verdicts",
        "llm_calls",
        "llm_cache",
        "llm_item_errors",
        "worker_state",
        "worker_cycles",
        "alerts",
        "decisions",
        "cv_requests",
        "system_alerts",
        "applications",
        "application_events",
    } <= tables
    assert LATEST == VERSIONS[-1] >= 28  # core 1-19 and 0028, plus the loaded plugins' own
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"company_norm", "title_norm"} <= cols


def test_upgrade_from_v1_keeps_rows(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    sql1 = dict(_migration_files())[1]
    conn.executescript(
        "BEGIN;\n" + sql1 + "\nCREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
        "applied_at TEXT NOT NULL);\nINSERT INTO schema_migrations VALUES (1, 'x');\nCOMMIT;"
    )
    conn.execute("INSERT INTO sources (name, created_at) VALUES ('s', 'x')")
    conn.execute(
        "INSERT INTO jobs (id, canonical_id, source, source_id, url, company, title, "
        "description_html, description_text, tags_json, fetched_at, raw_json, content_hash, "
        "first_seen_at, last_seen_at) VALUES (1,1,'s','1','https://e.com','C','T','','','[]',"
        "'x','{}','h','x','x')"
    )
    assert migrate(conn) == LATEST
    row = conn.execute("SELECT company, company_norm, title_norm FROM jobs").fetchone()
    assert row["company"] == "C" and row["company_norm"] is None and row["title_norm"] is None


def test_migration_files_exist():
    """Versions are unique and ascending; a plugin may own a range (the core reserves 20-27)."""
    assert Path(__file__).exists() and VERSIONS == sorted(set(VERSIONS))
    assert VERSIONS == [v for v, _ in _migration_files()] and VERSIONS[:19] == list(range(1, 20))


def test_upgrade_from_v2_to_v3_keeps_data(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    files = dict(_migration_files())
    conn.executescript(
        "BEGIN;\n" + files[1] + files[2] + "\nCREATE TABLE schema_migrations (version INTEGER "
        "PRIMARY KEY, applied_at TEXT NOT NULL);\nINSERT INTO schema_migrations VALUES (1,'x');"
        "\nINSERT INTO schema_migrations VALUES (2,'x');\nCOMMIT;"
    )
    conn.execute("INSERT INTO sources (name, created_at) VALUES ('s', 'x')")
    assert migrate(conn) == LATEST
    assert conn.execute("SELECT name FROM sources").fetchone()[0] == "s"
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0


def test_upgrade_from_v3_adds_worker_tables_and_constraints(tmp_path):
    import sqlite3

    import pytest

    conn = connect(tmp_path / "db.sqlite")
    files = dict(_migration_files())
    conn.executescript(
        "BEGIN;\n" + files[1] + files[2] + files[3] + "\nCREATE TABLE schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);\n"
        "INSERT INTO schema_migrations VALUES (1,'x'),(2,'x'),(3,'x');\nCOMMIT;"
    )
    conn.execute("INSERT INTO sources (name, created_at) VALUES ('s', 'x')")
    assert migrate(conn) == LATEST
    assert migrate(conn) == LATEST
    assert conn.execute("SELECT name FROM sources").fetchone()[0] == "s"
    conn.execute(
        "INSERT INTO jobs (id, canonical_id, source, source_id, url, company, title, "
        "description_html, description_text, tags_json, fetched_at, raw_json, content_hash, "
        "first_seen_at, last_seen_at) VALUES (1,1,'s','1','https://e.com','C','T','','','[]',"
        "'x','{}','h','x','x')"
    )
    ins = (
        "INSERT INTO decisions (canonical_id, job_id, action, reason, channel, at) "
        "VALUES (1,1,?,?,?, 'x')"
    )
    conn.execute(ins, ("rejected", "seniority", "panel"))  # PM contract: seniority + panel
    for bad in (
        ("rejected", "weather", "telegram"),
        ("maybe", None, "telegram"),
        ("interested", None, "web"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(ins, bad)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO decisions (canonical_id, job_id, action, note, channel, at) "
            "VALUES (1,1,'undo',?, 'panel', 'x')",
            ("n" * 501,),
        )
    # one active cv request per job
    conn.execute(
        "INSERT INTO decisions (canonical_id, job_id, action, channel, at) "
        "VALUES (1,1,'cv_requested','telegram','x')"
    )
    did = conn.execute("SELECT MAX(id) FROM decisions").fetchone()[0]
    q = (
        "INSERT INTO cv_requests (canonical_id, job_id, decision_id, status, requested_at) "
        "VALUES (1,1,?,?,'x')"
    )
    conn.execute(q, (did, "queued"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(q, (did, "in_progress"))
    conn.execute("UPDATE cv_requests SET status='done'")
    conn.execute(q, (did, "queued"))  # allowed again once the first is finished
    assert {r[1] for r in conn.execute("PRAGMA table_info(cv_requests)")} >= {"kit_dir", "error"}


def test_0010_upgrade_from_v9_keeps_decisions_and_widens_checks(tmp_path, monkeypatch):
    from cvboost.storage import db as dbmod
    from cvboost.storage.db import Store
    from tests.rules_helpers import add, sync

    files = _migration_files()
    monkeypatch.setattr(dbmod, "_migration_files", lambda: [f for f in files if f[0] <= 9])
    conn = connect(tmp_path / "v9.sqlite")
    assert migrate(conn) == 9
    store = Store(conn)
    sync(store, "fake")
    jid = add(store, "fake", "1", company="Acme", title="Data Engineer")
    conn.execute(
        "INSERT INTO alerts (id, canonical_id, job_id, status, created_at) "
        "VALUES (1, ?, ?, 'sent', 't0')",
        (jid, jid),
    )
    conn.execute(
        "INSERT INTO decisions (id, canonical_id, job_id, action, reason, note, channel, "
        "alert_id, tg_update_id, at) "
        "VALUES (1,?,?,'rejected','stack','a note','telegram',1,777,'t1')",
        (jid, jid),
    )
    conn.execute(
        "INSERT INTO decisions (id, canonical_id, job_id, action, channel, at) "
        "VALUES (2,?,?,'cv_requested','panel','t2')",
        (jid, jid),
    )
    conn.execute(
        "INSERT INTO cv_requests (canonical_id, job_id, decision_id, status, requested_at) "
        "VALUES (?,?,2,'queued','t3')",
        (jid, jid),
    )
    before = [
        tuple(r)
        for r in conn.execute(
            "SELECT id, canonical_id, job_id, action, reason, note, channel, alert_id, "
            "tg_update_id, at FROM decisions ORDER BY id"
        )
    ]
    assert len(before) == 2
    monkeypatch.undo()

    assert migrate(conn) == LATEST
    after = [
        tuple(r)
        for r in conn.execute(
            "SELECT id, canonical_id, job_id, action, reason, note, channel, alert_id, "
            "tg_update_id, at FROM decisions ORDER BY id"
        )
    ]
    assert after == before
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    ins = (
        "INSERT INTO decisions (canonical_id, job_id, action, note, channel, at) "
        "VALUES (?,?,?,?,'panel','t')"
    )
    conn.execute(ins, (jid, jid, "override_in", "n" * 500))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, (jid, jid, "override_in", "n" * 501))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, (jid, jid, "maybe", None))
    idx = {r[1] for r in conn.execute("PRAGMA index_list(decisions)")}
    assert {"idx_decisions_job", "idx_decisions_canonical"} <= idx
    fks = {(r[2], r[3]) for r in conn.execute("PRAGMA foreign_key_list(cv_requests)")}
    assert ("decisions", "decision_id") in fks
    assert conn.execute("SELECT decision_id FROM cv_requests").fetchone()[0] == 2
    assert conn.execute("SELECT alert_id, tg_update_id FROM decisions WHERE id=1").fetchone()[
        :
    ] == (1, 777)


def test_0011_upgrade_from_v10_keeps_rows_and_allows_withdrawn(tmp_path, monkeypatch):
    from cvboost.storage import db as dbmod
    from cvboost.storage.db import Store
    from tests.rules_helpers import add, sync

    files = _migration_files()
    monkeypatch.setattr(dbmod, "_migration_files", lambda: [f for f in files if f[0] <= 10])
    conn = connect(tmp_path / "v10.sqlite")
    assert migrate(conn) == 10
    store = Store(conn)
    sync(store, "fake")
    jid = add(store, "fake", "1", company="Acme", title="Data Engineer")
    with pytest.raises(sqlite3.IntegrityError):  # the old CHECK
        conn.execute("INSERT INTO applications VALUES (?, 'withdrawn', NULL, 't')", (jid,))
    conn.execute("INSERT INTO applications VALUES (?, 'applied', 'my note', 't1')", (jid,))
    conn.execute(
        "INSERT INTO application_events (canonical_id, status, note, at) "
        "VALUES (?, 'applied', 'my note', 't1')",
        (jid,),
    )
    conn.execute(
        "INSERT INTO decisions (id, canonical_id, job_id, action, reason, note, channel, at) "
        "VALUES (1,?,?,'rejected','stack','a note','panel','t2')",
        (jid, jid),
    )
    q = {
        "applications": "SELECT * FROM applications",
        "events": "SELECT * FROM application_events",
        "decisions": "SELECT id, canonical_id, job_id, action, reason, note, channel, at "
        "FROM decisions",
    }
    before = {k: [tuple(r) for r in conn.execute(v)] for k, v in q.items()}
    monkeypatch.undo()

    assert migrate(conn) == LATEST
    assert {k: [tuple(r) for r in conn.execute(v)] for k, v in q.items()} == before
    assert conn.execute("SELECT withdrawn FROM decisions").fetchone()[0] == 0  # old rows: not
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.execute("UPDATE applications SET status = 'withdrawn'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE applications SET status = 'maybe'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE decisions SET withdrawn = 2")


def test_0012_job_views_from_v11_keeps_rows_and_backfills_decided(tmp_path, monkeypatch):
    from cvboost.storage import db as dbmod
    from cvboost.storage.db import Store
    from tests.rules_helpers import add, sync

    files = _migration_files()
    monkeypatch.setattr(dbmod, "_migration_files", lambda: [f for f in files if f[0] <= 11])
    conn = connect(tmp_path / "v11.sqlite")
    assert migrate(conn) == 11
    store = Store(conn)
    sync(store, "fake")
    decided = add(store, "fake", "1", company="Acme", title="Data Engineer")
    applied = add(store, "fake", "2", company="Beta", title="Analytics Engineer")
    fresh = add(store, "fake", "3", company="Gamma", title="BI Engineer")
    conn.execute(
        "INSERT INTO decisions (canonical_id, job_id, action, channel, at) "
        "VALUES (?,?,'interested','panel','2026-11-01T10:00:00+00:00')",
        (decided, decided),
    )
    conn.execute("INSERT INTO applications VALUES (?, 'applied', NULL, 't1')", (applied,))
    q = {
        "jobs": "SELECT * FROM jobs ORDER BY id",
        "decisions": "SELECT * FROM decisions ORDER BY id",
        "applications": "SELECT * FROM applications",
    }
    before = {k: [tuple(r) for r in conn.execute(v)] for k, v in q.items()}
    monkeypatch.undo()

    assert migrate(conn) == LATEST
    assert migrate(conn) == LATEST  # repeatable
    assert {k: [tuple(r) for r in conn.execute(v)] for k, v in q.items()} == before
    views = {r[0]: r[1] for r in conn.execute("SELECT canonical_id, seen_at FROM job_views")}
    assert views == {decided: "2026-11-01T10:00:00+00:00", applied: "t1"}  # fresh stays unseen
    assert fresh not in views
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with pytest.raises(sqlite3.IntegrityError):  # FK to jobs
        conn.execute("INSERT INTO job_views VALUES (9999, 't')")
    with pytest.raises(sqlite3.IntegrityError):  # one row per group
        conn.execute("INSERT INTO job_views VALUES (?, 't')", (decided,))


def test_0014_0015_email_messages_and_llm_calls_email_kind(tmp_path):
    conn = connect(tmp_path / "m.sqlite")
    assert migrate(conn) == LATEST
    assert migrate(conn) == LATEST
    conn.execute("INSERT INTO sources (name, enabled, created_at) VALUES ('s', 1, 't')")
    for kind in ("score", "preflight", "cv", "email"):
        conn.execute(
            "INSERT INTO llm_calls (kind, model_alias, n_items, started_at, local_day, status) "
            "VALUES (?, 'haiku', 1, 't', 'd', 'ok')",
            (kind,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO llm_calls (kind, model_alias, n_items, started_at, local_day, status) "
            "VALUES ('Bogus Kind', 'haiku', 1, 't', 'd', 'ok')"
        )
    ins = (
        "INSERT INTO email_messages (mailbox, uidvalidity, uid, part, msgid_hash, first_seen_at, "
        "status) VALUES ('INBOX', 1, ?, ?, 'h', 't', ?)"
    )
    for status in ("parsed", "needs_llm", "crm", "duplicate", "parse_error"):
        conn.execute(ins, (hash(status) % 10**6, 0, status))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, (1, 0, "nonsense"))
    conn.execute(ins, (777, 0, "crm"))
    conn.execute(ins, (777, 1, "crm"))  # a second message unwrapped from the same UID
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, (777, 1, "crm"))
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
