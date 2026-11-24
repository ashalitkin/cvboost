from __future__ import annotations

import re

import pytest

from cvboost.notify import format as F


def view(**kw):
    d = dict(
        title="Senior Analytics Engineer",
        company="Acme",
        rate=None,
        location="Europe (Remote)",
        fit=82,
        profile="data_engineer",
        employment="contract",
        seniority="senior",
        summary="Контракт, удалённо.",
        warnings=["rate_unknown"],
        links=[("wwr", "https://weworkremotely.com/remote-jobs/x")],
    )
    d.update(kw)
    return F.AlertView(**d)


def test_alert_layout():
    text = F.render_alert(view())
    assert text.splitlines() == [
        "Senior Analytics Engineer — Acme",
        "Rate: not stated",
        "Location: Europe (Remote)",
        "Fit 82 · data_engineer · contract · senior",
        "Summary: Контракт, удалённо.",
        "Warnings: rate_unknown",
        "Sources:",
        "wwr: https://weworkremotely.com/remote-jobs/x",
    ]
    assert "Warnings" not in F.render_alert(view(warnings=[]))
    assert "Rate: $70–90/ч" in F.render_alert(view(rate="$70–90/ч"))


def test_untrusted_text_loses_links_mentions_and_invisible_characters():
    evil = (
        "Click https://evil.example.com now, @someuser or evil.ru/x or a@example.com "
        "\u202eRLO\u200b <b>x</b> *bold*"
    )
    t = F.render_alert(view(summary=evil, title=evil, company=evil, location=evil, warnings=[evil]))
    own = "https://weworkremotely.com/remote-jobs/x"
    assert t.replace(own, "").count("http") == 0
    for bad in ("someuser", "a@example.com", "\u202e", "\u200b", "evil.ru", "evil.example"):
        assert bad not in t, bad
    assert "evil\u2024ru/x" in t  # the domain stays readable, but is not a link
    assert "<b>x</b>" in t and "*bold*" in t  # no parse_mode: markup stays inert text


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ASP.NET developer", "ASP\u2024NET developer"),
        ("Monday.com", "Monday\u2024com"),
        ("example.ch", "example\u2024ch"),
        ("пример.рф", "пример\u2024рф"),
        ("xn--e1afmkfd.xn--p1ai", "xn--e1afmkfd\u2024xn--p1ai"),
        ("1.2.3.4", "1\u20242\u20243\u20244"),
        ("a.b.c.d.example.co.uk", "a\u2024b\u2024c\u2024d\u2024example\u2024co\u2024uk"),
        ("Node.js, Vue.js", "Node\u2024js, Vue\u2024js"),
        ("123.com", "123\u2024com"),
        ("example\u3002com", "example\u2024com"),
        ("example\uff0ecom", "example\u2024com"),
        ("example\uff61com", "example\u2024com"),
        ("1\u30022\uff0e3\uff614", "1\u20242\u20243\u20244"),
    ],
)
def test_dots_between_labels_are_replaced_by_a_dot_leader(text, expected):
    out = F.safe_text(text, 200)
    assert out == expected and "." not in out


@pytest.mark.parametrize(
    "text",
    ["3.5 years", "€80.000", "v2.0", "up to 1.5x", "2.4.1", "Great team. Apply now.", "3\uff0e5"],
)
def test_numbers_and_sentences_are_left_alone(text):
    assert F.safe_text(text, 100) == text


def test_sentence_dots_stay_and_no_domain_survives_in_a_whole_card():
    assert F.safe_text("Great team. Apply now.", 100) == "Great team. Apply now."
    t = F.render_alert(view(summary="see evil.co and 10.0.0.1/admin", title="x.io"))
    assert not re.search(r"\w\.\w", t.replace("weworkremotely.com/remote-jobs/x", ""))


def test_links_only_https_and_at_most_three():
    v = view(
        links=[
            ("wwr", "http://weworkremotely.com/1"),
            ("wwr", "https://weworkremotely.com/2 evil"),
            *[("wwr", f"https://weworkremotely.com/{i}") for i in range(5)],
        ]
    )
    t = F.render_alert(v)
    assert "http://" not in t and "evil" not in t
    assert t.count("https://") == 3


