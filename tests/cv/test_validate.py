from __future__ import annotations

import pytest

from cvboost.cv.profile import check_gaps, load_gaps, norm_link, parse_profile
from cvboost.cv.validate import feedback_text, needs_retry, validate
from tests.cv.helpers import GAPS, PROFILE, RESUME_DIR, claims, draft, draft_dict, with_md

# one sentence per gaps.yaml entry that must trigger it
EXAMPLES = {
    "mmm": "Built a marketing mix model with Robyn.",
    "power_bi": "Dashboards in Power BI.",
    "tableau": "Reports in Tableau.",
    "sfmc": "Campaigns in Salesforce Marketing Cloud.",
    "dataform": "Pipelines in Dataform.",
    "airflow": "Orchestration with Apache Airflow DAGs.",
    "data_vault": "Modeled the warehouse with Data Vault 2.0.",
    "governance_platforms": "Rolled out Collibra as the data catalog.",
    "medical_data": "HIPAA compliant reporting.",
    "communities": "Community analytics for Discord.",
    "push_email": "Push notification analytics.",
    "data_scientist": "Experienced data scientist.",
}


def reasons(**over) -> list[str]:
    return [v.reason for v in validate(draft(**over), PROFILE, GAPS)]


def test_clean_reference_draft_passes():
    assert validate(draft(), PROFILE, GAPS) == []


def test_every_gaps_yaml_entry_fires_on_its_example():
    assert set(EXAMPLES) == {g.id for g in GAPS.gaps}
    for g in GAPS.gaps:
        found = {
            v.code + ":" + v.arg: v
            for v in validate(draft(**with_md(EXAMPLES[g.id])), PROFILE, GAPS)
        }
        code = "gap_term" if g.mode == "forbid" else "gap_review"
        assert f"{code}:{g.id}" in found, g.id


def test_gap_in_a_claim_text_is_found_too():
    cl = claims()
    cl[0]["resume_text"] = "Reports in Tableau."
    assert "gap_term:tableau" in reasons(claims=cl)


def test_recruiter_message_may_name_a_gap_honestly():
    msg = {"connection_note": "I have not used Power BI.", "inmail": "No Tableau experience."}
    assert reasons(recruiter_message=msg) == []


def test_every_known_gap_of_the_real_profile_is_covered():
    assert PROFILE.known_gaps and check_gaps(PROFILE, GAPS) == []


def test_check_gaps_reports_an_unguarded_item_and_a_stale_entry():
    text = (RESUME_DIR / "profile.md").read_text() + "- Kubernetes: never used.\n"
    # the new bullet must be inside the Known gaps section: insert before Applications log
    text = (
        (RESUME_DIR / "profile.md")
        .read_text()
        .replace("## Applications log", "- Kubernetes: never used.\n\n## Applications log")
    )
    problems = check_gaps(parse_profile(text), GAPS)
    assert any("Kubernetes" in p for p in problems)
    stale = load_gaps(RESUME_DIR)
    stale.gaps[0].source = "Nothing like this"
    assert any("not in Known gaps" in p for p in check_gaps(PROFILE, stale))


def test_applications_log_is_not_claimable_and_not_in_the_prompt_text():
    assert "Applications log" not in PROFILE.sections
    assert "Applications log" not in PROFILE.prompt_text
    assert "Hooli Staffing" not in PROFILE.prompt_text
    assert not any(s.startswith("Known gaps") for s in PROFILE.sections)


def test_unsupported_number():
    assert "unsupported_number:99" in reasons(**with_md("Cut costs by 99%."))
    assert reasons(**with_md("Reporting in 18 minutes.")) == []  # 18 is a profile number


def test_bad_claims():
    cl = claims()
    cl[0]["profile_quote"] = "Led a team of 500 engineers"  # not in the section
    cl[1]["section"] = "No such section"
    cl[2]["section"] = "Education"  # real section, but the quote is from another one
    assert reasons(claims=cl) == ["bad_claim:1", "bad_claim:2", "bad_claim:3"]


def test_claim_quote_matching_ignores_case_markdown_and_spacing():
    cl = claims()
    cl[0]["profile_quote"] = "12+  HOURS/week   of manual reporting saved"
    assert reasons(claims=cl) == []


def test_structure():
    md = draft_dict()["resume_md"]
    assert "structure:h1" in reasons(resume_md=md.replace("# Jordan Example, PhD", "# Someone"))
    assert "structure:education" in reasons(resume_md=md.replace("## Education", "## Studies"))
    assert "structure:experience" in reasons(resume_md=md.replace("## Experience", "## Work"))


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        ("Great work \U0001f680", "hygiene:emoji"),
        ("![x](https://evil.example/p.png)", "hygiene:foreign_image"),
        ("![x](other.jpg)", "hygiene:foreign_image"),
        ("[site](http://example.com)", "hygiene:non_https_link"),
        ("<script>alert(1)</script>", "hygiene:raw_html"),
    ],
)
def test_hygiene(extra, code):
    assert code in reasons(**with_md(extra))


