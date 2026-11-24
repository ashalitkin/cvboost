from __future__ import annotations

from datetime import timedelta

import pytest

from cvboost.crm import store as S
from cvboost.crm.types import ParsedEvent
from cvboost.storage.db import Store, connect, migrate
from tests.conftest import NOW

T0 = NOW - timedelta(days=10)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "c.sqlite")
    migrate(c)
    return c


def ev(name="Jane Example", kind="message", snippet="hello", **kw):
    return ParsedEvent(kind=kind, name=name, snippet=snippet, **kw)


def rec(conn, e, at, mid=None):
    return S.record_event(conn, e, at, mid, NOW)


def one(conn, name="Jane Example"):
    return conn.execute("SELECT * FROM crm_contacts WHERE name = ?", (name,)).fetchone()


def test_new_contact_is_unread_in_inbox(conn):
    assert rec(conn, ev(company="Acme"), T0, "m1")
    c = one(conn)
    assert (c["col"], c["unread"], c["archived"], c["company"]) == ("inbox", 1, 0, "Acme")


def test_new_event_marks_unread_moves_to_top_keeps_column(conn):
    rec(conn, ev("A"), T0, "a1")
    rec(conn, ev("B"), T0 + timedelta(days=1), "b1")
    S.move(conn, one(conn, "A")["id"], "leads", NOW)
    S.mark_read(conn, one(conn, "A")["id"], NOW)
    assert [c["name"] for c in S.columns(conn)["inbox"]] == ["B"]
    rec(conn, ev("A", snippet="again"), T0 + timedelta(days=2), "a2")
    a = one(conn, "A")
    assert (a["unread"], a["col"]) == (1, "leads")
    assert [c["name"] for c in S.columns(conn)["leads"]] == ["A"]
    rec(conn, ev("B", snippet="again"), T0 + timedelta(days=3), "b2")
    rec(conn, ev("C"), T0 + timedelta(days=4), "c1")
    assert [c["name"] for c in S.columns(conn)["inbox"]] == ["C", "B"]


def test_event_pulls_contact_out_of_archive_into_inbox(conn):
    rec(conn, ev(), T0, "m1")
    cid = one(conn)["id"]
    S.move(conn, cid, "talk", NOW)
    S.set_archived(conn, cid, True, NOW)
    assert S.columns(conn)["talk"] == [] and len(S.archived(conn)) == 1
    rec(conn, ev(snippet="back"), T0 + timedelta(days=1), "m2")
    c = one(conn)
    assert (c["archived"], c["col"], c["unread"]) == (0, "inbox", 1)


def test_mark_read_and_open_clear_unread(conn):
    rec(conn, ev(), T0, "m1")
    cid = one(conn)["id"]
    S.mark_read(conn, cid, NOW)
    assert one(conn)["unread"] == 0
    S.mark_unread(conn, cid, NOW)
    assert one(conn)["unread"] == 1


def test_old_forwarded_event_does_not_unread_or_move(conn):
    rec(conn, ev(headline="New headline"), T0 + timedelta(days=5), "new")
    cid = one(conn)["id"]
    S.mark_read(conn, cid, NOW)
    S.move(conn, cid, "talk", NOW)
    assert rec(conn, ev(snippet="old one", headline="Old headline", company="OldCo"), T0, "old")
    c = one(conn)
    assert c["unread"] == 0 and c["col"] == "talk"
    assert c["last_event_at"] == (T0 + timedelta(days=5)).isoformat()
    assert c["headline"] == "New headline" and c["company"] == "OldCo"  # only fills gaps
    assert [e["snippet"] for e in S.events(conn, cid)] == ["hello", "old one"]  # by own date


def test_old_event_does_not_pull_archived_contact_back(conn):
    rec(conn, ev(), T0 + timedelta(days=5), "new")
    cid = one(conn)["id"]
    S.set_archived(conn, cid, True, NOW)
    rec(conn, ev(snippet="old"), T0, "old")
    assert one(conn)["archived"] == 1


