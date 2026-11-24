"""Issue #29: "Withdrawn" with a reason on an application, "Restore", OWNER CORRECTIONS."""

from __future__ import annotations

from cvboost.llm.prompts.corrections import render_corrections
from cvboost.llm.prompts.registry import score_version
from tests.conftest import NOW
from tests.web.conftest import Ids, csrf, db, llm_verdict, verdict
from tests.web.test_decisions import post


def start(client, cid):
    post(client, f"/jobs/{cid}/decision", action="prepare_cv")


def withdraw(client, cid, **kw):
    kw.setdefault("reason", "stack")
    return post(client, f"/applications/{cid}/withdraw", **kw)


def corrections(settings):
    return db(settings).owner_corrections(score_version(), 50)


def test_withdraw_moves_card_records_event_and_rejected_decision(client, seeded: Ids, settings):
    cid = seeded.b
    start(client, cid)
    r = withdraw(client, cid, reason="stack", note="needs paid account")
    assert r.status_code == 303 and r.headers["location"] == "/applications"
    conn = db(settings).conn
    assert conn.execute("SELECT status FROM applications").fetchone()[0] == "withdrawn"
    ev = conn.execute("SELECT status, note FROM application_events ORDER BY id DESC").fetchone()
    assert tuple(ev) == ("withdrawn", "stack: needs paid account")
    d = conn.execute("SELECT * FROM decisions ORDER BY id DESC").fetchone()
    assert (d["action"], d["reason"], d["note"], d["channel"], d["withdrawn"]) == (
        "rejected",
        "stack",
        "needs paid account",
        "panel",
        1,
    )
    page = client.get("/applications").text
    assert "Withdrawn" in page and "needs paid account" in page and "Restore" in page
    assert "Beta" not in client.get("/inbox").text


def test_withdraw_validation_changes_nothing(client, seeded: Ids, settings):
    cid = seeded.b
    start(client, cid)
    for kw in (
        {"reason": "other"},
        {"reason": "other", "note": "  "},
        {"reason": ""},
        {"reason": "nope"},
        {"reason": "stack", "note": "n" * 501},
    ):
        assert withdraw(client, cid, **kw).status_code == 400, kw
    no_csrf = client.post(f"/applications/{cid}/withdraw", data={"reason": "stack"})
    assert no_csrf.status_code == 403
    assert withdraw(client, cid, reason="other", note="n" * 500).status_code == 303
    conn = db(settings).conn
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE action='rejected'").fetchone()[0] == 1
    assert withdraw(client, cid).status_code == 409  # already withdrawn
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE action='rejected'").fetchone()[0] == 1


def test_withdraw_without_application_is_refused(client, seeded: Ids, settings):
    assert withdraw(client, seeded.b).status_code == 409
    assert db(settings).conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


def test_queued_cv_is_cancelled_in_progress_is_not(client, seeded: Ids, settings):
    start(client, seeded.b)
    start(client, seeded.xss)
    conn = db(settings).conn
    conn.execute("UPDATE cv_requests SET status='in_progress' WHERE canonical_id=?", (seeded.xss,))
    withdraw(client, seeded.b)
    withdraw(client, seeded.xss)
    st = {r[0]: r[1] for r in conn.execute("SELECT canonical_id, status FROM cv_requests")}
    assert st[seeded.b] == "cancelled" and st[seeded.xss] == "in_progress"
    page = client.get("/applications").text
    assert page.count("Restore") == 2
    # the CV finishing later does not move the card out of "Withdrawn"
    conn.execute("UPDATE cv_requests SET status='done' WHERE canonical_id=?", (seeded.xss,))
    rows = {r.canonical_id: r.status for r in db(settings).applications_view()}
    assert rows[seeded.xss] == "withdrawn" and rows[seeded.b] == "withdrawn"


def test_restore_returns_previous_status_and_undo_clears_correction(client, seeded: Ids, settings):
    cid = seeded.b
    llm_verdict(db(settings), cid, verdict(eligible=True, fit=77))
    post(client, f"/applications/{cid}/status", status="applied", note="mine")
    withdraw(client, cid, reason="rate", note="too low")
    assert [c["note"] for c in corrections(settings)] == ["too low"]
    assert corrections(settings)[0]["withdrawn"] is True
    r = post(client, f"/applications/{cid}/restore")
    assert r.status_code == 303
    conn = db(settings).conn
    app = conn.execute("SELECT status, note FROM applications").fetchone()
    assert tuple(app) == ("applied", "mine")  # the owner's card note survived
    assert conn.execute("SELECT action FROM decisions ORDER BY id DESC").fetchone()[0] == "undo"
    assert corrections(settings) == []
    assert post(client, f"/applications/{cid}/restore").status_code == 409
    assert "Beta" in client.get("/inbox?decision=all").text


def test_restore_of_a_derived_card_requeues_the_cancelled_cv(client, seeded: Ids, settings):
    cid = seeded.b
    start(client, cid)
    withdraw(client, cid)
    conn = db(settings).conn
    assert conn.execute("SELECT status FROM cv_requests").fetchone()[0] == "cancelled"
    post(client, f"/applications/{cid}/restore")
    assert [r[0] for r in conn.execute("SELECT status FROM cv_requests")] == ["queued"]
    assert {r.canonical_id: r.status for r in db(settings).applications_view()}[cid] == "preparing"
    # "Prepare CV" on a withdrawn job revives it without a second request
    withdraw(client, cid)
    start(client, cid)
    assert [r[0] for r in conn.execute("SELECT status FROM cv_requests")] == ["queued"]
    assert db(settings).application(cid)["status"] == "preparing"


