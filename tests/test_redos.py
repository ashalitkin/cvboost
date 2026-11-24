from __future__ import annotations

import time

import pytest

from cvboost import companies
from cvboost.connectors import ats, hn_jobs, mastodon, realworkfromanywhere, remotefirstjobs
from cvboost.core import geo, rules, salary
from cvboost.core.geo import classify_location
from cvboost.core.language import detect_language
from tests.rules_helpers import COMPILED, ev

N = 30000
NBSP = " "
INPUTS = {
    "spaces": " " * N,
    "nbsp": NBSP * N,
    "dollar-dash-spaces": "$5 -" + " " * N,
    "dollar-spaces-x": "$5" + " " * N + "x",
    "one-spaces": "1" + " " * N,
    "one-space-pairs": "1 " * 15000,
    "dollar-one-pairs": "$1 " * 10000,
    "nbsp-dollar": "$5" + NBSP * N + "per hour",
    "digits": "9" * N,
    "w2": "W2" + " " * N + "only",
    "hybrid": "is a" + " " * N + "hybrid",
    "money": "$" + "1," * 15000 + " per",
    "pay": "rate " + "$1 " * 10000,
    "paren": "(" + " " * N + "preferred",
    "word-then-dashes": "data" + "-" * N + "x",
    "alnum": "a1" * 15000,
    "arch-bi": "architect " * 3000 + "bi " * 3000,
    "tracking-dash": "tracking-" * 5000 + "data " * 3000,
    "server-side": "server-side " * 3000,
    "tag-spaces": "tag" + " " * N + "management",
    "machine": "machine " * 5000,
    "machine-dash": "machine-" * 5000,
    "dashes-space": "business - " * 5000,
    "business": "business " * 5000,
    "url-slug": "https://jobs.lever.co/" + "a-" * 15000,
    "ir35": "outside " + " " * N + "IR35",
    "uk-resident": "non-UK " + "x" * N,
    "emea": "remote " + "a " * 15000 + "emea",
    "hq": "(" + "x " * 15000 + "hq",
    "is-hiring": "a is " * 10000,
    "yc-paren": "x (YC " + "a" * N,
    "loc-lines": "Location:" + " \n" * 15000,
    "looking": "i'm looking for " * 3000 + "a " * 10000,
}
REGEXES = {
    "salary._DESC_RE": salary._DESC_RE,
    "salary._AMT_RE": salary._AMT_RE,
    "rules._TITLE_TAIL": rules._TITLE_TAIL,
    "rules._AUTH": rules._AUTH,
    "rules._RESIDE": rules._RESIDE,
    "rules._ONLY": rules._ONLY,
    "rules._CITIZEN": rules._CITIZEN,
    "rules._W2": rules._W2,
    "rules._ONSITE_SHORT": rules._ONSITE_SHORT,
    "rules._ONSITE_DESC": rules._ONSITE_DESC,
    "rules._TZ": rules._TZ,
    "rules._PART_TIME": rules._PART_TIME,
    "rules._MONEY": rules._MONEY,
    "rules._NEGATED": rules._NEGATED,
    "rules._OR_REMOTE": rules._OR_REMOTE,
    "rules._BROAD": rules._BROAD,
    "rules._HYB_BEFORE": rules._HYB_BEFORE,
    "rules._HYB_AFTER": rules._HYB_AFTER,
    "rules._UK_STRONG": rules._UK_STRONG,
    "rules._UK_BAD": rules._UK_BAD,
    "rules._UK_WEAK": rules._UK_WEAK,
    "rules._UK_WEAK_SHORT": rules._UK_WEAK_SHORT,
    "geo._INCL": geo._INCL,
    "rules._ANALYTICS_TITLE": rules._ANALYTICS_TITLE,  # #28
    "rules.unwanted": COMPILED.unwanted,  # #28 (performance marketer, ...)
    "relevance.negative": COMPILED.relevance.negative,  # #28
    **{
        f"relevance.{p.name}.title{i}": rx
        for p in COMPILED.relevance.profiles
        for i, (_, rx, _neg) in enumerate(p.title_groups)
    },
    "ats.TITLE_RE": ats.TITLE_RE,
    "ats.rules_title_re": ats.build_title_re(
        ats.profile_title_terms("tests/fixtures/config/rules.yaml")
    ),
    "companies._PAGE_URL": companies._PAGE_URL,
    "rwfa._ENCLOSURE": realworkfromanywhere._ENCLOSURE,
    "hn_jobs._HIRING": hn_jobs._HIRING,
    "hn_jobs._YC_SLUG": hn_jobs._YC_SLUG,
    "mastodon.JOB_WORDS": mastodon.JOB_WORDS,
    "mastodon.SEEKER": mastodon.SEEKER,
    "mastodon._HASHTAG": mastodon._HASHTAG,
    "mastodon._URL": mastodon._URL,
    "mastodon._ID": mastodon._ID,
    "remotefirstjobs._LOCATION": remotefirstjobs._LOCATION,
    "remotefirstjobs._TYPE": remotefirstjobs._TYPE,
    "companies.SLUG_RE": companies.SLUG_RE,  # also the sub-domain label of #24 platforms
}
LIMIT = 0.5