def test_out_of_order_ordering_uses_event_date_not_insert_time(conn):
    rec(conn, ev("A"), T0 + timedelta(days=3), "a")
    rec(conn, ev("B"), T0, "b")  # inserted later, but older
    assert [c["name"] for c in S.columns(conn)["inbox"]] == ["A", "B"]


def test_dedupe_by_message_id_and_by_content(conn):
    assert rec(conn, ev(), T0, "m1")
    assert not rec(conn, ev(), T0, "m1")  # same mail
    assert rec(conn, ev(snippet="x"), T0, None)
    assert not rec(conn, ev(snippet="x"), T0, None)  # same kind, time, snippet
    assert rec(conn, ev(snippet="y"), T0, None)
    assert conn.execute("SELECT COUNT(*) FROM crm_events").fetchone()[0] == 3
    # a duplicate must not touch the contact
    S.mark_read(conn, one(conn)["id"], NOW)
    rec(conn, ev(), T0 + timedelta(days=1), "m1")
    assert one(conn)["unread"] == 0


def test_digest_events_of_one_mail_are_distinct(conn):
    assert rec(conn, ev("A", index=0), T0, "d1")
    assert rec(conn, ev("B", index=1), T0, "d1")
    assert not rec(conn, ev("A", index=0), T0, "d1")


def test_unknown_sender_gets_the_placeholder_contact(conn):
    rec(conn, ParsedEvent(kind="other", name="", snippet="weekly"), T0, "o1")
    assert one(conn, S.UNKNOWN_NAME) is not None


def test_bad_links_are_not_stored(conn):
    rec(conn, ev(profile_url="https://evil.example.com/in/x", view_url="javascript:1"), T0, "m")
    assert one(conn)["profile_url"] is None
    assert conn.execute("SELECT view_url FROM crm_events").fetchone()[0] is None


def test_note_next_step_and_due(conn):
    rec(conn, ev(), T0, "m")
    cid = one(conn)["id"]
    assert S.set_note(conn, cid, "recruiter", "n" * 3000, NOW)
    c = one(conn)
    assert c["kind"] == "recruiter" and len(c["note"]) == 2000
    assert not S.set_note(conn, cid, "boss", "x", NOW)
    assert S.set_next(conn, cid, "написать", "2026-11-12", NOW)
    assert (one(conn)["next_step"], one(conn)["due_at"]) == ("написать", "2026-11-12")
    assert not S.set_next(conn, cid, "x", "12.11.2026", NOW)
    assert not S.set_next(conn, cid, "x", "2026-13-40", NOW)
    assert S.set_next(conn, cid, "", "", NOW)
    assert one(conn)["next_step"] is None and one(conn)["due_at"] is None


def test_related_jobs_by_company(conn):
    from tests.rules_helpers import add, sync

    st = Store(conn)
    sync(st, "remotive")
    j = add(st, "remotive", "1", company="Acme Inc.", title="Data Engineer")
    add(st, "remotive", "2", company="Acme Industries", title="Other")
    assert [x["id"] for x in S.related_jobs(conn, "ACME")] == [j]
    assert S.related_jobs(conn, None) == [] and S.related_jobs(conn, "Ac") == []


def test_stats(conn):
    rec(conn, ev("A", kind="invitation_accepted"), NOW - timedelta(days=3), "a1")
    rec(conn, ev("A", kind="message_accepted"), NOW - timedelta(days=2), "a2")
    rec(conn, ev("B", kind="invitation_accepted"), NOW - timedelta(days=20), "b1")
    rec(conn, ev("C", kind="invitation_accepted"), NOW - timedelta(days=1), "c1")
    S.move(conn, one(conn, "A")["id"], "talk", NOW)
    s = S.stats(conn, NOW)
    assert s["windows"][7] == {"accepted": 2, "replied": 1}
    assert s["windows"][30] == {"accepted": 3, "replied": 1}
    assert s["columns"] == {"inbox": 2, "leads": 0, "talk": 1} and s["talk"] == 1


# --- identity: profile slug first, name as the fallback ---

JANE = "https://www.linkedin.com/in/jane-example-1a2b3c"


