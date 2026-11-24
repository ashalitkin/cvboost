from __future__ import annotations

from tests.web.conftest import Ids, csrf, db


def post(client, path, **data):
    return client.post(path, data={"csrf_token": csrf(client), **data})


def test_reject_with_reason_hides_row(client, seeded: Ids, settings):
    cid = seeded.b
    assert "Beta" in client.get("/inbox").text
    r = post(client, f"/jobs/{cid}/decision", action="rejected", reason="rate", note="too low")
    assert r.status_code == 303 and r.headers["location"] == "/inbox"
    row = db(settings).conn.execute("SELECT * FROM decisions").fetchone()
    assert (row["canonical_id"], row["action"], row["reason"], row["channel"], row["note"]) == (
        cid,
        "rejected",
        "rate",
        "panel",
        "too low",
    )
    assert "Beta" not in client.get("/inbox").text
    assert "Beta" in client.get("/inbox?decision=all").text


def test_reject_needs_reason_and_valid_values(client, seeded: Ids, settings):
    cid = seeded.b
    assert post(client, f"/jobs/{cid}/decision", action="rejected").status_code == 400
    assert (
        post(client, f"/jobs/{cid}/decision", action="rejected", reason="nope").status_code == 400
    )
    assert post(client, f"/jobs/{cid}/decision", action="explode").status_code == 400
    assert (
        post(
            client, f"/jobs/{cid}/decision", action="rejected", reason="stack", note="n" * 501
        ).status_code
        == 400
    )
    assert db(settings).conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


def test_htmx_reject_returns_empty_fragment(client, seeded: Ids):
    tok = csrf(client)
    r = client.post(
        f"/jobs/{seeded.b}/decision",
        data={"action": "rejected", "reason": "stack", "csrf_token": tok},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and r.text == ""


def test_interested_stays_with_star_and_undo_clears(client, seeded: Ids):
    cid = seeded.b
    r = client.post(
        f"/jobs/{cid}/decision",
        data={"action": "interested", "csrf_token": csrf(client)},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and "★" in r.text and "Beta" in r.text
    assert "★ Interested" in client.get("/inbox").text
    post(client, f"/jobs/{cid}/decision", action="undo")
    assert "★ Interested" not in client.get("/inbox").text


def test_prepare_cv_twice_one_active_request_and_in_applications(client, seeded: Ids, settings):
    cid = seeded.b
    post(client, f"/jobs/{cid}/decision", action="prepare_cv")
    post(client, f"/jobs/{cid}/decision", action="prepare_cv")
    s = db(settings)
    active = s.conn.execute(
        "SELECT COUNT(*) FROM cv_requests WHERE status IN ('queued','in_progress')"
    ).fetchone()[0]
    assert active == 1
    assert "Beta" not in client.get("/inbox").text
    apps = client.get("/applications").text
    assert "Analytics Engineer" in apps and "Preparing CV" in apps


def test_decision_on_group_member_hides_group(client, seeded: Ids, settings):
    # decision stored on the non-canonical member id (like a Telegram button on that card)
    s = db(settings)
    s.record_decision(
        seeded.a_remotive,
        "rejected",
        channel="telegram",
        reason="stack",
        now=__import__("tests.conftest", fromlist=["NOW"]).NOW,
    )
    assert "Senior Data Engineer" not in client.get("/inbox").text


def test_decision_on_member_id_redirects(client, seeded: Ids):
    r = client.post(
        f"/jobs/{seeded.a_remotive}/decision",
        data={"action": "interested", "csrf_token": csrf(client)},
    )
    assert r.status_code == 307
