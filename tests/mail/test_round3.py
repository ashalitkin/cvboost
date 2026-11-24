"""SEC-07 (Authentication-Results lexing), SEC-08, SEC-09 and the QA lows of round 3."""

from __future__ import annotations

import email
import logging
from email import policy
from email.message import EmailMessage

import pytest

from cvboost.core.normalize import company_norm, has_key
from cvboost.mail.message import ParsedEmail, parse_auth_results, parse_messages
from cvboost.mail.parsers import registry
from cvboost.mail.parsers.registry import classify, prepare_llm_input
from tests.conftest import NOW
from tests.mail.helpers import eml, with_auth

OWNER = ["owner@example.com"]
QUOTED = '"a;dmarc=pass header.from={dom}"@evil.example.net'


def auth_of(header: str):
    m = email.message_from_bytes(
        b"Authentication-Results: " + header.encode() + b"\n\nbody", policy=policy.default
    )
    return parse_auth_results(m)


@pytest.mark.parametrize(
    "header",
    [
        # Gmail copies the envelope sender into smtp.mailfrom; a quoted local part with ';'
        "mx.google.com; spf=pass smtp.mailfrom="
        + QUOTED.format(dom="linkedin.com")
        + "; dmarc=fail header.from=linkedin.com",
        "mx.google.com; spf=pass smtp.mailfrom=" + QUOTED.format(dom="linkedin.com"),
        # the forged clause comes first, the real (failing) one after it
        "mx.google.com; spf=pass smtp.mailfrom="
        + QUOTED.format(dom="linkedin.com")
        + "; dmarc=fail header.from=evil.example",
        # a comment hides the injection the same way
        "mx.google.com; spf=pass (x; dmarc=pass header.from=linkedin.com) "
        "smtp.mailfrom=a@evil.example.net",
        # two dmarc clauses, one pass: ambiguous = untrusted
        "mx.google.com; dmarc=pass header.from=linkedin.com; dmarc=pass header.from=linkedin.com",
        "mx.google.com; dmarc=fail header.from=evil.io; dmarc=pass header.from=linkedin.com",
        # a quoted header.i / header.d is attacker text and is discarded
        'mx.google.com; dkim=pass header.i="@linkedin.com" header.d="linkedin.com"',
        'mx.google.com; dmarc=pass header.from="linkedin.com"',
        # unquoted injection through whitespace-free values
        "mx.google.com; spf=pass smtp.mailfrom=x=header.from=linkedin.com; dmarc=none",
        "mx.google.com; dmarc=none header.from=linkedin.com",
    ],
)
def test_forged_clauses_never_make_a_platform_mail_verified(header):
    (p,) = parse_messages(with_auth(eml("linkedin_alert_5.eml"), header))
    assert p.trust == "untrusted"
    assert classify(p, None, NOW).status == "dkim_fail"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "header",
    [
        "mx.google.com; spf=pass smtp.mailfrom=" + QUOTED.format(dom="example.com"),
        "mx.google.com; spf=pass smtp.mailfrom="
        + QUOTED.format(dom="example.com")
        + "; dmarc=fail header.from=example.com",
        "mx.google.com; dmarc=pass header.from=example.com; dmarc=pass header.from=example.com",
    ],
)
def test_forged_clauses_never_unwrap_a_spoofed_forward(header):
    raw = with_auth(eml("forward_inline_crm.eml"), header)
    (p,) = parse_messages(raw, forwarders=OWNER)
    assert not p.forwarded and p.trust != "owner_forwarded"
    assert classify(p, None, NOW).status != "crm"  # type: ignore[arg-type]


def test_lexer_keeps_the_legit_google_header_working():
    a = auth_of(
        "mx.google.com; dkim=pass header.i=@example.com header.s=google; arc=pass (i=1); "
        "spf=pass (google.com: domain of o@example.com designates 1.2.3.4) "
        'smtp.mailfrom="o;x"@example.com; dmarc=pass (p=NONE) header.from=example.com'
    )
    assert a is not None and a.dmarc_from() == "example.com"
    assert a.dkim_domains() == ("example.com",)
    assert [c[0] for c in a.clauses] == ["dkim", "arc", "spf", "dmarc"]
    assert "smtp.mailfrom" not in a.clauses[2][2]  # the quoted value is dropped


def test_comment_nesting_and_escapes_do_not_split_clauses():
    a = auth_of("mx.google.com; dmarc=pass (a (b; dmarc=fail) \\) ; c) header.from=example.com")
    assert a is not None and len(a.clauses) == 1 and a.dmarc_from() == "example.com"


# --- SEC-08 -------------------------------------------------------------------------------------


