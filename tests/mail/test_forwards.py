"""Forwards: unwrapped only for the owner's own authenticated addresses."""

from __future__ import annotations

import email
from datetime import UTC, datetime
from email import policy

import pytest

from cvboost.mail.message import ParsedEmail, parse_messages
from cvboost.mail.parsers.registry import classify
from tests.conftest import NOW
from tests.mail.helpers import eml, with_auth

OWNER = ["owner@example.com"]


def test_no_forwarders_configured_means_no_unwrapping():
    for name in ("forward_inline_alert.eml", "forward_attachments.eml", "forward_inline_crm.eml"):
        (p,) = parse_messages(eml(name))
        assert p.from_addr == "owner@example.com" and not p.forwarded


def test_inline_forward_of_a_job_alert_is_routed_by_the_original_sender():
    (p,) = parse_messages(eml("forward_inline_alert.eml"), forwarders=OWNER)
    assert p.forwarded and p.from_addr == "jobalerts-noreply@linkedin.com"
    assert p.outer_from == "owner@example.com" and p.trust == "owner_forwarded"
    assert p.received_at == datetime(2026, 11, 10, 4, 16, tzinfo=UTC)  # the original Date
    assert p.subject.startswith("Owner: your job alert")  # original subject, not "Fwd:"
    c = classify(p, None, NOW)  # type: ignore[arg-type]
    assert (c.status, len(c.jobs)) == ("parsed", 5)


def test_inline_forward_links_come_from_the_forwarded_body_only():
    (p,) = parse_messages(eml("forward_inline_crm.eml"), forwarders=OWNER)
    assert p.from_addr == "hit-reply@linkedin.com" and p.from_name == "Jane Recruiter"
    assert p.received_at == datetime(2026, 11, 10, 14, 53, tzinfo=UTC)
    assert p.subject == "Message accepted by Jane Recruiter"
    assert "accepted your inmail message" in p.text and "Forwarded message" not in p.text
    assert "https://www.example.com/" not in p.links  # the forwarder's signature link
    assert any("linkedin.com/comm/feed" in link for link in p.links)


def test_forward_as_attachment_gives_one_parsed_email_per_attached_message():
    got = parse_messages(eml("forward_attachments.eml"), forwarders=OWNER)
    assert [p.from_addr for p in got] == [
        "jobalerts-noreply@linkedin.com",
        "hit-reply@linkedin.com",
    ]
    assert all(p.forwarded and p.outer_from == "owner@example.com" for p in got)
    assert got[1].message_id == "crm-accepted-1@mail.linkedin.example.com"
    assert [classify(p, None, NOW).status for p in got] == ["parsed", "crm"]  # type: ignore[arg-type]


def test_forward_from_a_stranger_is_not_unwrapped_and_not_trusted():
    (p,) = parse_messages(eml("forward_from_stranger.eml"), forwarders=OWNER)
    assert p.from_addr == "mallory@example.net" and not p.forwarded
    assert classify(p, None, NOW).status == "needs_llm"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "auth",
    [
        None,  # SEC-02: no header from our server
        "mx.google.com; spf=fail; dmarc=fail header.from=example.com",
        "mx.google.com; dkim=pass header.i=@example.com; spf=pass",  # no dmarc pass
        "mx.google.com; dmarc=pass header.from=other.example",
        "mx.google.com; dmarc=pass header.from=notexample.com",  # no substring match
        "mx.google.com; dmarc=pass header.from=example.com.evil.io",
        "mx.google.com; dmarc=pass header.from=mail.example.com",  # exactly the domain
        "mx.evil.example; dmarc=pass header.from=example.com",  # not our server
    ],
)
def test_forwarder_without_exact_dmarc_pass_is_not_unwrapped(auth):
    (p,) = parse_messages(with_auth(eml("forward_inline_alert.eml"), auth), forwarders=OWNER)
    assert p.from_addr == "owner@example.com" and not p.forwarded
    assert p.trust != "owner_forwarded"
    assert not classify(p, None, NOW).jobs  # type: ignore[arg-type]


def test_unwrapped_mail_has_owner_forwarded_trust_never_verified():
    for name in (
        "forward_inline_alert.eml",
        "forward_attachments.eml",
        "forward_inline_alert_CRLF.eml",
    ):
        got = parse_messages(eml(name), forwarders=OWNER)
        assert got and all(p.trust == "owner_forwarded" and not p.verified for p in got)


def test_crlf_inline_forwards_are_unwrapped():
    raw = eml("forward_inline_crm_CRLF.eml")
    body = email.message_from_bytes(raw, policy=policy.default).get_body(("plain",)).get_content()
    assert "\r" in body  # the fixture really has CR in the decoded text
    (p,) = parse_messages(raw, forwarders=OWNER)
    assert p.forwarded and p.from_addr == "hit-reply@linkedin.com" and "\r" not in p.text
    assert p.received_at == datetime(2026, 11, 10, 14, 53, tzinfo=UTC)
    (a,) = parse_messages(eml("forward_inline_alert_CRLF.eml"), forwarders=OWNER)
    assert classify(a, None, NOW).status == "parsed"  # type: ignore[arg-type]


def test_real_style_google_header_with_comments_unwraps():
    auth = (
        "mx.google.com; dkim=pass header.i=@example.com header.s=google header.b=abc; "
        "arc=pass (i=1); spf=neutral (google.com: 209.85.220.41 is neither permitted nor denied "
        "by domain of owner@example.com) smtp.mailfrom=owner@example.com; "
        "dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=example.com; dara=pass"
    )
    (p,) = parse_messages(with_auth(eml("forward_inline_crm.eml"), auth), forwarders=OWNER)
    assert p.forwarded


def test_auto_forwarded_mail_keeps_original_from_and_needs_no_unwrapping():
    raw = eml("linkedin_alert_5.eml")
    assert b"X-Forwarded-For" in raw
    (p,) = parse_messages(raw, forwarders=OWNER)
    assert p.from_addr == "jobalerts-noreply@linkedin.com" and not p.forwarded and p.verified
    (crm,) = parse_messages(eml("linkedin_hit_reply_autoforwarded.eml"))
    assert crm.from_addr == "hit-reply@linkedin.com" and crm.verified
    assert classify(crm, None, NOW).status == "crm"  # type: ignore[arg-type]


def test_parsed_email_contract_is_constructible_from_the_seven_public_fields():
    p = ParsedEmail("a@example.com", "s", None, None, "t", "", [])
    assert (p.forwarded, p.outer_from, p.dkim_domains, p.trust, p.verified) == (
        False,
        None,
        (),
        "untrusted",
        False,
    )
