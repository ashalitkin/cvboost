from __future__ import annotations

from datetime import timedelta

from cvboost.collect import collect, collect_source
from cvboost.connectors.email_alerts import EmailAlertsConnector
from cvboost.mail.parsers import registry
from cvboost.mail.parsers.base import HandlerResult
from tests.conftest import NOW
from tests.mail.helpers import CREDS, FakeMailbox, NoHttp, eml


def make(mb: FakeMailbox, **kw) -> EmailAlertsConnector:
    return EmailAlertsConnector(CREDS, mailbox_factory=mb.factory, **kw)


def run_once(store, conn: EmailAlertsConnector, http: NoHttp, when=NOW, limit=None):
    return collect_source(store, http.client, conn, limit=limit, now=when)


def jobs(store):
    return store.conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()


def msgs(store):
    return store.conn.execute("SELECT * FROM email_messages ORDER BY id").fetchall()


def test_linkedin_mail_makes_5_jobs_and_no_link_is_opened(store):
    mb, http = FakeMailbox({1: eml("linkedin_alert_5.eml")}), NoHttp()
    res = run_once(store, make(mb), http)
    assert (res.status, res.found, res.new) == ("ok", 5, 5)
    rows = jobs(store)
    assert {r["source"] for r in rows} == {"email:linkedin"}
    assert all(r["url"] == f"https://www.linkedin.com/jobs/view/{r['source_id']}/" for r in rows)
    blob = " ".join(f"{r['url']} {r['title']} {r['raw_json']}" for r in rows)
    assert "trackingId" not in blob and "unsubscribe" not in blob.lower()
    assert http.requests == []
    (m,) = msgs(store)
    assert (m["status"], m["jobs_found"], m["sender_domain"], m["uid"]) == (
        "parsed",
        5,
        "linkedin.com",
        1,
    )
    assert m["run_id"] is not None and m["llm_input"] is None


def test_all_platform_samples_in_one_run(store):
    mb = FakeMailbox(
        {
            1: eml("linkedin_alert_6.eml"),
            2: eml("dice_alert_5.eml"),
            3: eml("indeed_alert_10.eml"),
            4: eml("indeed_login_code.eml"),
            5: eml("freelancermap_activate.eml"),
            6: eml("recruiter_unknown.eml"),
        }
    )
    res = run_once(store, make(mb), NoHttp())
    assert (res.found, res.new) == (21, 21)
    by_status = {m["uid"]: m["status"] for m in msgs(store)}
    assert by_status == {
        1: "parsed",
        2: "parsed",
        3: "parsed",
        4: "ignored",
        5: "ignored",
        6: "needs_llm",
    }
    pending = [m for m in msgs(store) if m["status"] == "needs_llm"]
    assert pending[0]["llm_input"] and pending[0]["platform"] == "recruiter"


def test_second_run_does_not_reread_old_messages(store):
    mb, http = FakeMailbox({1: eml("linkedin_alert_5.eml"), 2: eml("dice_alert_5.eml")}), NoHttp()
    conn = make(mb)
    run_once(store, conn, http)
    assert mb.fetched() == [1, 2]
    mb.log.clear()
    res = run_once(store, conn, http, when=NOW + timedelta(minutes=10))
    assert (res.found, res.new) == (0, 0) and mb.fetched() == []  # the n:* quirk returned UID 2
    mb.add(3, eml("indeed_alert_10.eml"))
    mb.log.clear()
    res = run_once(store, conn, http, when=NOW + timedelta(minutes=20))
    assert mb.fetched() == [3] and res.new == 10


def test_uidvalidity_change_rereads_without_duplicates(store):
    mb, http = FakeMailbox({1: eml("linkedin_alert_5.eml")}), NoHttp()
    conn = make(mb)
    run_once(store, conn, http)
    mb.uidvalidity = 2
    res = run_once(store, conn, http, when=NOW + timedelta(minutes=10))
    assert res.new == 0 and len(jobs(store)) == 5
    assert [m["status"] for m in msgs(store)] == ["parsed", "duplicate"]


def test_more_than_50_messages_are_taken_50_per_run(store):
    mb = FakeMailbox(
        {
            u: eml("recruiter_unknown.eml").replace(b"recruiter-1@", b"r%d@" % u)
            for u in range(1, 121)
        }
    )
    conn = make(mb)
    run_once(store, conn, NoHttp())
    assert len(mb.fetched()) == 50
    mb.log.clear()
    run_once(store, conn, NoHttp(), when=NOW + timedelta(minutes=10))
    assert mb.fetched() == list(range(51, 101))


def test_oversized_message_is_not_downloaded(store):
    mb = FakeMailbox({1: eml("linkedin_alert_5.eml"), 2: eml("dice_alert_5.eml")})
    mb.size_override[1] = 3 * 1024 * 1024
    run_once(store, make(mb), NoHttp())
    assert mb.fetched() == [2]
    assert {m["uid"]: m["status"] for m in msgs(store)} == {1: "too_large", 2: "parsed"}