def test_forward_as_attachment_unwraps_only_the_first_level():
    crm = email.message_from_bytes(
        eml("linkedin_hit_reply_autoforwarded.eml"), policy=policy.default
    )
    middle = EmailMessage()
    middle["From"] = "Jobs <jobalerts-noreply@linkedin.com>"
    middle["Subject"] = "middle"
    middle["Message-ID"] = "<middle@mail.example.com>"
    middle.set_content("see attachment")
    middle.add_attachment(crm, disposition="attachment")  # a hit-reply nested inside
    outer = EmailMessage()
    outer["From"] = "Owner <owner@example.com>"
    outer["Subject"] = "Fwd"
    outer["Message-ID"] = "<outer@mail.example.com>"
    outer["Authentication-Results"] = "mx.google.com; dmarc=pass header.from=example.com"
    outer.set_content("fwd")
    outer.add_attachment(middle, disposition="attachment")
    got = parse_messages(outer.as_bytes(), forwarders=OWNER)
    assert [p.from_addr for p in got] == ["jobalerts-noreply@linkedin.com"]  # not the nested one
    assert all(p.trust == "owner_forwarded" for p in got)


# --- SEC-09 -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "mail jane​@agency​.example.org now",  # zero-width characters
        "mail ｊａｎｅ＠ａｇｅｎｃｙ．ｅｘａｍｐｌｅ．ｏｒｇ now",  # fullwidth forms
        "call +351​ 912‍ 345 678 please",
        "call ＋３５１ ９１２ ３４５ ６７８ please",
    ],
)
def test_obfuscated_contacts_are_masked_in_llm_input(text):
    p = ParsedEmail("sam@agency.example.org", "Role", None, None, text, "", [])
    out = prepare_llm_input(p, [])
    assert "agency" not in out and "912" not in out and "jane" not in out
    assert "[email]" in out or "[phone]" in out


# --- QA lows ------------------------------------------------------------------------------------


def test_note_for_platforms_without_canonical_links_and_warning_on_zero(caplog):
    p = ParsedEmail("donotreply@jobalert.indeed.com", "s", None, None, "", "", [])
    assert registry._count_note(p, "indeed", 10) == "10 jobs"
    with caplog.at_level(logging.WARNING, logger="cvboost.mail"):
        assert registry._count_note(p, "indeed", 0) == "0 jobs"
    assert "no jobs parsed" in caplog.text


def test_linkedin_card_does_not_walk_up_into_the_alert_intro():
    text = (
        "Your job alert has been created: Dbt in Portugal.\n"
        "You’ll receive notifications when new jobs are posted.\n\n"
        "View job: https://www.linkedin.com/comm/jobs/view/4487361379?trackingId=x\n"
    )
    p = ParsedEmail("jobalerts-noreply@linkedin.com", "s", None, None, text, "", [])
    assert registry.linkedin.parse(p) == []  # a link without a card is not a job


@pytest.mark.parametrize(
    "name", ["Confidential", "Undisclosed", "Not disclosed", "Undisclosed client"]
)
def test_confidential_style_companies_are_not_dedupe_keys(name):
    assert not has_key(company_norm(name))
    assert has_key(company_norm("Acme"))


# --- round 4: SEC-07b (comment escapes, fail closed, dkim consistency) --------------------------

ESC = '"a) dmarc=pass header.from={dom}; x"@evil.example.net'
GMAIL_SPF = "spf=pass (google.com: domain of {env} designates 1.2.3.4 as permitted sender) "


@pytest.mark.parametrize("dom", ["linkedin.com"])
@pytest.mark.parametrize(
    "header",
    [
        # the quoted envelope inside the spf COMMENT closes the comment early if quotes are ignored
        "mx.google.com; "
        + GMAIL_SPF.format(env=ESC.format(dom="linkedin.com"))
        + "smtp.mailfrom=bounce@evil.example.net; dmarc=fail header.from=linkedin.com",
        "mx.google.com; "
        + GMAIL_SPF.format(env=ESC.format(dom="linkedin.com"))
        + "smtp.mailfrom=bounce@evil.example.net",
        # unclosed ( or " at the end must not swallow Gmail's real dmarc=fail
        "mx.google.com; dmarc=fail header.from=linkedin.com; spf=pass (unclosed dmarc=pass",
        'mx.google.com; dmarc=fail header.from=linkedin.com; spf=pass smtp.mailfrom="oops',
        'mx.google.com; spf=pass (c "unclosed ; dmarc=pass header.from=linkedin.com); '
        "dmarc=fail header.from=linkedin.com",
        "mx.google.com; dmarc=pass header.from=linkedin.com) ",  # stray )
        # forged dkim=pass after a comment, next to a real failing one / without dmarc
        "mx.google.com; spf=pass (x) ; dkim=fail header.d=evil.io; dkim=pass header.d=linkedin.com",
        "mx.google.com; dkim=pass header.d=linkedin.com; dkim=neutral header.d=linkedin.com",
        "mx.google.com; dkim=pass header.d=linkedin.com; dkim=pass header.d=evil.io",
        # too long: no silent truncation, even if the real result sits in the first 8000 chars
        "mx.google.com; dmarc=pass header.from=linkedin.com; spf=pass (" + "x" * 9000 + ")",
    ],
)
def test_round4_escapes_are_untrusted(header, dom):
    (p,) = parse_messages(with_auth(eml("linkedin_alert_5.eml"), header))
    assert p.trust == "untrusted"
    assert classify(p, None, NOW).status == "dkim_fail"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "header",
    [
        "mx.google.com; "
        + GMAIL_SPF.format(env=ESC.format(dom="example.com"))
        + "smtp.mailfrom=bounce@evil.example.net",
        "mx.google.com; "
        + GMAIL_SPF.format(env=ESC.format(dom="example.com"))
        + "smtp.mailfrom=b@evil.example.net; dmarc=fail header.from=example.com",
        "mx.google.com; dmarc=fail header.from=example.com; spf=pass (unclosed dmarc=pass",
    ],
)
def test_round4_spoofed_forward_is_not_unwrapped(header):
    (p,) = parse_messages(with_auth(eml("forward_inline_crm.eml"), header), forwarders=OWNER)
    assert not p.forwarded and p.trust != "owner_forwarded"
    assert classify(p, None, NOW).status != "crm"  # type: ignore[arg-type]


