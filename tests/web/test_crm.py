from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from cvboost.crm import store as S
from cvboost.crm.types import ParsedEvent
from cvboost.storage.db import migrate
from cvboost.web.app import create_app
from tests.conftest import NOW
from tests.web.conftest import csrf, db

T0 = NOW - timedelta(days=2)


def make(settings) -> TestClient:
    return TestClient(create_app(settings, clock=lambda: NOW), follow_redirects=False)


def add_event(settings, name, at=T0, mid=None, **kw):
    st = db(settings)
    migrate(st.conn)
    kw.setdefault("kind", "message")
    kw.setdefault("snippet", "hello")
    with st.transaction():
        S.record_event(st.conn, ParsedEvent(name=name, **kw), at, mid or f"{name}-{at}", NOW)
    return st.conn.execute("SELECT id FROM crm_contacts WHERE name=?", (name,)).fetchone()[0]


def post(c, path, **data):
    return c.post(path, data={"csrf_token": csrf(c, "/crm"), **data})


def row(settings, cid):
    return db(settings).conn.execute("SELECT * FROM crm_contacts WHERE id=?", (cid,)).fetchone()


def test_nav_has_crm_and_empty_page_renders(settings):
    page = make(settings).get("/crm")
    assert page.status_code == 200 and 'href="/crm"' in page.text and "No contacts yet" in page.text


def test_three_columns_order_and_bold_unread(settings):
    a = add_event(settings, "Alice Old", T0)
    b = add_event(settings, "Bob New", T0 + timedelta(hours=5))
    c = make(settings)
    post(c, f"/crm/{a}/move", col="leads")
    post(c, f"/crm/{a}/read")
    page = c.get("/crm").text
    assert "Inbox" in page and "Leads" in page and "Talking" in page
    inbox, leads = page.split("<h2>Leads")[0], page.split("<h2>Leads")[1]
    assert "Bob New" in inbox and "Alice Old" in leads
    assert "crm-card unread" in inbox and "crm-card unread" not in leads  # only Bob is bold
    assert row(settings, b)["unread"] == 1 and row(settings, a)["unread"] == 0


def test_cards_ordered_by_last_event(settings):
    add_event(settings, "First", T0)
    add_event(settings, "Second", T0 + timedelta(hours=1))
    page = make(settings).get("/crm").text
    assert page.index("Second") < page.index("First")


def test_opening_the_contact_marks_it_read(settings):
    a = add_event(settings, "Alice", view_url="https://www.linkedin.com/comm/messaging/thread/2-X/")
    c = make(settings)
    page = c.get(f"/crm/{a}")
    assert page.status_code == 200 and row(settings, a)["unread"] == 0
    assert 'href="https://www.linkedin.com/comm/messaging/thread/2-X/"' in page.text
    assert "View the message" in page.text


def test_move_archive_and_return(settings):
    a = add_event(settings, "Alice")
    c = make(settings)
    assert post(c, f"/crm/{a}/move", col="talk").status_code == 303
    assert row(settings, a)["col"] == "talk"
    post(c, f"/crm/{a}/archive")
    assert row(settings, a)["archived"] == 1
    assert "Alice" not in c.get("/crm").text and "Alice" in c.get("/crm?archive=1").text
    add_event(settings, "Alice", T0 + timedelta(hours=3), mid="again")  # new event: back to inbox
    assert (row(settings, a)["archived"], row(settings, a)["col"]) == (0, "inbox")
    assert post(c, f"/crm/{a}/move", col="bogus").status_code == 400


def test_note_type_next_step_and_validation(settings):
    a = add_event(settings, "Alice")
    c = make(settings)
    assert post(c, f"/crm/{a}/note", kind="recruiter", note="<b>hi</b>").status_code == 303
    assert post(c, f"/crm/{a}/note", kind="boss", note="x").status_code == 400
    assert post(c, f"/crm/{a}/note", kind="other", note="x" * 2001).status_code == 400
    assert post(c, f"/crm/{a}/next", step="call", due="2026-11-12").status_code == 303
    assert post(c, f"/crm/{a}/next", step="call", due="12/11").status_code == 400
    r = row(settings, a)
    assert (r["next_step"], r["due_at"]) == ("call", "2026-11-12")
    page = c.get(f"/crm/{a}").text
    assert "&lt;b&gt;hi&lt;/b&gt;" not in page or "<b>hi</b>" not in page  # escaped, never raw
    post(c, f"/crm/{a}/next", done="1")
    assert row(settings, a)["next_step"] is None


def test_every_post_needs_csrf(settings):
    a = add_event(settings, "Alice")
    c = make(settings)
    for path, data in [
        ("read", {}), ("move", {"col": "talk"}), ("archive", {}), ("note", {"kind": "other"}),
        ("next", {"step": "x"}), ("reply", {}), ("context", {"owner_context": "x"}),
    ]:  # fmt: skip
        assert c.post(f"/crm/{a}/{path}", data=data).status_code == 403, path
    assert row(settings, a)["col"] == "inbox" and row(settings, a)["unread"] == 1


