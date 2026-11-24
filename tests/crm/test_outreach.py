from __future__ import annotations

import pytest

from cvboost.crm.outreach import OutreachConfig, find_emails, is_email, load_outreach
from tests.fixture_profile import OUTREACH_YAML

CFG = load_outreach(OUTREACH_YAML)


def test_config_has_the_three_approved_links():
    assert CFG.links == {
        "case_studies": "https://example.com/case-studies",
        "services": "https://example.com/services",
        "calendly": "https://calendly.com/jordan-example/30min",
    }


@pytest.mark.parametrize(
    "text",
    [
        "Book: https://calendly.com/jordan-example/30min.",
        "See https://example.com/case-studies/ and https://EXAMPLE.com/services",
        "Mail hello@example.com or write on LinkedIn",
        "No links here. Node.js and dbt are fine",
    ],
)
def test_approved_links_pass(text):
    assert CFG.foreign_links(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "https://evil.example.com/x",
        "http://example.com/case-studies/secret",
        "https://example.com/other",
        "https://calendly.com/jordan-example/30min?x=1",
        "www.example.com",
        "example.com/case-studies-extra",
        "go to bit.ly/abc or tinyurl.com/x",
        "https://calendly.com/jordan-example/30min.evil.com",
    ],
)
def test_other_urls_are_rejected(text):
    assert CFG.foreign_links(text)


def test_config_must_be_https():
    with pytest.raises(ValueError):
        OutreachConfig(links={"a": "http://x.com"})
    with pytest.raises(ValueError):
        OutreachConfig(links={"a": "https://x.com/ y"})


def test_emails_found_exactly_and_validated():
    assert find_emails(
        "Write to Hello.World+x@Example.COM, thanks. Or hello.world+x@Example.COM"
    ) == [
        "Hello.World+x@Example.COM",
        "hello.world+x@Example.COM",
    ]
    assert find_emails("no address, just name at example dot com") == []
    assert (
        is_email("hello@example.com")
        and not is_email("hello@example")
        and not is_email("a b@example.com")
    )


@pytest.mark.parametrize(
    "text",
    [
        "https://evil.ru/x", "evil.ru/x", "evil.info", "visit evil.xyz now",
        "еxample.com/case-studies",  # Cyrillic а
        "https://еxample.com/case-studies",
        "evil[.]com", "evil(.)com/x", "evil。com", "evil\u3002com/x", "ｅｖｉｌ．ｃｏｍ",
    ],
)  # fmt: skip
def test_schemeless_lookalike_and_defanged_urls_are_rejected(text):
    assert CFG.foreign_links(text)


def test_fullwidth_and_defanged_approved_link_is_still_checked_after_nfkc():
    assert CFG.foreign_links("https://calendly.com/jordan-example/30min") == []
    assert CFG.foreign_links("https://calendly[.]com/jordan-example/30min") == []


def test_email_never_truncated_next_to_non_ascii():
    assert find_emails("write to joão@example.com") == []
    assert find_emails("write to hеllo@example.com") == []  # Cyrillic е
    assert find_emails("hello@example.comé") == []
    assert find_emails("hello@example.com.") == ["hello@example.com"]
    assert find_emails("«hello@example.com»") == ["hello@example.com"]
    assert not is_email("o@example.com\u00e3") and not is_email("joão@example.com")
