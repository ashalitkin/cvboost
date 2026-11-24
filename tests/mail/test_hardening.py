"""SEC-04..06, QA fixes: sizes, contact masking, model output fields, placeholder company,
handler statuses, sources view."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from cvboost.collect import collect_source
from cvboost.connectors.email_alerts import MAX_BYTES, EmailAlertsConnector
from cvboost.core.dedupe import assign_canonical
from cvboost.llm.schema import EmailJob
from cvboost.mail.mask import mask_contacts, strip_unsafe_field
from cvboost.mail.parsers import registry
from cvboost.mail.parsers.base import HandlerResult
from cvboost.models import UNDISCLOSED_COMPANY
from tests.conftest import NOW
from tests.mail.helpers import CREDS, FakeMailbox, NoHttp, eml
from tests.rules_helpers import add, sync


def run_once(store, mb, when=NOW):
    conn = EmailAlertsConnector(CREDS, mailbox_factory=mb.factory)
    return collect_source(store, NoHttp().client, conn, limit=None, now=when)


def msgs(store):
    return store.conn.execute("SELECT * FROM email_messages ORDER BY id").fetchall()


def test_missing_size_counts_as_too_large_and_is_not_downloaded(store):
    mb = FakeMailbox({1: eml("linkedin_alert_5.eml")})
    mb.sizes = lambda uids: {}  # type: ignore[method-assign]  # the server did not answer
    run_once(store, mb)
    assert mb.fetched() == [] and [m["status"] for m in msgs(store)] == ["too_large"]


def test_body_longer_than_the_limit_is_dropped_even_if_the_size_lied(store):
    big = eml("linkedin_alert_5.eml") + b"x" * (MAX_BYTES + 1)
    mb = FakeMailbox({1: big})
    mb.size_override[1] = 100  # claims to be small
    res = run_once(store, mb)
    assert [m["status"] for m in msgs(store)] == ["too_large"] and res.found == 0


def test_mask_contacts_hides_addresses_and_phones_but_not_rates_or_dates():
    t = (
        "Call +351 912 345 678 or (212) 555-0123 or 212-555-0123, "
        "mail jane.doe+x@agency.example.org. "
        "Rate $70-85/hour, 120000-150000 EUR, started 2026-11-10, ref 12345."
    )
    out = mask_contacts(t)
    assert "[phone]" in out and out.count("[phone]") == 3 and "[email]" in out
    assert "agency.example.org" not in out and "912" not in out and "555" not in out
    assert "$70-85/hour" in out and "120000-150000" in out and "2026-11-10" in out


def test_recruiter_job_description_and_llm_input_contain_no_contacts(store):
    from tests.llm.helpers import FakeClient
    from tests.mail.test_email_extract import answer, run

    raw = eml("recruiter_unknown.eml").replace(
        b"Best,", b"Reach me: sam.recruiter@agency.example.org or +351 912 345 678.\n\nBest,"
    )
    run_once(store, FakeMailbox({1: raw}))
    (m,) = msgs(store)
    assert "@" not in m["llm_input"].replace("[email]", "") and "912" not in m["llm_input"]
    client = FakeClient(answer())
    run(store, client)
    assert "sam.recruiter" not in client.requests[0].stdin_text
    (j,) = store.conn.execute("SELECT description_text FROM jobs").fetchall()
    assert "sam.recruiter" not in j[0] and "912" not in j[0]


def test_model_text_fields_cannot_carry_urls_addresses_or_phones():
    job = EmailJob.model_validate(
        {
            "title": "Data Engineer https://evil.example/x",
            "company": "Acme jane@evil.example.net",
            "location": "Remote +351 912 345 678",
            "remote": True,
            "employment_type": "contract",
            "rate_text": "$80/h www.evil.example/pay",
            "link_ref": None,
        }
    )
    blob = f"{job.title}|{job.company}|{job.location}|{job.rate_text}"
    assert "http" not in blob and "@" not in blob and "912" not in blob and "evil" not in blob
    assert job.title == "Data Engineer" and job.company == "Acme" and job.rate_text == "$80/h"
    assert strip_unsafe_field("a   b") == "a b"


def test_redos_mask():
    for s in ("a" * 30000, "@" * 30000, "1 " * 15000, "+" + "1" * 30000, "(" * 30000, "a@" * 15000):
        t0 = time.perf_counter()
        mask_contacts(s)
        strip_unsafe_field(s)
        assert time.perf_counter() - t0 < 1.0


def test_undisclosed_client_jobs_with_the_same_title_stay_separate_groups(store):
    sync(store, "email:linkedin")
    a = add(
        store,
        "email:linkedin",
        "a",
        company=UNDISCLOSED_COMPANY,
        title="Data Engineer",
        description_text="first role text",
    )
    b = add(
        store,
        "email:linkedin",
        "b",
        company=UNDISCLOSED_COMPANY,
        title="Data Engineer",
        description_text="second role text",
    )
    assign_canonical(store.conn, 30)
    got = [
        store.conn.execute("SELECT canonical_id FROM jobs WHERE id=?", (i,)).fetchone()[0]
        for i in (a, b)
    ]
    assert got == [a, b]
    c = add(
        store, "email:linkedin", "c", company="Acme", title="Data Engineer", description_text="x1"
    )
    d = add(
        store, "email:linkedin", "d", company="Acme", title="Data Engineer", description_text="x2"
    )
    assign_canonical(store.conn, 30)
    assert store.conn.execute("SELECT canonical_id FROM jobs WHERE id=?", (d,)).fetchone()[0] == c


def test_handler_with_a_bad_status_is_recorded_as_crm_and_not_reread(store):
    calls = []

    def weird(parsed, st, now):
        calls.append(parsed.subject)
        return HandlerResult("weird", "x")

    registry.register_handler("hit-reply@linkedin.com", weird)
    try:
        mb = FakeMailbox({1: eml("linkedin_hit_reply_autoforwarded.eml")})
        run_once(store, mb)
        (m,) = msgs(store)
        assert (m["status"], m["note"]) == ("crm", "bad status")
        mb.log.clear()
        run_once(store, mb, NOW + timedelta(minutes=10))
        assert mb.fetched() == [] and len(calls) == 1
    finally:
        registry.unregister_handler(weird)


def test_untrusted_crm_mail_never_reaches_the_handler(store):
    calls = []

    def handler(parsed, st, now):
        calls.append(parsed)
        return HandlerResult("crm")

    raw = eml("linkedin_hit_reply_autoforwarded.eml").replace(
        b"dmarc=pass header.from=linkedin.com", b"dmarc=fail header.from=linkedin.com"
    )
    raw = raw.replace(b"dkim=pass", b"dkim=fail")
    registry.register_handler("hit-reply@linkedin.com", handler)
    try:
        run_once(store, FakeMailbox({1: raw}))
    finally:
        registry.unregister_handler(handler)
    assert calls == [] and [m["status"] for m in msgs(store)] == ["crm_untrusted"]


def test_trust_level_reaches_the_handler(store):
    seen = []

    def handler(parsed, st, now):
        seen.append(parsed.trust)
        return HandlerResult("crm")

    registry.register_handler("hit-reply@linkedin.com", handler)
    try:
        mb = FakeMailbox(
            {1: eml("linkedin_hit_reply_autoforwarded.eml"), 2: eml("forward_inline_crm.eml")}
        )
        conn = EmailAlertsConnector(
            CREDS, forwarders=["owner@example.com"], mailbox_factory=mb.factory
        )
        collect_source(store, NoHttp().client, conn, limit=None, now=NOW)
    finally:
        registry.unregister_handler(handler)
    assert seen == ["verified", "owner_forwarded"]


def test_sources_view_unconfigured_and_sub_source_jobs_sum_into_the_parent(store):
    from cvboost.sources_view import source_rows

    mb = FakeMailbox({1: eml("linkedin_alert_5.eml")})
    run_once(store, mb)
    rows = {r.name: r for r in source_rows(store, NOW, frozenset({"email_alerts"}))}
    assert rows["email_alerts"].last_run == "not configured" and rows["email_alerts"].jobs == 5
    assert not any(n.startswith("email:") for n in rows)
    rows = {r.name: r for r in source_rows(store, NOW)}
    assert rows["email_alerts"].last_run != "not configured"


@pytest.fixture(autouse=True)
def _clean_handlers():
    yield