def count(conn):
    return conn.execute("SELECT COUNT(*) FROM crm_contacts").fetchone()[0]


def test_slug_is_the_identity_when_the_name_agrees(conn):
    rec(conn, ev("Jane Example", profile_url=JANE), T0, "a")
    rec(conn, ev("jane  EXAMPLE", profile_url=JANE + "?trk=x"), T0 + timedelta(days=1), "b")
    assert count(conn) == 1
    assert (
        conn.execute("SELECT profile_slug FROM crm_contacts").fetchone()[0] == "jane-example-1a2b3c"
    )


def test_slug_with_another_name_never_merges_or_overwrites(conn):
    rec(conn, ev("Jane Example", headline="Recruiter", company="RealCo", profile_url=JANE), T0, "a")
    cid = one(conn)["id"]
    S.mark_read(conn, cid, NOW)
    S.set_archived(conn, cid, True, NOW)
    rec(conn, ev("Mallory", headline="Evil", company="EvilCo", profile_url=JANE),
        T0 + timedelta(days=1), "b")  # fmt: skip
    j = one(conn)
    assert (j["headline"], j["company"], j["archived"], j["unread"]) == (
        "Recruiter",
        "RealCo",
        1,
        0,
    )
    m = one(conn, "Mallory")
    assert m["profile_slug"] is None and m["profile_url"] is None and m["id"] != cid
    assert len(S.events(conn, cid)) == 1


def test_two_slugs_with_one_name_are_two_contacts(conn):
    rec(conn, ev("John Smith", profile_url="https://www.linkedin.com/in/john-smith-1"), T0, "a")
    rec(conn, ev("John Smith", profile_url="https://www.linkedin.com/in/john-smith-2"), T0, "b")
    assert count(conn) == 2
    rec(
        conn, ev("John Smith", profile_url="https://www.linkedin.com/comm/in/john-smith-1"), T0, "c"
    )
    assert count(conn) == 2  # /comm/in/ is the same person


def test_later_mail_with_slug_attaches_to_the_name_only_contact(conn):
    rec(conn, ev("Jane Example"), T0, "a")  # no profile link in the first mail
    rec(conn, ev("Jane Example", profile_url=JANE), T0 + timedelta(days=1), "b")
    assert count(conn) == 1
    c = one(conn)
    assert c["profile_slug"] == "jane-example-1a2b3c" and c["profile_url"] == JANE
    assert len(S.events(conn, c["id"])) == 2


def test_slug_attach_also_works_for_an_older_event(conn):
    rec(conn, ev("Jane Example"), T0 + timedelta(days=3), "new")
    rec(conn, ev("Jane Example", profile_url=JANE), T0, "old")
    assert count(conn) == 1 and one(conn)["profile_slug"] == "jane-example-1a2b3c"


def test_name_only_mail_matches_only_contacts_without_a_slug(conn):
    rec(conn, ev("John Smith", profile_url="https://www.linkedin.com/in/john-smith-1"), T0, "a")
    rec(conn, ev("John Smith", snippet="no link"), T0 + timedelta(days=1), "b")
    assert count(conn) == 2  # the slug contact is not touched by a bare name
    rec(conn, ev("John Smith", snippet="again"), T0 + timedelta(days=2), "c")
    assert count(conn) == 2  # the second bare-name mail joins the first slug-less contact
    slugged = conn.execute("SELECT * FROM crm_contacts WHERE profile_slug IS NOT NULL").fetchone()
    assert len(S.events(conn, slugged["id"])) == 1 and slugged["unread"] == 1


def test_a_slug_never_moves_to_another_contact(conn):
    rec(conn, ev("Jane Example", profile_url=JANE), T0, "a")
    rec(conn, ev("Jane Other"), T0, "b")
    rec(conn, ev("Jane Other", profile_url=JANE), T0 + timedelta(days=1), "c")
    assert count(conn) == 2  # the slug belongs to Jane Example; the event goes there
    assert conn.execute("SELECT COUNT(*) FROM crm_events").fetchone()[0] == 3