def test_https_and_mailto_links_are_fine():
    assert reasons(**with_md("[a](https://example.com) [b](mailto:jordan@example.com)")) == []


def test_money_in_recruiter_message():
    msg = {"connection_note": "Rate is $80/hour.", "inmail": "ok"}
    assert "hygiene:money_in_message" in reasons(recruiter_message=msg)
    msg = {"connection_note": "ok", "inmail": "My rate: 90 EUR per hour."}
    assert "hygiene:money_in_message" in reasons(recruiter_message=msg)


def test_retry_classes():
    v = validate(draft(**with_md("Dashboards in Power BI.")), PROFILE, GAPS)
    assert needs_retry(v)
    mixed = validate(draft(**with_md("Power BI and HIPAA.")), PROFILE, GAPS)
    assert not needs_retry(mixed)
    assert not needs_retry(validate(draft(injection_suspected=True), PROFILE, GAPS))
    assert not needs_retry([])


def test_feedback_uses_only_codes_names_and_digits():
    md = with_md("Dashboards in Power BI, saved 99 hours, see http://evil.example.")["resume_md"]
    v = validate(draft(resume_md=md), PROFILE, GAPS)
    text = feedback_text(v, GAPS)
    assert "Power BI" in text and "99" in text  # the gap name comes from gaps.yaml, 99 from digits
    assert "evil" not in text and "saved" not in text and len(text) <= 1900


def test_money_in_the_resume_is_a_violation():
    assert "money_in_resume" in reasons(**with_md("Contract rate $65/hr."))
    assert "money_in_resume" in reasons(**with_md("Rate 90 EUR per hour."))


def test_numbers_come_only_from_the_fact_sections():
    # 65 and 85 appear only in the Applications log (other jobs' rates)
    assert "65" not in PROFILE.numbers and "85" not in PROFILE.numbers
    assert "unsupported_number:65" in reasons(**with_md("Delivered 65 dashboards."))
    assert "unsupported_number:77" in reasons(**with_md("Delivered 77 dashboards."))


@pytest.mark.parametrize(
    "extra",
    [
        "Write me at evil@attacker.example.net.",
        "Portfolio: https://attacker.example/cv",
        "See attacker.com for more.",
        "[more](https://attacker.example)",
        "[mail](mailto:evil@attacker.example.net)",
        "[x](https://localhost/steal)",
        "Also sales@example.com.",
        "linkedin.com/in/someone-else",
    ],
)
def test_foreign_contacts_in_the_resume_need_review(extra):
    v = validate(draft(**with_md(extra)), PROFILE, GAPS)
    hit = [x for x in v if x.code == "foreign_contact"]
    assert hit and all(x.kind == "review" for x in hit)
    assert not needs_retry(v)  # never auto-retried into "done"


@pytest.mark.parametrize("field", ["connection_note", "inmail"])
def test_foreign_contacts_in_the_recruiter_message_need_review(field):
    msg = {"connection_note": "Hi.", "inmail": "Hello."}
    msg[field] = "Reply to evil@attacker.example.net or https://attacker.example/x please."
    v = validate(draft(recruiter_message=msg), PROFILE, GAPS)
    assert [(x.code, x.kind) for x in v] == [("foreign_contact", "review")]


def test_own_contacts_pass():
    ok = "example.com, www.linkedin.com/in/jordan-example, github.com/jordan-example/sample-project"
    assert reasons(**with_md(ok)) == []
    msg = {"connection_note": "See https://example.com", "inmail": "jordan@example.com"}
    assert reasons(recruiter_message=msg) == []
    assert PROFILE.contacts.emails == {"jordan@example.com"}
    assert {"example.com", "linkedin.com/in/jordan-example", "github.com/jordan-example"} <= (
        PROFILE.contacts.links
    )


def test_the_draft_strings_are_normalised_before_any_scan():
    # fullwidth letters and zero-width characters must not hide a forbidden term
    sneaky = "Dashboards in \uff30ower B\u200bI."
    d = draft(**with_md(sneaky))
    assert "Power BI" in d.resume_md and "\u200b" not in d.resume_md
    assert "gap_term:power_bi" in [v.reason for v in validate(d, PROFILE, GAPS)]
    d2 = draft(
        recruiter_message={"connection_note": "a\u202eb", "inmail": "x"},
        fit_notes_ru={**draft_dict()["fit_notes_ru"], "summary": "ok\u200b"},
    )
    assert d2.recruiter_message.connection_note == "ab" and d2.fit_notes_ru.summary == "ok"


def test_published_article_urls_are_allowed_exactly():
    urls = sorted(PROFILE.contacts.exact)
    assert any(u.startswith("medium.com/data-science/") for u in urls)
    for u in urls:
        assert reasons(**with_md(f"Article: https://{u}")) == [], u
    assert (
        reasons(
            **with_md(
                "[Warehouse patterns that scale]"
                "(https://medium.com/data-science/warehouse-patterns-that-scale-0a1b2c3d4e5f)"
            )
        )
        == []
    )


