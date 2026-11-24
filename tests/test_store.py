from __future__ import annotations

from datetime import timedelta

import pytest

from tests.conftest import NOW, make_job


def test_job_validators():
    with pytest.raises(ValueError):
        make_job(url="javascript:alert(1)")
    with pytest.raises(ValueError):
        make_job(fetched_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        make_job(title="  ")


def test_content_hash_ignores_whitespace():
    a = make_job(description_text="a  b\n c")
    b = make_job(description_text="a b c")
    assert a.content_hash == b.content_hash
    assert a.content_hash != make_job(description_text="other").content_hash


def _sync(store):
    from cvboost.connectors.base import Connector

    class Fake(Connector):
        name = "fake"
        default_enabled = True

    store.sync_sources({"fake": Fake}, NOW)  # type: ignore[dict-item]


def test_upsert_new_then_seen_again(store):
    _sync(store)
    run1 = store.start_run("fake", NOW)
    with store.transaction():
        job_id, is_new = store.upsert_job(make_job(), run1, NOW)
    assert is_new
    run2 = store.start_run("fake", NOW + timedelta(hours=1))
    with store.transaction():
        job_id2, is_new2 = store.upsert_job(make_job(), run2, NOW + timedelta(hours=1))
    assert job_id2 == job_id and not is_new2
    assert store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 2
    row = store.conn.execute("SELECT id, canonical_id FROM jobs").fetchone()
    assert row["canonical_id"] == row["id"]


def test_upsert_changed_content_updates(store):
    _sync(store)
    run = store.start_run("fake", NOW)
    with store.transaction():
        store.upsert_job(make_job(), run, NOW)
    run2 = store.start_run("fake", NOW)
    with store.transaction():
        store.upsert_job(make_job(description_text="New text"), run2, NOW)
    row = store.conn.execute("SELECT description_text FROM jobs").fetchone()
    assert row[0] == "New text"


def test_upsert_only_salary_max_changed_updates_row(store):
    _sync(store)
    run = store.start_run("fake", NOW)
    with store.transaction():
        store.upsert_job(make_job(salary_max=100), run, NOW)
    run2 = store.start_run("fake", NOW)
    with store.transaction():
        _, is_new = store.upsert_job(make_job(salary_max=150), run2, NOW)
    assert not is_new
    assert store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert store.conn.execute("SELECT salary_max FROM jobs").fetchone()[0] == 150


def test_upsert_outside_transaction_is_refused(store):
    _sync(store)
    run = store.start_run("fake", NOW)
    with pytest.raises(AssertionError):
        store.upsert_job(make_job(), run, NOW)


def test_mark_stale_runs(store):
    _sync(store)
    old = store.start_run("fake", NOW - timedelta(hours=3))
    fresh = store.start_run("fake", NOW - timedelta(minutes=5))
    assert store.mark_stale_runs(timedelta(hours=1), NOW) == 1
    r = {r["id"]: r for r in store.conn.execute("SELECT * FROM source_runs")}
    assert r[old]["status"] == "error" and r[old]["error_type"] == "interrupted"
    assert r[fresh]["status"] == "running"


def test_consecutive_failures_and_health(store):
    _sync(store)
    for i in range(2):
        rid = store.start_run("fake", NOW + timedelta(hours=i))
        store.finish_run(
            rid,
            status="error",
            found=0,
            new=0,
            invalid=0,
            error_type="X",
            error_message="boom",
            now=NOW,
        )
    assert store.source_state("fake").consecutive_failures == 2
    h = store.source_health()[0]
    assert h.last_error == "X: boom" and h.last_success_at is None
    rid = store.start_run("fake", NOW + timedelta(hours=5))
    store.finish_run(rid, status="ok", found=1, new=1, invalid=0, now=NOW)
    assert store.source_state("fake").consecutive_failures == 0


def test_model_strips_control_and_bidi_chars():
    job = make_job(
        title="Data\x00 Eng\u202eineer\u200b",
        company="Ac\x07me",
        location_text="Remote\ufeff",
        description_text="line1\nline2\x1b[31m\ttab\u2066",
    )
    assert job.title == "Data Engineer"
    assert job.company == "Acme"
    assert job.location_text == "Remote"
    assert job.description_text == "line1\nline2[31m\ttab"
    with pytest.raises(ValueError):
        make_job(title="\x00\u200b")


def test_last_auth_info_method_comes_from_preflight_not_later_score_rows(store):
    def call(kind, **kw):
        store.insert_llm_call(
            kind=kind,
            model_alias="haiku",
            prompt_version=None,
            n_items=0,
            attempt=1,
            now=NOW,
            local_day="2026-11-06",
            status="ok",
            **kw,
        )

    assert store.last_auth_info() == (None, None)
    call("preflight", auth_method="claude.ai", credential_expires_at="2026-11-26")
    call("score", credential_expires_at="2026-11-27")  # later row: expiry only
    assert store.last_auth_info() == ("claude.ai", "2026-11-27")