def test_xss_and_only_https_linkedin_links(settings):
    a = add_event(
        settings,
        "<script>alert(1)</script>",
        headline="<img src=x onerror=alert(1)>",
        snippet="<b onmouseover=alert(2)>x</b>",
        profile_url="https://www.linkedin.com/in/x",
    )
    st = db(settings)  # bypass the writer: rows that slipped in before the CHECKs are still safe
    st.conn.execute("PRAGMA ignore_check_constraints=ON")
    st.conn.execute("UPDATE crm_events SET view_url='javascript:alert(1)'")
    st.conn.execute("UPDATE crm_contacts SET profile_url='http://evil.example.com/'")
    c = make(settings)
    for path in ("/crm", f"/crm/{a}"):
        t = c.get(path).text
        assert "<script>alert" not in t and "<img src=x" not in t and "<b onmouseover" not in t
        assert "javascript:" not in t and "evil.example.com" not in t
        assert "&lt;script&gt;" in t


def test_related_jobs_shown_by_company(settings, seeded):
    a = add_event(settings, "Alice", company="Acme")
    page = make(settings).get(f"/crm/{a}").text
    assert "Senior Data Engineer" in page and f"/jobs/{seeded.a_remoteok}" in page


def test_reply_button_queues_one_request_and_shows_the_draft(settings):
    a = add_event(settings, "Alice")
    c = make(settings)
    assert "Reply draft (Opus)" in c.get(f"/crm/{a}").text
    assert post(c, f"/crm/{a}/reply").status_code == 303
    post(c, f"/crm/{a}/reply")  # a second click does not queue a second one
    st = db(settings)
    assert st.conn.execute("SELECT COUNT(*) FROM crm_reply_requests").fetchone()[0] == 1
    page = c.get(f"/crm/{a}").text
    assert "queued for Opus" in page and "Reply draft (Opus)</button>" not in page
    st.conn.execute(
        "UPDATE crm_reply_requests SET status='done', draft='Hi <i>there</i>', finished_at=?",
                    (NOW.isoformat(),))  # fmt: skip
    page = c.get(f"/crm/{a}").text
    assert "Hi &lt;i&gt;there&lt;/i&gt;" in page and "<i>there" not in page
    assert "readonly" in page  # nothing to send: a text to copy


def test_stats_block(settings):
    add_event(settings, "Alice", kind="invitation_accepted", at=NOW - timedelta(days=1))
    page = make(settings).get("/crm").text
    assert "accepted the invitation, 7 d: 1" in page


def test_owner_context_saved_escaped_and_limited(settings):
    a = add_event(settings, "Alice")
    c = make(settings)
    assert (
        post(c, f"/crm/{a}/context", owner_context="<b>my InMail</b>\r\nline2").status_code == 303
    )
    assert row(settings, a)["owner_context"] == "<b>my InMail</b>\nline2"
    page = c.get(f"/crm/{a}").text
    assert "&lt;b&gt;my InMail&lt;/b&gt;" in page and "<b>my InMail" not in page
    assert post(c, f"/crm/{a}/context", owner_context="x" * 4001).status_code == 400
    assert post(c, f"/crm/{a}/context", owner_context="").status_code == 303
    assert row(settings, a)["owner_context"] is None
    assert post(c, "/crm/999/context", owner_context="x").status_code == 404


def test_two_blocks_with_copy_buttons_escaped_and_no_mailto(settings):
    a = add_event(settings, "Alice")
    c = make(settings)
    post(c, f"/crm/{a}/reply")
    st = db(settings)
    st.conn.execute(
        "UPDATE crm_reply_requests SET status='done', draft='LI <i>reply</i>', finished_at=?, "
        "email_to='hello@example.com', email_subject='Sub <b>', "
        "email_body='Body <script>x</script>'",
        (NOW.isoformat(),),
    )
    page = c.get(f"/crm/{a}").text
    assert "LI &lt;i&gt;reply&lt;/i&gt;" in page and "Body &lt;script&gt;" in page
    assert "<script>x" not in page and "mailto:" not in page
    assert page.count("data-copy=") == 4 and 'rows="20"' in page
    assert 'src="/static/crm.js"' in page
    assert "hello@example.com" in page
    assert c.get("/static/crm.js").status_code == 200
    st.conn.execute(
        "UPDATE crm_reply_requests SET email_to=NULL, email_subject=NULL, email_body=NULL"
    )
    page = c.get(f"/crm/{a}").text
    assert page.count("data-copy=") == 1 and "Email</h3>" not in page


def test_owner_context_limit_counts_after_crlf_normalisation(settings):
    a = add_event(settings, "Alice")
    c = make(settings)
    assert post(c, f"/crm/{a}/context", owner_context="x\r\n" * 2000).status_code == 303
    assert len(row(settings, a)["owner_context"]) == 3999