@pytest.mark.parametrize(
    "source,url,ok",
    [
        ("wwr", "https://weworkremotely.com/remote-jobs/x", True),
        ("wwr", "https://www.weworkremotely.com/remote-jobs/x", True),  # subdomain
        ("wwr", "https://evil.example.com/x", False),
        ("wwr", "https://weworkremotely.com.evil.example/x", False),
        ("wwr", "https://xweworkremotely.com/x", False),
        ("himalayas", "https://himalayas.app/companies/a/jobs/b", True),
        ("himalayas", "https://weworkremotely.com/x", False),  # another source's host
        ("hn_hiring", "https://news.ycombinator.com/item?id=1", True),
        ("hn_hiring", "https://hn.algolia.com/x", False),
        ("fake", "https://example.com/x", False),  # unknown source
        ("wwr", "https://weworkremotely.com/\u0440\u0443", False),  # non-ASCII
        ("wwr", "https://weworkremotely.com/a\u202eb", False),  # bidi override
        ("wwr", "https://weworkremotely.com:8443/x", False),  # odd port
        ("wwr", "https://user@weworkremotely.com/x", False),
        ("wwr", "http://weworkremotely.com/x", False),
    ],
)
def test_link_host_must_belong_to_the_source(source, url, ok):
    assert (F.https_link(source, url) == url) is ok


def test_length_is_capped():
    big = "я" * 5000
    t = F.render_alert(
        view(
            title=big,
            company=big,
            location=big,
            summary=big,
            warnings=[big] * 9,
            links=[("s", "https://e.example.com/" + "p" * 470)] * 5,
        )
    )
    assert len(t) <= F.MAX_MESSAGE
    s = F.render_summary([(70, big, big)] * 40, 15)
    assert len(s) <= F.MAX_MESSAGE and s.count("\n") == 16 and "and 25 more in the panel" in s


@pytest.mark.parametrize(
    "state", ["initial", "reasons", "interested", "rejected:stack", "cv_queued"]
)
@pytest.mark.parametrize("alert_id", [1, 123456, 10**10 - 1])
def test_callback_data_fits_64_bytes_and_parses(state, alert_id):
    for row in F.keyboard(alert_id, state)["inline_keyboard"]:
        for b in row:
            assert 1 <= len(b["callback_data"].encode()) <= 64
            m = F.CALLBACK_RE.match(b["callback_data"])
            assert m and int(m.group(2)) == alert_id


def test_unknown_keyboard_state():
    with pytest.raises(ValueError):
        F.keyboard(1, "nope")


def test_safe_text_basics():
    assert F.safe_text("x" * 50, 10) == "x" * 9 + "…"
    assert F.safe_text(None, 10) == ""


@pytest.mark.parametrize(
    "evil",
    [
        "a" * 200_000,
        "a." * 100_000,
        "a@" * 100_000,
        "-." * 100_000,
        "http" * 50_000,
        "@" + "a" * 200_000,
        "x.com/" * 40_000,
        "a.b" * 60_000,
    ],
)
def test_safe_text_is_fast_on_hostile_input(evil):
    import time

    t0 = time.monotonic()
    F.safe_text(evil, 400)
    assert time.monotonic() - t0 < 0.5


# --- #70b: English chrome around untrusted data ---


def test_link_placeholder_and_rate_are_english():
    assert F.LINK == "[link]"
    assert F.safe_text("see https://evil.example/x now", 80) == "see [link] now"
    assert F.safe_text("mail a@example.com", 80) == "mail [link]"
    text = F.render_alert(view(rate="$70–90/h", location=None))
    assert "Rate: $70–90/h" in text and "Location: —" in text
    assert F.render_alert(view(rate="?")).splitlines()[1] == "Rate: not stated"


def test_summary_header_and_overflow_line_are_english():
    s = F.render_summary([(70, f"T{i}", "C") for i in range(20)], 15)
    lines = s.splitlines()
    assert lines[0] == "20 more matches (no buttons, see the panel):"
    assert lines[1] == "70 · T0 — C"
    assert lines[-1] == "and 5 more in the panel"


def test_reason_labels_and_system_texts_have_no_cyrillic():
    import re

    from cvboost.notify import alerts as A

    cyr = re.compile("[Ѐ-ӿ]")
    assert F.REASON_LABEL == {
        "stack": "Stack",
        "rate": "Rate",
        "location": "Location",
        "other": "Other",
    }
    texts = [
        A.cv_ready_text("T", "C", "apply_with_caveats", 5, None),
        A.cv_ready_text("T", "C", None, 5, "https://panel.example.com/"),
        A.cv_review_text("T", "C", [], 5, None),
        A.cv_failed_text("T", "C", "boom"),
        A.cover_ready_text("T", "C", 5, None),
        A.cover_review_text("T", "C", ["x"], 5, None),
        A.cover_failed_text("T", "C", "boom"),
        A.cv_config_text("bad"),
        A.cv_stopped_text("wait"),
    ]
    assert texts[0] == "CV ready: T — C. Recommendation: apply with caveats. Panel → job #5"
    assert texts[1].endswith("see the analysis in the kit. https://panel.example.com/jobs/5")
    assert texts[2] == "CV needs review: T — C. Reasons: needs review. Panel → job #5"
    assert texts[3] == "CV failed: T — C. boom"
    for t in texts:
        assert cyr.search(t) is None, t
