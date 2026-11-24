from __future__ import annotations

import pytest

from cvboost.crm.handler import handle_email, matches_sender
from cvboost.crm.parse import parse_linkedin, safe_linkedin_url
from cvboost.crm.types import EVENT_KINDS
from tests.crm.helpers import ALL, load


def test_every_fixture_parses_with_only_https_linkedin_links():
    assert len(ALL) >= 6
    for f in ALL:
        evs = parse_linkedin(load(f.name))
        assert evs, f.name
        for e in evs:
            assert e.kind in EVENT_KINDS
            for u in (e.profile_url, e.view_url):
                assert u is None or (u.startswith("https://www.linkedin.com/") and "?" not in u)
            assert len(e.snippet) <= 500


def test_invitation_accepted():
    (e,) = parse_linkedin(load("linkedin_crm_invitation_accepted.eml"))
    assert (e.kind, e.name) == ("invitation_accepted", "Jane Example")
    assert e.headline == "Senior Technical Recruiter at Example Talent Partners"
    assert e.company == "Example Talent Partners"
    assert e.profile_url == "https://www.linkedin.com/comm/in/jane-example-1a2b3c"


def test_new_message_has_snippet_and_view_link():
    (e,) = parse_linkedin(load("linkedin_crm_message.eml"))
    assert (e.kind, e.name, e.company) == ("message", "Tom Sample", "Sample Analytics Ltd")
    assert e.snippet.startswith("Hi Jordan, we are looking for a contract")
    assert "short call this week" in e.snippet
    assert e.view_url == "https://www.linkedin.com/comm/messaging/thread/2-ABCDEF123456/"


def test_missing_headline_is_fine():
    (e,) = parse_linkedin(load("linkedin_crm_message_no_headline.eml"))
    assert e.name == "Anna Demo" and e.headline is None and e.company is None
    assert e.snippet.startswith("Thanks for connecting")


def test_html_only_mail_and_bad_links_dropped():
    (e,) = parse_linkedin(load("linkedin_crm_message_htmlonly.eml"))
    assert e.name == "Mark Test" and e.company == "Test Agency"
    assert "send me your rate" in e.snippet
    assert e.view_url == "https://www.linkedin.com/comm/messaging/thread/2-HTML777/"


def test_digest_gives_one_event_per_sender():
    evs = parse_linkedin(load("linkedin_crm_digest.eml"))
    assert [e.name for e in evs] == ["Olivia Placeholder", "Peter Fixture", "Quinn Mock"]
    assert [e.index for e in evs] == [0, 1, 2]
    assert evs[0].company == "Placeholder Recruiting" and evs[1].company is None
    assert evs[2].headline is None and "server-side GTM" in evs[2].snippet
    assert all(e.view_url for e in evs)


def test_unknown_layout_is_an_other_event_with_the_subject():
    (e,) = parse_linkedin(load("linkedin_crm_unknown.eml"))
    assert e.kind == "other" and e.name == ""
    assert e.snippet == "Jordan, your profile was viewed 12 times this week"


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://www.linkedin.com/in/x?trk=1", "https://www.linkedin.com/in/x"),
        ("https://linkedin.com/in/x", "https://linkedin.com/in/x"),
        ("http://www.linkedin.com/in/x", None),
        ("https://www.linkedin.com.evil.example.com/in/x", None),
        ("https://evil.example.com/www.linkedin.com", None),
        ("https://user:pw@www.linkedin.com/in/x", None),
        ("https://www.linkedin.com:8443/in/x", None),
        ("javascript:alert(1)", None),
        ("https://www.linkedin.com/in/x y", None),
        (None, None),
    ],
)
def test_safe_linkedin_url(url, ok):
    assert safe_linkedin_url(url) == ok


def test_hostile_text_stays_data():
    p = load("linkedin_crm_message.eml")
    p.subject = "<script>alert(1)</script> sent you a message"
    p.text = "x\n" * 5
    (e,) = parse_linkedin(p)
    assert e.name.startswith("<script>")  # kept as text; the templates escape it
    assert e.kind == "message"


