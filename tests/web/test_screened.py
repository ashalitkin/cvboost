"""Issue #27: "Screened out by AI", "Fits → move to Inbox", the note on "No", /stats block."""

from __future__ import annotations

from tests.web.conftest import Ids, csrf, db, llm_verdict, verdict
from tests.web.test_decisions import post

HX = {"HX-Request": "true"}


def screen(settings, jid, **kw):
    s = db(settings)
    llm_verdict(
        s,
        jid,
        verdict(
            eligible=False,
            fit=41,
            eligibility_reason="US residents only",
            red_flags=["us_only"],
            **kw,
        ),
    )


def test_screened_lists_exactly_rules_passed_and_haiku_no_and_no_decision(
    client, seeded: Ids, settings
):
    screen(settings, seeded.b)  # Beta: Haiku said no
    page = client.get("/screened")
    assert page.status_code == 200
    assert "Beta" in page.text and "US residents only" in page.text and "us_only" in page.text
    for other in ("Acme", "Mixed"):  # eligible or not yet scored: not here
        assert other not in page.text
    assert "Beta" not in client.get("/inbox").text  # Inbox default: eligible + unscored only
    assert 'href="/screened"' in client.get("/inbox").text  # the menu item


def test_owner_decision_removes_from_screened(client, seeded: Ids, settings):
    screen(settings, seeded.b)
    post(client, f"/jobs/{seeded.b}/decision", action="rejected", reason="stack")
    assert "Beta" not in client.get("/screened").text


def test_override_moves_to_inbox_with_the_explanation_and_undo_returns(
    client, seeded: Ids, settings
):
    screen(settings, seeded.b)
    cid = seeded.b
    r = post(client, f"/jobs/{cid}/decision", action="override_in", note="Mostly dbt, fits me")
    assert r.status_code == 303 and r.headers["location"] == "/inbox"
    row = db(settings).conn.execute("SELECT * FROM decisions").fetchone()
    assert (row["action"], row["note"], row["channel"]) == (
        "override_in",
        "Mostly dbt, fits me",
        "panel",
    )
    assert "Beta" not in client.get("/screened").text
    inbox = client.get("/inbox").text
    assert "Beta" in inbox and "Returned by you:" in inbox and "Mostly dbt, fits me" in inbox
    # lives on as a normal job
    assert post(client, f"/jobs/{cid}/decision", action="interested").status_code == 303
    assert "★ Interested" in client.get("/inbox").text
    post(client, f"/jobs/{cid}/decision", action="undo")  # un-star: still the owner's pick
    assert "Returned by you:" in client.get("/inbox").text
    post(client, f"/jobs/{cid}/decision", action="undo")  # undo the override itself
    assert "Beta" in client.get("/screened").text
    assert "Beta" not in client.get("/inbox").text


def test_htmx_override_row_leaves_screened_and_undo_leaves_inbox(client, seeded: Ids, settings):
    screen(settings, seeded.b)
    tok = csrf(client)
    r = client.post(
        f"/jobs/{seeded.b}/decision",
        data={"action": "override_in", "note": "ok", "csrf_token": tok},
        headers=HX,
    )
    assert r.status_code == 200 and r.text == ""
    r = client.post(
        f"/jobs/{seeded.b}/decision",
        data={"action": "undo", "csrf_token": tok},
        headers=HX,
    )
    assert r.status_code == 200 and r.text == ""
    assert "Beta" in client.get("/screened").text


def test_override_needs_a_note_within_limit_and_a_haiku_no(client, seeded: Ids, settings):
    screen(settings, seeded.b)
    cid = seeded.b
    assert post(client, f"/jobs/{cid}/decision", action="override_in").status_code == 400
    assert (
        post(client, f"/jobs/{cid}/decision", action="override_in", note="   ").status_code == 400
    )
    assert (
        post(client, f"/jobs/{cid}/decision", action="override_in", note="n" * 501).status_code
        == 400
    )
    # Haiku did not reject Acme (eligible) -> nothing to override
    assert (
        post(
            client, f"/jobs/{seeded.a_remoteok}/decision", action="override_in", note="x"
        ).status_code
        == 409
    )
    assert db(settings).conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    ok = post(client, f"/jobs/{cid}/decision", action="override_in", note="n" * 500)
    assert ok.status_code == 303