def test_round4_header_over_the_limit_is_invalid_not_truncated():
    a = auth_of("mx.google.com; dmarc=pass header.from=linkedin.com; spf=pass (" + "x" * 9000 + ")")
    assert (
        a is not None
        and a.invalid
        and a.dmarc_from() is None
        and not a.authenticates("linkedin.com")
    )


def test_round4_legit_gmail_header_with_quoted_envelope_in_the_comment_is_verified():
    env = '"o;)x"@example.com'
    header = (
        "mx.google.com; dkim=pass header.i=@linkedin.com header.s=s; "
        f"spf=pass (google.com: domain of {env} designates 1.2.3.4 as permitted sender) "
        "smtp.mailfrom=bounce@linkedin.com; dmarc=pass (p=NONE sp=NONE dis=NONE) "
        "header.from=linkedin.com"
    )
    (p,) = parse_messages(with_auth(eml("linkedin_alert_5.eml"), header))
    assert p.trust == "verified"


def test_round5_only_dmarc_decides_dkim_alone_is_untrusted():
    base = eml("linkedin_alert_5.eml")
    for header in (
        "mx.google.com; dkim=pass header.d=maild.linkedin.com",
        "mx.google.com; dkim=pass header.d=linkedin.com; spf=pass smtp.mailfrom=a@linkedin.com",
        "mx.google.com; dkim=pass header.d=maild.linkedin.com; dmarc=fail header.from=linkedin.com",
    ):
        (p,) = parse_messages(with_auth(base, header))
        assert p.trust == "untrusted" and classify(p, None, NOW).status == "dkim_fail"  # type: ignore[arg-type]
    ok = "mx.google.com; dkim=pass header.d=x.io; dmarc=pass header.from=linkedin.com"
    assert parse_messages(with_auth(base, ok))[0].trust == "verified"


@pytest.mark.parametrize(
    "header",
    [
        # RFC 2047 encoded words are decoded by policy.default: lex the RAW header instead
        "mx.google.com; spf=pass smtp.mailfrom="
        "=?us-ascii?q?x=3Bdmarc=3Dpass_header.from=3D{d}?=@evil.com",
        "mx.google.com; dkim=pass header.i==?us-ascii?q?=40{d}?=",
        "mx.google.com; spf=pass "
        "(domain of =?utf-8?q?a=29_dmarc=3Dpass_header.from=3D{d}?=@evil.com)",
    ],
)
@pytest.mark.parametrize("dom", ["linkedin.com", "example.com"])
def test_round5_encoded_words_make_the_header_invalid(header, dom):
    h = header.format(d=dom)
    a = auth_of(h)
    assert a is not None and a.invalid and not a.authenticates(dom)
    (p,) = parse_messages(with_auth(eml("linkedin_alert_5.eml"), h))
    assert p.trust == "untrusted"
    (q,) = parse_messages(with_auth(eml("forward_inline_crm.eml"), h), forwarders=OWNER)
    assert not q.forwarded


def test_round5_the_real_headers_still_verify():
    legit = (
        "mx.google.com; dkim=pass header.i=@maild.linkedin.com header.s=d2048-202308-0d "
        "header.b=d31OQWLW; spf=pass (google.com: domain of bounce@bounce.linkedin.com designates "
        "1.2.3.4 as permitted sender) smtp.mailfrom=bounce@bounce.linkedin.com; "
        "dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=linkedin.com"
    )
    assert parse_messages(with_auth(eml("linkedin_alert_5.eml"), legit))[0].trust == "verified"
    fwd = (
        "mx.google.com; dkim=pass header.i=@example.com header.s=google header.b=c8fWyBJs; "
        "arc=pass (i=1); spf=neutral (google.com: 209.85.220.41 is neither permitted nor denied "
        "by domain of o@example.com) smtp.mailfrom=o@example.com; "
        "dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=example.com; dara=pass"
    )
    (p,) = parse_messages(with_auth(eml("forward_inline_crm.eml"), fwd), forwarders=OWNER)
    assert p.forwarded and p.trust == "owner_forwarded"
