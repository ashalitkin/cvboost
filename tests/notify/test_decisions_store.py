"""Store contract shared with #6 (panel) and #7 (CV drafting)."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from tests.conftest import NOW
from tests.rules_helpers import add, sync


@pytest.fixture
def ids(s):
    sync(s, "a", "b")
    one = add(s, "a", "1", company="X", title="Data Engineer")
    two = add(s, "b", "2", company="X", title="Data Engineer")
    s.conn.execute("UPDATE jobs SET canonical_id = ? WHERE id = ?", (one, two))
    other = add(s, "a", "3", company="Y", title="Analyst")
    return one, two, other


def test_record_decision_panel_channel_and_fields(s, ids):
    one, two, _ = ids
    did, created = s.record_decision(
        two, "rejected", channel="panel", reason="seniority", note="  too junior  ", now=NOW
    )
    row = s.conn.execute("SELECT * FROM decisions WHERE id = ?", (did,)).fetchone()
    assert created and row["channel"] == "panel" and row["reason"] == "seniority"
    assert row["note"] == "too junior" and row["canonical_id"] == one and row["job_id"] == two


def test_validation(s, ids):
    one = ids[0]
    with pytest.raises(ValueError):
        s.record_decision(one, "cv_requested", channel="panel", now=NOW)
    with pytest.raises(ValueError):
        s.record_decision(one, "interested", channel="panel", reason="rate", now=NOW)
    with pytest.raises(ValueError):
        s.record_decision(999, "interested", channel="panel", now=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        s.record_decision(one, "rejected", channel="fax", now=NOW)


def test_note_is_cut_to_500(s, ids):
    did, _ = s.record_decision(ids[0], "undo", channel="panel", note="n" * 900, now=NOW)
    assert len(s.conn.execute("SELECT note FROM decisions WHERE id=?", (did,)).fetchone()[0]) == 500


def test_latest_decisions_covers_the_whole_group_and_undo_is_visible(s, ids):
    one, two, other = ids
    s.record_decision(one, "interested", channel="telegram", now=NOW)
    got = s.latest_decisions([one, two, other])
    assert set(got) == {one, two} and got[two]["action"] == "interested"  # member of the group
    s.record_decision(
        two, "rejected", channel="panel", reason="stack", now=NOW + timedelta(minutes=1)
    )
    assert s.latest_decisions([one])[one]["action"] == "rejected"
    s.record_decision(one, "undo", channel="panel", now=NOW + timedelta(minutes=2))
    assert s.latest_decisions([one, two])[two]["action"] == "undo"


def test_request_cv_is_one_transaction_and_idempotent(s, ids):
    one, two, _ = ids
    did, rid, created = s.request_cv(one, channel="panel", now=NOW)
    assert created
    did2, rid2, created2 = s.request_cv(two, channel="panel", now=NOW)  # same group
    assert (rid2, created2) == (rid, False) and did2 != did
    assert s.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 2  # the word is kept
    assert s.conn.execute("SELECT COUNT(*) FROM cv_requests").fetchone()[0] == 1
    req = s.conn.execute("SELECT * FROM cv_requests").fetchone()
    assert (req["status"], req["canonical_id"], req["job_id"], req["kit_dir"]) == (
        "queued",
        one,
        one,
        None,
    )


def test_request_cv_redelivery_by_update_id(s, ids):
    a = s.request_cv(ids[0], channel="telegram", tg_update_id=5, now=NOW)
    b = s.request_cv(ids[0], channel="telegram", tg_update_id=5, now=NOW)
    assert a[:2] == b[:2] and b[2] is False


def test_failed_request_can_be_requested_again(s, ids):
    s.request_cv(ids[0], channel="panel", now=NOW)
    s.conn.execute("UPDATE cv_requests SET status='failed', error='boom'")
    _, rid, created = s.request_cv(ids[0], channel="panel", now=NOW)
    assert created and s.conn.execute("SELECT COUNT(*) FROM cv_requests").fetchone()[0] == 2


def test_failed_transaction_leaves_nothing(s, ids):
    # a request for a job that does not exist must not leave an orphan decision behind
    with pytest.raises(ValueError):
        s.request_cv(12345, channel="panel", now=NOW)
    assert s.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    assert not s.conn.in_transaction
