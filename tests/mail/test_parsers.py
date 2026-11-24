"""Deterministic parsers over sanitised copies of real alert mails (2026-11-10).

Fixtures marked SYNTHETIC are guesses (no real sample yet) and say so in their name.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from cvboost.mail import canonical
from cvboost.mail.message import ParsedEmail, parse_messages
from cvboost.mail.parsers import registry
from cvboost.mail.parsers.registry import classify
from tests.conftest import NOW
from tests.mail.helpers import eml


def run(name: str, **kw):
    (p,) = parse_messages(eml(name))
    return p, classify(p, None, NOW, **kw)  # type: ignore[arg-type]


def test_linkedin_alert_5_jobs_canonical_links_and_no_tracking():
    p, c = run("linkedin_alert_5.eml")
    assert (c.status, c.platform, len(c.jobs)) == ("parsed", "linkedin", 5)
    for j in c.jobs:
        assert j.url == f"https://www.linkedin.com/jobs/view/{j.source_id}/"
        assert j.title and j.company and j.location
        blob = " ".join([j.title, j.company, j.location, j.url])
        assert "trackingId" not in blob and "unsubscribe" not in blob.lower()
    assert p.received_at == datetime(2026, 11, 10, 4, 16, 56, tzinfo=UTC)


def test_linkedin_alert_6_jobs_and_auto_forwarded_headers_do_not_matter():
    # the main path: Gmail filter forwarding keeps From=linkedin and adds X-Forwarded-For
    raw = eml("linkedin_alert_5.eml")
    assert b"X-Forwarded-For:" in raw
    _, c = run("linkedin_alert_5.eml")
    assert c.status == "parsed"
    _, c6 = run("linkedin_alert_6.eml")
    assert len(c6.jobs) == 6
    assert c6.jobs[0].title == "Data Engineer" and c6.jobs[0].company == "Experis"
    assert c6.jobs[0].location == "San Francisco, California, United States"


def test_linkedin_html_fallback_is_used_when_text_has_no_blocks():
    p, _ = run("linkedin_alert_5.eml")
    html_only = ParsedEmail(**{**p.__dict__, "text": "see html"})
    jobs = registry.linkedin.parse(html_only)
    assert len(jobs) == 5 and jobs[0].url.startswith("https://www.linkedin.com/jobs/view/")


def test_dice_alert_5_jobs():
    _, c = run("dice_alert_5.eml")
    assert (c.status, c.platform, len(c.jobs)) == ("parsed", "dice", 5)
    j = c.jobs[1]
    assert j.title == "Azure Data Engineer (ADF)" and j.company == "Talent Groups"
    assert j.url == "https://www.dice.com/job-detail/deb6060a-f5e7-402c-926b-14c48e53fb94"
    assert j.remote is True and j.posted_at == datetime(2026, 11, 9, tzinfo=UTC)


def test_indeed_alert_10_jobs_no_tracking_links():
    _, c = run("indeed_alert_10.eml")
    assert (c.status, c.platform, len(c.jobs)) == ("parsed", "indeed", 10)
    assert all("engage.indeed.com" not in j.url for j in c.jobs)
    assert all(j.url.startswith("https://pt.indeed.com/jobs?q=") for j in c.jobs)
    assert len({j.source_id for j in c.jobs}) == 10
    j = next(j for j in c.jobs if j.company == "Solutiva")
    assert j.title == "Consultor comercial independente" and "€ por mês" in (j.salary_text or "")
    j = next(j for j in c.jobs if j.company == "Dentsply Sirona")
    assert j.location == "Remoto" and j.remote is True and j.salary_text is None


@pytest.mark.parametrize(
    "name",
    [
        "indeed_login_code.eml",
        "indeed_passkey.eml",
        "dice_verify.eml",
        "freelancermap_activate.eml",
        "freelancermap_alert_limit.eml",
    ],
)
def test_account_mails_are_ignored_without_llm(name):
    _, c = run(name)
    assert c.status == "ignored" and c.llm_input is None and not c.jobs


def test_freelancermap_project_alert_SYNTHETIC():
    _, c = run("freelancermap_projects_SYNTHETIC.eml")
    assert (c.status, len(c.jobs)) == ("parsed", 2)
    assert c.jobs[0].url == "https://www.freelancermap.de/projekt/senior-data-engineer-m-w-d"
    assert "abmelden" not in " ".join(j.url for j in c.jobs)


def test_unknown_sender_goes_to_llm_with_links_replaced():
    p, c = run("recruiter_unknown.eml")
    assert (c.status, c.platform) == ("needs_llm", "recruiter")
    assert c.llm_input and "[link]" in c.llm_input and "agency.example.org" not in c.llm_input
    assert "Subject: Contract role" in c.llm_input and len(c.llm_input) <= 8000


def test_platform_mail_needs_an_authenticated_from_domain():
    p, _ = run("linkedin_alert_5.eml")
    assert p.trust == "verified" and "maild.linkedin.com" in p.dkim_domains
    spoof = ParsedEmail(**{**p.__dict__, "trust": "untrusted"})
    c = classify(spoof, None, NOW)  # type: ignore[arg-type]
    assert (c.status, c.jobs) == ("dkim_fail", [])  # no parser, no LLM: ignored and logged
    assert classify(spoof, None, NOW, require_dkim=False).status == "parsed"  # type: ignore[arg-type]
    owner = ParsedEmail(**{**p.__dict__, "trust": "owner_forwarded"})
    assert classify(owner, None, NOW).status == "parsed"  # type: ignore[arg-type]


def _with_auth(raw: bytes, header: str | None, name: str = "Authentication-Results") -> bytes:
    import email
    from email import policy

    m = email.message_from_bytes(raw, policy=policy.default)
    del m["Authentication-Results"]
    if header:
        m[name] = header
    return m.as_bytes()


ATTACK = (
    "mx.google.com; spf=pass smtp.mailfrom=dkim=pass.header.d=linkedin.com@attacker.example.net; "
    "dmarc=fail header.from=linkedin.com"
)


@pytest.mark.parametrize(
    "header",
    [
        ATTACK,  # SEC-01: the payload sits in an envelope local part, dmarc failed
        "mx.google.com; dkim=fail header.d=linkedin.com; dmarc=fail header.from=linkedin.com",
        "mx.example.net; dkim=pass header.d=linkedin.com; dmarc=pass header.from=linkedin.com",
        "mx.google.com; dkim=pass header.d=attacker.example; dmarc=pass header.from=attacker.io",
        "mx.google.com; dkim=pass (comment dmarc=pass header.from=linkedin.com) header.d=evil.io",
    ],
)
def test_spoofed_platform_mail_is_not_verified_and_not_parsed(header):
    raw = _with_auth(eml("linkedin_alert_5.eml"), header)
    (p,) = parse_messages(raw)
    assert p.trust == "untrusted"
    c = classify(p, None, NOW)  # type: ignore[arg-type]
    assert c.status == "dkim_fail" and not c.jobs and c.llm_input is None


def test_only_the_topmost_trusted_authentication_results_counts():
    import email
    from email import policy

    raw = _with_auth(eml("linkedin_alert_5.eml"), None)
    m = email.message_from_bytes(raw, policy=policy.default)
    # a forged header added by the sender sits BELOW ours, and one from another server is ignored
    m["Authentication-Results"] = "mx.google.com; dmarc=fail header.from=linkedin.com"
    m["Authentication-Results"] = "other.example; dmarc=pass header.from=linkedin.com"
    (p,) = parse_messages(m.as_bytes())
    assert p.trust == "untrusted"
    top = "mx.google.com; dmarc=pass (policy=none) header.from=linkedin.com"
    m2 = email.message_from_bytes(
        _with_auth(eml("linkedin_alert_5.eml"), top), policy=policy.default
    )
    m2["Authentication-Results"] = "mx.google.com; dmarc=fail header.from=linkedin.com"
    m2.add_header("Authentication-Results", top)
    del m2["Authentication-Results"]
    m2["Authentication-Results"] = top
    assert parse_messages(m2.as_bytes())[0].trust == "verified"


def test_dmarc_pass_with_related_header_from_verifies_and_authserv_is_configurable():
    raw = _with_auth(
        eml("linkedin_alert_5.eml"),
        "mx.google.com; dmarc=pass (p=NONE) header.from=linkedin.com",
    )
    assert parse_messages(raw)[0].trust == "verified"
    raw = _with_auth(
        eml("linkedin_alert_5.eml"), "mail.mine.example; dmarc=pass header.from=linkedin.com"
    )
    assert parse_messages(raw)[0].trust == "untrusted"
    assert parse_messages(raw, authserv="mail.mine.example")[0].trust == "verified"


def test_linkedin_messaging_senders_never_parsed_as_jobs():
    p, _ = run("linkedin_alert_5.eml")
    for addr in registry.DEFAULT_CRM_SENDERS:
        c = classify(ParsedEmail(**{**p.__dict__, "from_addr": addr}), None, NOW)  # type: ignore[arg-type]
        assert c.status == "crm" and not c.jobs
        low = ParsedEmail(**{**p.__dict__, "from_addr": addr, "trust": "untrusted"})
        assert classify(low, None, NOW).status == "crm_untrusted"  # type: ignore[arg-type]


def test_note_shows_jobs_and_links_and_warns_when_fewer_jobs_than_links(caplog):
    import logging

    p, c = run("linkedin_alert_5.eml")
    assert c.note == "5 jobs / 5 links"
    extra = ParsedEmail(
        **{**p.__dict__, "links": [*p.links, "https://www.linkedin.com/jobs/view/4999999999"]}
    )
    with caplog.at_level(logging.WARNING, logger="cvboost.mail"):
        c = classify(extra, None, NOW)  # type: ignore[arg-type]
    assert (
        c.status == "parsed"
        and c.note == "5 jobs / 6 links"
        and "5 jobs parsed but 6" in caplog.text
    )


def test_linkedin_card_is_structural_not_positional():
    p, _ = run("linkedin_alert_5.eml")
    noisy = p.text.replace(
        "\n\nThis company is actively hiring\nView job:",
        "\n$90 - $120/hr\n2 connections\n\nThis company is actively hiring\nView job:",
    )
    jobs = registry.linkedin.parse(ParsedEmail(**{**p.__dict__, "text": noisy}))
    assert len(jobs) == 5 and jobs[0].title == "CRM & Growth Marketing Specialist"
    assert (
        jobs[0].location == "Dilbeek, Flemish Region, Belgium"
        and jobs[0].salary_text == "$90 - $120/hr"
    )


# --- canonical links ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "href,expected",
    [
        (
            "https://www.linkedin.com/comm/jobs/view/4487361379?trackingId=x&otpToken=y",
            "https://www.linkedin.com/jobs/view/4487361379/",
        ),
        (
            "https://www.linkedin.com/jobs/view/data-engineer-at-acme-4487361379?x=1",
            "https://www.linkedin.com/jobs/view/4487361379/",
        ),
        (
            "https://click.example.net/r?u=https%253A%252F%252Fwww.linkedin.com%252Fjobs%252Fview%252F4487361379",
            "https://www.linkedin.com/jobs/view/4487361379/",
        ),
        (
            "https://www.linkedin.com/jobs/search/?currentJobId=4487361379&x=1",
            "https://www.linkedin.com/jobs/view/4487361379/",
        ),
        (
            "https://www.dice.com/job-detail/EA15847D-D38B-45DC-A37A-1F5816A02AA0",
            "https://www.dice.com/job-detail/ea15847d-d38b-45dc-a37a-1f5816a02aa0",
        ),
        (
            "https://de.indeed.com/viewjob?from=x&jk=0123456789abcdef",
            "https://www.indeed.com/viewjob?jk=0123456789abcdef",
        ),
        (
            "https://www.freelancermap.com/project/abc-def-123",
            "https://www.freelancermap.com/project/abc-def-123",
        ),
    ],
)
def test_canonicalise(href, expected):
    link = canonical.canonicalise(href)
    assert link is not None and link.url == expected


@pytest.mark.parametrize(
    "href",
    [
        "https://www.linkedin.com/comm/mypreferences/u/emailunsub?x=1",
        "https://www.linkedin.com/jobs/view/12",  # id too short
        "https://notlinkedin.com/jobs/view/4487361379",
        "https://engage.indeed.com/f/a/oEiIAbK9~/AAR9hBA~/C8_Ggj5kyZq4",
        "https://www.dice.com/dashboard/alerts",
        "javascript:alert(1)",
    ],
)
def test_not_a_job_link(href):
    assert canonical.canonicalise(href) is None


def test_canonical_url_is_rebuilt_not_copied_from_a_look_alike_host():
    link = canonical.canonicalise(
        "https://evil.example/go?u=https://www.linkedin.com/jobs/view/4487361379"
    )
    assert link is not None and link.url == "https://www.linkedin.com/jobs/view/4487361379/"


# --- ReDoS --------------------------------------------------------------------------------------

N = 30000
HOSTILE = {
    "dashes": "-" * N,
    "spaces": " " * N,
    "digits": "9" * N,
    "view": "View job: " + "a" * N,
    "href": "https://www.linkedin.com/jobs/view/" + "a-" * (N // 2),
    "href-qs": "https://www.indeed.com/x?" + "a=b&" * (N // 4),
    "dice": "https://www.dice.com/job-detail/" + "a" * N,
    "subdomains": "https://" + "a." * (N // 2) + "linkedin.com/jobs/view/123456",
    "freelancermap": "https://www.freelancermap.de/projekt/" + "a-" * (N // 2),
    "lines": "x\n" * N,
    "pct": "%25" * (N // 3),
}


@pytest.mark.parametrize("key", HOSTILE)
def test_redos_canonical_and_parsers(key):
    s = HOSTILE[key]
    t0 = time.perf_counter()
    canonical.canonicalise(s)
    p = ParsedEmail("jobalerts-noreply@linkedin.com", "s", None, None, s, "", [s])
    for plat in registry.PLATFORMS:
        plat.parse(p)
    registry.IGNORE_SUBJECT.search(s)
    assert time.perf_counter() - t0 < 1.0, key