@pytest.mark.parametrize("name", INPUTS)
@pytest.mark.parametrize("rx_name", REGEXES)
def test_each_regex_is_fast(rx_name, name):
    rx = REGEXES[rx_name]
    best = float("inf")
    for _ in range(3):  # min of 3 runs: a loaded machine must not fail a linear regex
        t = time.perf_counter()
        list(rx.finditer(INPUTS[name]))
        best = min(best, time.perf_counter() - t)
        if best < LIMIT:
            break
    assert best < LIMIT


@pytest.mark.parametrize("name", INPUTS)
def test_evaluate_is_fast_on_every_field(name):
    text = INPUTS[name]
    t = time.perf_counter()
    ev(description_text=text, title=text, location_text=text, salary_text=text)
    ev(description_text=text + " authorized to work in " + text)
    assert time.perf_counter() - t < LIMIT


@pytest.mark.parametrize("name", INPUTS)
def test_geo_and_language_are_fast(name):
    t = time.perf_counter()
    classify_location(INPUTS[name], ("worldwide",))
    detect_language(INPUTS[name], ("en",), 30)
    assert time.perf_counter() - t < LIMIT


def test_fold_collapses_nbsp_and_keeps_single_newlines():
    assert rules.fold("a   b\n \n\n c") == "a b\nc"


@pytest.mark.parametrize("name", INPUTS)
def test_the_platform_connectors_are_fast_on_hostile_titles_and_slugs(name):
    """#24 added no regex of its own: titles go through TITLE_RE (above), slugs through SLUG_RE;
    the XML and JSON parsers get the same hostile text in every field."""
    from datetime import UTC, datetime

    from cvboost.connectors import REGISTRY
    from cvboost.connectors.ats import ATSConnector

    text = INPUTS[name].replace("&", "").replace("<", "")
    t = time.perf_counter()
    for cls in REGISTRY.values():
        if not issubclass(cls, ATSConnector) or cls.name in ("greenhouse", "lever", "ashby"):
            continue
        c = cls()
        with pytest.raises(ValueError):
            c.url_for(text[:70] + ".")
        title = ats.title_matches(text, c._title_re)
        assert isinstance(title, bool)
    xml = (
        f"<workzag-jobs><position><id>1</id><name>{text}</name><office>{text}</office>"
        f"<createdAt>{text}</createdAt></position></workzag-jobs>"
    ).encode()
    from cvboost.connectors.personio import PersonioConnector

    c = PersonioConnector()
    [raw] = c.parse_all(xml, c.url_for("acme"))
    try:
        c.normalise(raw, datetime.now(UTC))
    except ValueError:
        pass  # a title of only spaces is refused: that is fine, it must just be quick
    assert time.perf_counter() - t < LIMIT * 4
