"""#33 wired to #17: the real dispatcher, a fake mailbox, one connector run."""

from __future__ import annotations

from dataclasses import replace

import pytest

from cvboost import connectors
from cvboost.collect import collect_source
from cvboost.connectors.email_alerts import EmailAlertsConnector
from cvboost.crm.handler import LINKEDIN_SENDERS, handle_email
from cvboost.mail.message import parse_messages
from cvboost.mail.parsers import registry
from cvboost.storage.db import Store, connect, migrate
from tests.conftest import NOW
from tests.mail.helpers import CREDS, FakeMailbox, NoHttp, eml, with_auth

OWNER = ["owner@example.com"]
VERIFIED = "mx.google.com; dkim=pass header.i=@linkedin.com; dmarc=pass header.from=linkedin.com"


@pytest.fixture
def store(tmp_path):
    conn = connect(tmp_path / "w.sqlite")
    migrate(conn)
    st = Store(conn)
    st.sync_sources(connectors.REGISTRY, NOW)
    return st


def run(store, mb, **kw):
    conn = EmailAlertsConnector(CREDS, mailbox_factory=mb.factory, **kw)
    return collect_source(store, NoHttp().client, conn, limit=None, now=NOW)


def contacts(store):
    return store.conn.execute("SELECT * FROM crm_contacts ORDER BY id").fetchall()


def statuses(store):
    return [(m["status"], m["note"]) for m in store.conn.execute("SELECT * FROM email_messages")]


def test_registry_routes_linkedin_senders_to_the_crm_handler():
    for addr in LINKEDIN_SENDERS:
        first = next(h for m, h in registry._handlers if registry._matches(m, addr))
        assert first is handle_email, addr


def test_verified_hit_reply_mail_creates_a_contact_in_one_run(store):
    mb = FakeMailbox({1: with_auth(eml("linkedin_crm_message.eml"), VERIFIED)})
    res = run(store, mb)
    assert res.status == "ok" and res.new == 0  # not a job
    (c,) = contacts(store)
    assert (c["name"], c["col"], c["unread"]) == ("Tom Sample", "inbox", 1)
    assert c["company"] == "Sample Analytics Ltd"
    (e,) = store.conn.execute("SELECT * FROM crm_events").fetchall()
    assert e["kind"] == "message" and e["view_url"].startswith("https://www.linkedin.com/")
    assert statuses(store) == [("crm", "1 new / 1 event(s)")]


def test_inmail_hit_reply_and_digest_senders(store):
    raw = with_auth(eml("linkedin_crm_digest.eml"), VERIFIED)
    run(store, FakeMailbox({1: raw}))
    assert len(contacts(store)) == 3


def test_untrusted_mail_creates_nothing(store):
    run(store, FakeMailbox({1: eml("linkedin_crm_message.eml")}))  # no Authentication-Results
    assert contacts(store) == []
    assert [s[0] for s in statuses(store)] == ["crm_untrusted"]


def test_owner_forwarded_mail_is_taken(store):
    run(store, FakeMailbox({1: eml("forward_inline_crm.eml")}), forwarders=OWNER)
    (c,) = contacts(store)
    assert c["name"] == "Jane Recruiter"
    assert statuses(store)[0][0] == "crm"


def test_handler_refuses_untrusted_on_its_own(store):
    (p,) = parse_messages(eml("linkedin_crm_message.eml"))
    assert p.trust == "untrusted"
    r = handle_email(p, store, NOW)
    assert r.status == "crm_untrusted" and contacts(store) == []
    r = handle_email(replace(p, trust="verified"), store, NOW)
    assert r.status == "crm" and len(contacts(store)) == 1


def test_handler_is_idempotent_with_and_without_ids_and_dates(store):
    (p,) = parse_messages(with_auth(eml("linkedin_crm_message.eml"), VERIFIED))
    for variant in (p, replace(p, message_id=None), replace(p, message_id=None, received_at=None)):
        for _ in range(2):
            handle_email(variant, store, NOW)
    n = store.conn.execute("SELECT COUNT(*) FROM crm_events").fetchone()[0]
    assert n == 3  # one per distinct identity (id / no id / no id and no date), never doubled
    handle_email(replace(p, message_id=None, received_at=None), store, NOW)
    assert store.conn.execute("SELECT COUNT(*) FROM crm_events").fetchone()[0] == 3
    assert len(contacts(store)) == 1


def test_second_run_over_the_same_mailbox_adds_nothing(store):
    mb = FakeMailbox({1: with_auth(eml("linkedin_crm_message.eml"), VERIFIED)})
    run(store, mb)
    run(store, mb)
    assert store.conn.execute("SELECT COUNT(*) FROM crm_events").fetchone()[0] == 1