def test_override_needs_csrf(client, seeded: Ids, settings):
    screen(settings, seeded.b)
    r = client.post(f"/jobs/{seeded.b}/decision", data={"action": "override_in", "note": "x"})
    assert r.status_code in (400, 403)
    assert db(settings).conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


def test_owner_text_is_escaped_everywhere(client, seeded: Ids, settings):
    screen(settings, seeded.b)
    evil = "<script>alert(7)</script><img src=x onerror=alert(8)>"
    post(client, f"/jobs/{seeded.b}/decision", action="override_in", note=evil)
    post(
        client, f"/jobs/{seeded.a_remoteok}/decision", action="rejected", reason="stack", note=evil
    )
    for path in ("/inbox", f"/jobs/{seeded.b}", "/stats", "/usage"):
        text = client.get(path + ("?decision=all" if path == "/inbox" else "")).text
        assert "<script>alert(7)" not in text and "<img src=x onerror=alert(8)>" not in text


def test_reject_note_may_be_500_and_is_optional(client, seeded: Ids, settings):
    r = post(
        client, f"/jobs/{seeded.b}/decision", action="rejected", reason="stack", note="n" * 500
    )
    assert r.status_code == 303
    r = post(client, f"/jobs/{seeded.a_remoteok}/decision", action="rejected", reason="rate")
    assert r.status_code == 303


def test_job_page_offers_override_only_when_haiku_said_no(client, seeded: Ids, settings):
    assert "Fits → move to Inbox" not in client.get(f"/jobs/{seeded.b}").text
    screen(settings, seeded.b)
    page = client.get(f"/jobs/{seeded.b}").text
    assert "Fits → move to Inbox" in page and 'maxlength="500"' in page
    post(client, f"/jobs/{seeded.b}/decision", action="override_in", note="because")
    page = client.get(f"/jobs/{seeded.b}").text
    assert "Returned by you:" in page and "because" in page and "Fits → move to Inbox" not in page


def test_stats_shows_owner_corrections(client, seeded: Ids, settings):
    screen(settings, seeded.b)
    post(client, f"/jobs/{seeded.b}/decision", action="override_in", note="dbt-heavy, fits")
    post(
        client,
        f"/jobs/{seeded.a_remoteok}/decision",
        action="rejected",
        reason="stack",
        note="too much Spark",
    )
    text = client.get("/stats").text
    assert "Owner corrections" in text
    assert "dbt-heavy, fits" in text and "too much Spark" in text
    assert "Beta" in text and "fit 41" in text


def test_mark_button_says_return_only_when_override_is_the_newest(client, seeded: Ids, settings):
    screen(settings, seeded.b)
    post(client, f"/jobs/{seeded.b}/decision", action="override_in", note="because")
    page = client.get("/inbox").text
    assert "Return to screened out" in page and "Undo the last one" not in page
    post(client, f"/jobs/{seeded.b}/decision", action="interested")
    for path in ("/inbox", f"/jobs/{seeded.b}"):
        page = client.get(path).text
        assert "Returned by you:" in page and "because" in page
        assert "Return to screened out" not in page and "Undo the last one" in page


def test_override_then_no_has_no_mark(client, seeded: Ids, settings):
    screen(settings, seeded.b)
    post(client, f"/jobs/{seeded.b}/decision", action="override_in", note="because")
    post(client, f"/jobs/{seeded.b}/decision", action="rejected", reason="stack")
    page = client.get("/inbox?decision=all&eligible=any").text
    assert "Returned by you:" not in page
    assert "Returned by you:" not in client.get(f"/jobs/{seeded.b}").text