def test_broken_message_is_a_failure_and_the_others_are_processed(store, monkeypatch):
    mb = FakeMailbox({1: eml("linkedin_alert_5.eml"), 2: b"", 3: eml("dice_alert_5.eml")})
    from cvboost.connectors import email_alerts

    real = email_alerts.parse_messages

    def flaky(data, **kw):
        if not data:
            raise ValueError("cannot parse")
        return real(data, **kw)

    monkeypatch.setattr(email_alerts, "parse_messages", flaky)
    res = run_once(store, make(mb), NoHttp())
    assert (res.status, res.new) == ("ok", 10)
    last = store.conn.execute("SELECT status, error_type FROM source_runs").fetchone()
    assert (last["status"], last["error_type"]) == ("ok", "partial")
    assert {m["uid"]: m["status"] for m in msgs(store)}[2] == "parse_error"


def test_same_job_in_two_alerts_is_one_job_with_two_sightings(store):
    first = eml("linkedin_alert_5.eml")
    second = first.replace(b"Message-ID: <", b"Message-ID: <again-", 1)
    mb = FakeMailbox({1: first, 2: second})
    res = run_once(store, make(mb), NoHttp())
    assert res.new == 5 and len(jobs(store)) == 5
    sightings = store.conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
    assert sightings == 5  # same run: one sighting per job; the second mail adds no job


def test_inner_message_id_dedupes_direct_and_forwarded_copies(store):
    direct = eml("linkedin_alert_5.eml")
    mb = FakeMailbox({1: direct, 2: eml("forward_attachments.eml")})
    conn = EmailAlertsConnector(CREDS, forwarders=["owner@example.com"], mailbox_factory=mb.factory)
    run_once(store, conn, NoHttp())
    statuses = [(m["uid"], m["part"], m["status"]) for m in msgs(store)]
    assert statuses == [(1, 0, "parsed"), (2, 0, "duplicate"), (2, 1, "crm")]
    assert len(jobs(store)) == 5


def test_forward_attachment_adds_old_mail_with_original_date(store):
    mb = FakeMailbox({1: eml("forward_attachments.eml")})
    conn = EmailAlertsConnector(CREDS, forwarders=["owner@example.com"], mailbox_factory=mb.factory)
    run_once(store, conn, NoHttp())
    assert len(jobs(store)) == 5
    assert jobs(store)[0]["posted_at"].startswith("2026-11-10T04:16:56")


def test_crm_handler_receives_the_unwrapped_message_and_the_status_is_crm(store):
    seen = []

    def handler(parsed, st, now):
        seen.append((parsed.from_addr, parsed.forwarded, parsed.subject, parsed.received_at))
        return HandlerResult("crm", "accepted")

    registry.register_handler("hit-reply@linkedin.com", handler)
    try:
        mb = FakeMailbox(
            {1: eml("forward_inline_crm.eml"), 2: eml("linkedin_hit_reply_autoforwarded.eml")}
        )
        conn = EmailAlertsConnector(
            CREDS, forwarders=["owner@example.com"], mailbox_factory=mb.factory
        )
        run_once(store, conn, NoHttp())
    finally:
        registry.unregister_handler(handler)
    assert [s[:3] for s in seen] == [
        ("hit-reply@linkedin.com", True, "Message accepted by Jane Recruiter"),
        ("hit-reply@linkedin.com", False, "Message accepted by Jane Recruiter"),
    ]
    assert [m["status"] for m in msgs(store)] == ["crm", "crm"] and not jobs(store)
    assert msgs(store)[0]["note"] == "accepted"


def test_handler_that_raises_does_not_stop_the_run(store):
    def boom(parsed, st, now):
        raise RuntimeError("secret detail")

    registry.register_handler("@linkedin.com", boom)
    try:
        mb = FakeMailbox({1: eml("linkedin_hit_reply_autoforwarded.eml")})
        run_once(store, make(mb), NoHttp())
    finally:
        registry.unregister_handler(boom)
    (m,) = msgs(store)
    assert m["status"] == "crm" and m["note"] == "handler error: RuntimeError"


def test_not_configured_is_skipped_without_run_row(store):
    conn = EmailAlertsConnector(None)
    assert not conn.configured()
    http = NoHttp()
    (res,) = collect(store, http.client, [conn], limit=None, force=False, now=NOW)
    assert (res.status, res.skip_reason) == ("skipped", "not_configured")
    assert store.conn.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0] == 0


def test_connection_error_is_a_failed_run_with_backoff(store):
    from cvboost.mail.imap import MailError

    class Dead(FakeMailbox):
        def open(self):
            raise MailError("IMAP4.error: login failed")

    res = run_once(store, EmailAlertsConnector(CREDS, mailbox_factory=Dead().factory), NoHttp())
    assert res.status == "error" and "login failed" in (res.error_message or "")
    assert store.source_state("email_alerts").consecutive_failures == 1


def test_registered_sources_exist_for_the_jobs_foreign_key(store):
    names = {r[0] for r in store.conn.execute("SELECT name FROM sources")}
    assert {
        "email_alerts",
        "email:linkedin",
        "email:indeed",
        "email:dice",
        "email:recruiter",
    } <= names


def test_worker_cycle_without_imap_env_is_ok_and_makes_no_run_row(store, tmp_path):
    from cvboost.worker import run_cycle
    from tests.worker.helpers import FakeClock, ScoreRecorder, make_deps

    deps = make_deps(
        tmp_path, [EmailAlertsConnector(None)], FakeClock(NOW), score_fn=ScoreRecorder()
    )
    stats = run_cycle(store, deps)
    assert stats.status == "ok" and not stats.errors
    assert store.conn.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0] == 0