def test_matches_sender_is_exact():
    assert matches_sender("hit-reply@linkedin.com")
    assert matches_sender("LinkedIn <HIT-REPLY@linkedin.com>")
    assert not matches_sender("jobalerts-noreply@linkedin.com")
    assert not matches_sender("hit-reply@linkedin.com.evil.example.com")


def test_handler_records_and_reports_crm(tmp_path):
    from cvboost.storage.db import Store, connect, migrate
    from tests.conftest import NOW

    conn = connect(tmp_path / "h.sqlite")
    migrate(conn)
    store = Store(conn)
    p = load("linkedin_crm_digest.eml")
    r = handle_email(p, store, NOW)
    assert r.status == "crm" and "3 new" in r.note
    r2 = handle_email(p, store, NOW)  # the same mail again: nothing new
    assert r2.status == "crm" and "0 new" in r2.note
    assert conn.execute("SELECT COUNT(*) FROM crm_events").fetchone()[0] == 3


@pytest.mark.parametrize(
    "name", ["linkedin_crm_message_accepted.eml", "linkedin_crm_message_accepted_fwd.eml"]
)
def test_inmail_accepted_real_layout(name):
    """Layout of the first real mail (Sales Navigator «InMail accepted»), names replaced."""
    (e,) = parse_linkedin(load(name))
    assert (e.kind, e.name) == ("message_accepted", "Nora Sample")
    assert e.snippet.startswith("Hi Jordan, I would be interested in seeing this.")
    assert e.snippet.endswith("sync up a time?")
    assert "View in Sales" not in e.snippet and "http" not in e.snippet
    assert e.view_url == (
        "https://www.linkedin.com/sales/inbox/2-ZXhhbXBsZS10aHJlYWQtaWQtMDAxXzEwMA=="
    )
    assert e.profile_url is None  # the only /in/ link is the owner's own profile
    assert e.headline is None and e.company is None


def test_sales_inbox_url_rejects_other_wrappers():
    from cvboost.crm.parse import sales_inbox_url

    assert sales_inbox_url("https://www.linkedin.com/sales-web-view?url=x") is None
    assert (
        sales_inbox_url("https://evil.example.com/sales-web-view?url=%2Fsales%2Finbox%2F2-abcd")
        is None
    )


# --- security: links come only from the right place (digest blocks, profile anchors) ---


def test_digest_each_person_gets_their_own_link_and_profile():
    p = load("linkedin_crm_digest.eml")
    p.text = p.text.replace(
        "View message: https://www.linkedin.com/comm/messaging/thread/2-DIG111/?trk=digest",
        "View profile: https://www.linkedin.com/comm/in/olivia-placeholder-1\n"
        "View message: https://www.linkedin.com/comm/messaging/thread/2-DIG111/?trk=digest",
    )
    evs = parse_linkedin(p)
    assert [e.view_url.rsplit("/", 2)[-2] for e in evs] == ["2-DIG111", "2-DIG222", "2-DIG333"]
    assert len({e.view_url for e in evs}) == 3
    assert [e.profile_url for e in evs] == [
        "https://www.linkedin.com/comm/in/olivia-placeholder-1",
        None,
        None,
    ]  # nobody inherits another block's /in/ link


def test_digest_block_without_its_own_link_is_skipped_and_stray_text_makes_no_contact():
    p = load("linkedin_crm_digest.eml")
    p.text = p.text.replace(
        "View message: https://www.linkedin.com/comm/messaging/thread/2-DIG222/?trk=digest\n", ""
    )
    assert [e.name for e in parse_linkedin(p)] == ["Olivia Placeholder", "Quinn Mock"]
    # a sender's message with blank lines and a bare link must not become a contact
    p = load("linkedin_crm_digest.eml")
    p.text = p.text.replace(
        '"Saw your GA4 article, can we talk?"',
        '"hi"\n\nEvil Person\n"pay me"\nhttps://www.linkedin.com/messaging/thread/2-EVIL/\n',
    )
    names = [e.name for e in parse_linkedin(p)]
    assert "Evil Person" not in names and names == ["Olivia Placeholder", "Quinn Mock"]


