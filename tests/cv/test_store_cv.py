from __future__ import annotations

from datetime import timedelta

from tests.conftest import NOW
from tests.cv.helpers import queue_request


def row(store, rid):
    return store.conn.execute("SELECT * FROM cv_requests WHERE id=?", (rid,)).fetchone()


def test_claim_takes_the_oldest_queued_and_counts_the_attempt(store):
    a, b = queue_request(store, 2)
    got = store.claim_next_cv_request(NOW, 2)
    assert got["id"] == a[0] and got["status"] == "in_progress" and got["attempts"] == 1
    assert store.claim_next_cv_request(NOW, 2)["id"] == b[0]
    assert store.claim_next_cv_request(NOW, 2) is None


def test_claim_skips_requests_out_of_attempts(store):
    ((rid, _, _),) = queue_request(store)
    store.conn.execute("UPDATE cv_requests SET attempts=2")
    assert store.claim_next_cv_request(NOW, 2) is None
    assert not store.has_queued_cv_request(2)


def test_requeue_and_refund(store):
    ((rid, _, _),) = queue_request(store)
    store.claim_next_cv_request(NOW, 2)
    assert store.requeue_cv_request(rid, feedback="fix it", refund=True)
    r = row(store, rid)
    assert (r["status"], r["attempts"], r["feedback"], r["started_at"]) == (
        "queued",
        0,
        "fix it",
        None,
    )
    assert not store.requeue_cv_request(rid, feedback=None, refund=False)  # not in progress


def test_finish_only_from_in_progress(store):
    ((rid, _, cid),) = queue_request(store)
    assert not store.finish_cv_request(rid, status="done", now=NOW)  # still queued
    store.claim_next_cv_request(NOW, 2)
    store.conn.execute("UPDATE cv_requests SET status='cancelled' WHERE id=?", (rid,))
    assert not store.finish_cv_request(rid, status="done", now=NOW)
    assert row(store, rid)["status"] == "cancelled"


def test_finish_stores_reasons_and_clips_error(store):
    ((rid, _, cid),) = queue_request(store)
    store.claim_next_cv_request(NOW, 2)
    assert store.finish_cv_request(
        rid, status="needs_review", now=NOW, kit_dir=str(cid), review_reasons=["a:b", "c"],
        error="e" * 500, pdf_status="ok",
    )  # fmt: skip
    r = row(store, rid)
    assert r["review_reasons"] == '["a:b", "c"]' and len(r["error"]) == 300


def test_recover_stale(store):
    ((rid, _, _),) = queue_request(store)
    store.claim_next_cv_request(NOW, 2)
    assert store.recover_stale_cv_requests(NOW - timedelta(minutes=1), 2, NOW) == (0, [])
    assert store.recover_stale_cv_requests(NOW + timedelta(minutes=1), 2, NOW) == (1, [])
    assert row(store, rid)["status"] == "queued"


def test_recover_fails_exhausted_requests_with_finished_at(store):
    (a, _, _), (b, _, _) = queue_request(store, 2)
    store.claim_next_cv_request(NOW, 2)
    store.conn.execute("UPDATE cv_requests SET attempts=2 WHERE id=?", (a,))  # in progress, spent
    store.conn.execute("UPDATE cv_requests SET attempts=2 WHERE id=?", (b,))  # queued, spent
    requeued, failed = store.recover_stale_cv_requests(NOW + timedelta(hours=1), 2, NOW)
    assert requeued == 0 and sorted(failed) == sorted([a, b])
    for rid in (a, b):
        r = row(store, rid)
        assert r["status"] == "failed" and r["finished_at"] and r["error"] == "attempts used up"


def test_latest_edit_and_approve(store):
    ((rid, _, cid),) = queue_request(store)
    assert store.latest_cv_request(cid)["id"] == rid  # #6's group-aware lookup
    store.claim_next_cv_request(NOW, 2)
    store.finish_cv_request(rid, status="needs_review", now=NOW, kit_dir=str(cid))
    assert store.mark_kit_edited(cid, pdf_status="stale", now=NOW)
    r = row(store, rid)
    assert r["edited_at"] and r["pdf_status"] == "stale"
    assert store.approve_kit(cid, now=NOW) and row(store, rid)["status"] == "done"
    assert not store.approve_kit(cid, now=NOW)  # already done