def test_restore_does_not_touch_a_request_that_was_not_cancelled_by_it(
    client, seeded: Ids, settings
):
    cid = seeded.b
    start(client, cid)
    conn = db(settings).conn
    conn.execute("UPDATE cv_requests SET status='done', finished_at='x'")
    withdraw(client, cid)
    post(client, f"/applications/{cid}/restore")
    assert conn.execute("SELECT status FROM cv_requests").fetchone()[0] == "done"


def test_status_forms_refuse_a_withdrawn_application(client, seeded: Ids, settings):
    cid = seeded.b
    start(client, cid)
    withdraw(client, cid)
    for path in (f"/applications/{cid}/status", f"/jobs/{cid}/status"):
        assert post(client, path, status="applied").status_code == 409
        assert post(client, path, status="").status_code == 409
    assert db(settings).application(cid)["status"] == "withdrawn"


def test_double_restore_is_409_everywhere(client, seeded: Ids):
    cid = seeded.b
    start(client, cid)
    withdraw(client, cid)
    assert post(client, f"/applications/{cid}/restore").status_code == 303
    assert post(client, f"/applications/{cid}/restore").status_code == 409
    assert post(client, f"/jobs/{cid}/decision", action="prepare_cv").status_code == 303


def test_prompt_block_caps_withdrawn_corrections(client, seeded: Ids, settings):
    store = db(settings)
    ids = [seeded.b, seeded.xss, seeded.mixed_alive]
    for cid in ids:
        llm_verdict(store, cid, verdict(eligible=True, fit=80))
        start(client, cid)
        withdraw(client, cid, reason="rate", note=f"w{cid}")
    llm_verdict(store, seeded.a_remoteok, verdict(eligible=True, fit=80))
    post(
        client,
        f"/jobs/{seeded.a_remoteok}/decision",
        action="rejected",
        reason="stack",
        note="plain",
    )
    got = store.owner_corrections(score_version(), 15, 1)
    assert sorted(c["withdrawn"] for c in got) == [False, True]
    assert len(store.owner_corrections(score_version(), 15)) == 4  # no cap: all of them


def test_restore_under_a_newer_decision_writes_no_undo_but_ends_correction(
    client, seeded: Ids, settings
):
    cid = seeded.b
    llm_verdict(db(settings), cid, verdict(eligible=True, fit=77))
    start(client, cid)
    withdraw(client, cid)
    post(client, f"/jobs/{cid}/decision", action="interested")  # newest word is not "Withdrawn"
    conn = db(settings).conn
    n = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    post(client, f"/applications/{cid}/restore")
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == n  # nothing popped
    assert (
        conn.execute("SELECT action FROM decisions ORDER BY id DESC").fetchone()[0] == "interested"
    )
    assert corrections(settings) == []


def test_inbox_undo_on_a_withdrawn_job_is_the_same_restore(client, seeded: Ids, settings):
    cid = seeded.b
    start(client, cid)
    withdraw(client, cid)
    post(client, f"/jobs/{cid}/decision", action="undo")
    app = db(settings).application(cid)
    assert app["status"] == "preparing"


def test_prompt_block_marks_changed_mind_and_keeps_cache_key(client, seeded: Ids, settings):
    before = score_version()
    cid = seeded.b
    llm_verdict(db(settings), cid, verdict(eligible=True, fit=90))
    start(client, cid)
    withdraw(client, cid, reason="location", note="paid account for <b>apply</b>")
    block, rev = render_corrections(corrections(settings))
    assert "changed their mind" in block and "location" in block
    assert "&lt;b&gt;" in block and "<b>" not in block
    assert rev and score_version() == before
    withdraw_no_text = render_corrections(
        [
            {
                "title": "t",
                "company": "c",
                "action": "rejected",
                "reason": "rate",
                "note": None,
                "withdrawn": True,
                "ai_eligible": True,
                "ai_fit": 70,
            }
        ]
    )[0]
    assert "(no text" in withdraw_no_text


def test_stats_and_job_page_show_it(client, seeded: Ids, settings):
    cid = seeded.b
    llm_verdict(db(settings), cid, verdict(eligible=True, fit=90))
    start(client, cid)
    assert "Withdraw</summary>" in client.get(f"/jobs/{cid}").text  # the form on the job page
    withdraw(client, cid, reason="other", note="<script>x</script>")
    assert "withdrew after choosing" in client.get("/stats").text
    job = client.get(f"/jobs/{cid}").text
    assert "Restore" in job and "<script>x</script>" not in job
    assert "<script>x</script>" not in client.get("/applications").text
    r = post(client, f"/applications/{cid}/restore", next="job")
    assert r.headers["location"] == f"/jobs/{cid}"


def test_job_page_without_application_has_no_withdraw_form(client, seeded: Ids):
    assert "/withdraw" not in client.get(f"/jobs/{seeded.b}").text


def test_status_form_cannot_set_withdrawn(client, seeded: Ids):
    start(client, seeded.b)
    assert post(client, f"/applications/{seeded.b}/status", status="withdrawn").status_code == 400
    _ = csrf, NOW