def test_digest_html_only_blocks_use_their_own_anchors():
    p = load("linkedin_crm_digest.eml")
    h = "".join(
        f'<p>{n}</p><p>"msg {i}"</p><p><a href="https://www.linkedin.com/comm/messaging/thread/2-H{i}/'
        f'?trk=x">View message</a></p><p>&nbsp;</p>'
        for i, n in enumerate(["Ann One", "Bob Two"])
    )
    p.text, p.html = "", f"<html><body><p>You have 2 new messages</p>{h}</body></html>"
    evs = parse_linkedin(p)
    assert [e.name for e in evs] == ["Ann One", "Bob Two"]
    assert [e.view_url.split("/")[-2] for e in evs] == ["2-H0", "2-H1"]
    assert evs[0].view_url != evs[1].view_url


def test_message_text_with_a_profile_link_is_not_used_as_the_profile():
    p = load("linkedin_crm_message.eml")
    p.text = p.text.replace(
        'short call this week?"',
        "short call? see https://www.linkedin.com/in/victim-9z and View profile: "
        'https://www.linkedin.com/in/victim-9z"',
    )
    (e,) = parse_linkedin(p)
    assert e.profile_url == "https://www.linkedin.com/comm/in/tom-sample-9f8e7d"
    p.text = p.text.replace("View profile: https://www.linkedin.com/comm/in/tom-sample-9f8e7d", "")
    (e,) = parse_linkedin(p)
    assert e.profile_url is None  # nothing outside the text: no profile at all
    assert "victim" in e.snippet  # kept as plain text


def test_links_inside_the_snippet_are_not_view_links():
    p = load("linkedin_crm_message.eml")
    p.text = p.text.replace(
        "View message: https://www.linkedin.com/comm/messaging/thread/2-ABCDEF123456/?midToken=AQxyz&trk=eml-msg\n"
        "Reply: https://www.linkedin.com/comm/messaging/thread/2-ABCDEF123456/?reply=1\n",
        "",
    ).replace('"Hi Jordan,', '"Hi Jordan, https://www.linkedin.com/messaging/thread/2-FAKE/')
    (e,) = parse_linkedin(p)
    assert e.view_url is None


def test_handler_caps_events_per_mail(tmp_path):
    from dataclasses import replace

    from cvboost.crm.handler import MAX_EVENTS, handle_email
    from cvboost.storage.db import Store, connect, migrate
    from tests.conftest import NOW

    conn = connect(tmp_path / "cap.sqlite")
    migrate(conn)
    blocks = "\n\n".join(
        f'Person {i:02d}\n"hello {i}"\nView message: '
        f"https://www.linkedin.com/comm/messaging/thread/2-T{i:02d}/"
        for i in range(30)
    )
    p = replace(load("linkedin_crm_digest.eml"), text="You have 30 new messages\n\n" + blocks)
    r = handle_email(p, Store(conn), NOW)
    assert conn.execute("SELECT COUNT(*) FROM crm_events").fetchone()[0] == MAX_EVENTS == 20
    assert r.status == "crm" and "10 over the cap skipped" in r.note


# --- #37 (SEC-33-R2-01): the quote is the sender's; links count only after its closing mark ---

REAL_PROFILE = "https://www.linkedin.com/comm/in/tom-sample-9f8e7d"
REAL_VIEW = "https://www.linkedin.com/comm/messaging/thread/2-ABCDEF123456/"


def _with_inside(line: str):
    p = load("linkedin_crm_message.eml")
    p.text = p.text.replace(
        'Would you have time for a short call this week?"',
        f'{line}\nWould you have time for a short call this week?"',
    )
    assert line in p.text
    return p


def test_A_profile_line_inside_the_message_does_not_win():
    p = _with_inside("View profile: https://www.linkedin.com/in/victim-9z")
    (e,) = parse_linkedin(p)
    assert e.profile_url == REAL_PROFILE and e.view_url == REAL_VIEW
    assert "victim-9z" in e.snippet and "short call this week" in e.snippet  # the whole message