@pytest.mark.parametrize(
    "extra",
    [
        "https://medium.com/@someone-else/their-article-123",
        "https://medium.com",
        "medium.com/@jordan.example_12345/other-post",
        "https://medium.com/data-science/warehouse-patterns-that-scale-0a1b2c3d4e5f/extra",
        "https://medium.com/data-science/another-article-ffff",
    ],
)
def test_other_medium_urls_are_foreign(extra):
    assert "foreign_contact:resume" in reasons(**with_md(extra))


def test_article_urls_do_not_open_the_recruiter_message_to_other_addresses():
    msg = {"connection_note": "Hi", "inmail": "Write to medium@attacker.example.net"}
    assert "foreign_contact:inmail" in reasons(recruiter_message=msg)


@pytest.mark.parametrize(
    "extra",
    [
        "medium.com/@jordan.example_12345?ref=x",
        "https://medium.com/@jordan.example_12345#top",
        "https://medium.com/data-science/x@evil.example.net",
        "https://user@medium.com/@jordan.example_12345",
        "https://medium.com:8443/@jordan.example_12345",
        "http://medium.com/@jordan.example_12345",
        "https://github.com/jordan-example/repo?tab=x",
        "https://example.com/anything",
    ],
)
def test_urls_must_match_whole_and_clean(extra):
    assert "foreign_contact:resume" in reasons(**with_md(extra))
    msg = {"connection_note": "Hi", "inmail": extra}
    assert any(r.startswith("foreign_contact") for r in reasons(recruiter_message=msg))


def test_href_targets_are_held_to_the_same_rule():
    bad = "[me](https://medium.com/@jordan.example_12345?ref=x)"
    assert "foreign_contact:href" in reasons(**with_md(bad))
    good = "[me](https://medium.com/@jordan.example_12345/)"  # trailing slash is dropped
    assert reasons(**with_md(good)) == []


DOTDOT = [
    "https://github.com/jordan-example/../../evil/repo",
    "https://github.com/jordan-example/%2e%2e/evil",
    "https://github.com/jordan-example/%2E%2E/evil",
    "https://github.com/jordan-example/.%2e/evil",
    "https://github.com/jordan-example/./repo",
    "https://github.com/jordan-example/a\\..\\evil",
    "https://linkedin.com/in/jordan-example/../../company/evil",
    "ftp://example.com",
    "https://github.com/jordan-example/..",
    "https://github.com/jordan-example/%2e%2e",
    "https://linkedin.com/in/jordan-example/..",
]


@pytest.mark.parametrize("url", DOTDOT)
def test_dot_segments_and_foreign_schemes_are_foreign(url):
    assert "foreign_contact:resume" in reasons(**with_md(url))
    msg = {"connection_note": "Hi", "inmail": url}
    assert "foreign_contact:inmail" in reasons(recruiter_message=msg)
    assert any(r.startswith("foreign_contact") for r in reasons(**with_md(f"[x]({url})")))


@pytest.mark.parametrize("url", [u for u in DOTDOT if u.startswith("https://") and "\\" not in u])
def test_dot_segments_in_a_markdown_href(url):
    assert "foreign_contact:href" in reasons(**with_md(f"[x]({url})"))


def test_markdown_link_with_dotdot_is_foreign():
    bad = "[x](https://github.com/jordan-example/../evil)"
    assert "foreign_contact:href" in reasons(**with_md(bad))
    assert "foreign_contact:resume" in reasons(**with_md(bad))
    msg = {"connection_note": "Hi", "inmail": bad}
    assert "foreign_contact:inmail" in reasons(recruiter_message=msg)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/jordan-example",
        "https://github.com/jordan-example/sample-project",
        "https://github.com/jordan-example/my.repo/",
        "https://www.linkedin.com/in/jordan-example",
        "https://example.com",
    ],
)
def test_own_links_still_pass_after_dot_segment_rule(url):
    assert reasons(**with_md(url)) == []
    assert reasons(**with_md(f"[x]({url})")) == []
    assert reasons(recruiter_message={"connection_note": "Hi", "inmail": url}) == []


def test_trailing_sentence_punctuation_is_still_dropped():
    assert reasons(**with_md("See github.com/jordan-example/sample-project.")) == []
    assert reasons(**with_md("Site: example.com.")) == []


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/jordan-example/%252e%252e/evil",  # double-encoded: a literal segment
        "https://github.com/jordan-example/..;/evil",  # ";" makes it a literal segment for browsers
    ],
)
def test_literal_look_alikes_stay_below_the_owner_prefix(url):
    # facts: browsers do not collapse these, so they stay inside github.com/jordan-example/
    key = norm_link(url)
    assert not key.startswith("!") and key.startswith("github.com/jordan-example/")
    assert key not in PROFILE.contacts.links
    assert not any(r.startswith("foreign_contact") for r in reasons(**with_md(url)))