def test_A2_reply_and_message_lines_inside_the_message_do_not_win():
    for line in (
        "Reply: https://www.linkedin.com/comm/messaging/thread/2-FAKE/",
        "Message Tom: https://www.linkedin.com/messaging/thread/2-FAKE/",
        "View message: https://www.linkedin.com/messaging/thread/2-FAKE/",
    ):
        (e,) = parse_linkedin(_with_inside(line))
        assert e.view_url == REAL_VIEW and e.profile_url == REAL_PROFILE, line


def test_A_without_the_real_footer_profile_is_none():
    p = _with_inside("View profile: https://www.linkedin.com/in/victim-9z")
    p.text = p.text.replace(f"View profile: {REAL_PROFILE}?trk=eml-msg", "")
    p.text = p.text.replace("?midToken=AQxyz&trk=eml-msg", "")
    (e,) = parse_linkedin(p)
    assert e.profile_url is None and e.view_url == REAL_VIEW


def test_unquoted_message_layout_takes_no_links_at_all():
    p = load("linkedin_crm_message.eml")
    p.text = p.text.replace('"Hi Jordan,', "Hi Jordan,").replace('this week?"', "this week?")
    (e,) = parse_linkedin(p)
    assert e.profile_url is None and e.view_url is None  # in doubt: nothing


def test_inmail_look_alike_cta_inside_the_text_does_not_win():
    p = load("linkedin_crm_message_accepted.eml")
    p.text = p.text.replace(
        "sync up a time?",
        "sync up a time?\nView in Sales Navigator https://www.linkedin.com/sales/inbox/2-FAKEFAKE",
    )
    (e,) = parse_linkedin(p)
    assert (
        e.view_url == "https://www.linkedin.com/sales/inbox/2-ZXhhbXBsZS10aHJlYWQtaWQtMDAxXzEwMA=="
    )
    assert "FAKEFAKE" not in (e.view_url or "")


HTML_MSG = (
    "<html><body><p>Tom Sample</p><p>Head of Data at Sample Analytics Ltd</p>"
    '<blockquote>"Hi Jordan, {inside} Call this week?"</blockquote>'
    '<p><a href="https://www.linkedin.com/comm/messaging/thread/2-REAL1/?trk=x">'
    "View message</a></p>"
    "{footer}</body></html>"
)
FOOTER = (
    '<p><a href="https://www.linkedin.com/comm/in/tom-sample-9f8e7d?trk=x">View profile</a></p>'
)
INSIDE = (
    '<a href="https://www.linkedin.com/in/victim-9z">View profile</a> '
    '<a href="https://www.linkedin.com/comm/messaging/thread/2-FAKE/">Reply</a>'
)


def _html(inside="", footer=""):
    p = load("linkedin_crm_message_htmlonly.eml")
    p.subject = "Tom Sample sent you a message"
    p.text, p.html = "", HTML_MSG.format(inside=inside, footer=footer)
    return p


def test_B2_html_only_anchor_inside_the_quote_is_ignored():
    (e,) = parse_linkedin(_html(INSIDE, FOOTER))
    assert e.profile_url == "https://www.linkedin.com/comm/in/tom-sample-9f8e7d"
    assert e.view_url == "https://www.linkedin.com/comm/messaging/thread/2-REAL1/"
    (e,) = parse_linkedin(_html(INSIDE, ""))  # no real profile link: none, never the injected one
    assert e.profile_url is None
    assert e.view_url == "https://www.linkedin.com/comm/messaging/thread/2-REAL1/"


def test_B2_html_only_without_blockquote_markup_uses_the_text_quote_boundary():
    h = HTML_MSG.replace("<blockquote>", "<p>").replace("</blockquote>", "</p>")
    p = load("linkedin_crm_message_htmlonly.eml")
    p.subject = "Tom Sample sent you a message"
    p.text, p.html = "", h.format(inside=INSIDE, footer=FOOTER)
    (e,) = parse_linkedin(p)
    assert e.profile_url == "https://www.linkedin.com/comm/in/tom-sample-9f8e7d"
    assert e.view_url == "https://www.linkedin.com/comm/messaging/thread/2-REAL1/"
