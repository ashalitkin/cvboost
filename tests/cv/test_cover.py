"""Cover letter (#32): generated with the kit, validated like the resume, or made on demand."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from cvboost.cv import kit as K
from cvboost.cv.schema import Claim, CoverDraft, CvDraft
from cvboost.cv.validate import mixed_tools, validate, validate_cover
from cvboost.llm.prompts import cv_v1 as P
from cvboost.llm.prompts.registry import current
from tests.cv.helpers import (
    GAPS,
    PROFILE,
    FakeRenderer,
    claims,
    cv_cfg,
    draft_dict,
    make_settings,
    ok_draft,
    queue_request,
)
from tests.cv.test_step import cv_calls, req_row, run
from tests.llm.helpers import FakeClient

LETTER = """\
Jordan Example, PhD
Berlin, Germany · jordan@example.com · linkedin.com/in/jordan-example

Dear hiring team,

I am applying for the Senior Analytics Engineer role at Example Co.

A few examples:

- For a logistics SaaS company I built self-service analytics for 60+ customer teams.
- For a US agency I saved 12+ hours/week of manual reporting.

To be open about gaps: I have not used HubSpot. I would focus on it first.

I typically work B2B via my own company, but I am open to other engagement models, including
employment through an EOR. I overlap with US Pacific 9:00 AM - 12:00 PM PT.

Best regards,<br>
Jordan Example
"""


def cover_dict(**over) -> dict:
    d = {
        "cover_letter_md": LETTER,
        "cover_claims": claims()[:2],
        "injection_suspected": False,
    }
    d.update(over)
    return d


def cvc(md: str, cl=None):
    return validate_cover(
        md, [Claim(**c) for c in claims()[:2]] if cl is None else cl, PROFILE, GAPS
    )


def codes(vs):
    return [v.reason for v in vs]


# --- validator ---


def test_clean_letter_passes():
    assert cvc(LETTER) == []


def test_letter_naming_a_gap_from_gaps_yaml_is_rejected_with_retry():
    vs = cvc(LETTER.replace("HubSpot", "Power BI"))
    assert codes(vs) == ["cover_gap_term:power_bi"] and vs[0].kind == "retry"


def test_review_gap_in_letter_needs_the_owner():
    vs = cvc(LETTER + "\nI have done HIPAA compliant reporting.\n")
    assert [v.kind for v in vs] == ["review"]


def test_invented_number_in_letter():
    vs = cvc(LETTER.replace("60+", "333"))
    assert "cover_unsupported_number:333" in codes(vs)


def test_claim_with_a_made_up_quote_and_too_few_claims():
    bad = [Claim(**{**claims()[0], "profile_quote": "never said this"}), Claim(**claims()[1])]
    assert codes(cvc(LETTER, bad)) == ["cover_bad_claim:1"]
    assert codes(cvc(LETTER, [Claim(**claims()[0])])) == ["cover_bad_claim:count"]


def test_foreign_contacts_money_html_emoji_and_length():
    assert "cover_foreign_contact" in codes(cvc(LETTER + "\nWrite to evil@example.org\n"))
    assert "cover_foreign_contact" in codes(cvc(LETTER + "\nhttps://evil.example.net/x\n"))
    assert "cover_hygiene:money" in codes(cvc(LETTER + "\nMy rate is $90/hour.\n"))
    assert "cover_hygiene:raw_html" in codes(cvc(LETTER + "\n<b>x</b>\n"))
    assert "cover_hygiene:emoji" in codes(cvc(LETTER + "\n\U0001f600\n"))
    assert "cover_hygiene:image" in codes(cvc(LETTER + "\n![](photo.jpg)\n"))
    assert "cover_structure:too_long" in codes(cvc(LETTER + "word " * 460))
    assert "cover_structure:headings" in codes(cvc("# Jordan\n\n" + LETTER))


def tools(text: str) -> list[str]:
    return mixed_tools(text, PROFILE)


def test_tool_check_accepts_real_combinations():
    for ok in (
        "I set up GA4 and GTM server-side tracking.",
        "I write SQL and dbt models every day.",
        "I use Claude Code with BigQuery and dbt.",
        "For the agency I used BigQuery, Meltano and Looker Studio.",
    ):
        assert tools(ok) == [], ok
    four = open(Path(__file__).parent / "fixtures_4301_letter.md").read()
    assert tools(four) == []


def test_tool_check_flags_tools_on_the_wrong_case():
    for bad in (
        "For the mortgage fintech I built the Snowflake warehouse with dbt.",
        "At Acme Subscriptions I built the BigQuery warehouse.",
        "For a US logistics SaaS company, I used Looker Enterprise on Snowflake.",
        "For a real-estate developer I built a BigQuery warehouse with dbt.",
    ):
        assert tools(bad), bad
    assert tools("For a real-estate developer I built a warehouse to measure channel ROI.") == []


def test_mixed_tools_is_review_not_retry_so_it_never_costs_a_second_call():
    mixed = LETTER.replace(
        "built self-service", "built the Snowflake warehouse with dbt, self-service"
    )
    mixed = mixed.replace("logistics SaaS company", "mortgage fintech")
    vs = [v for v in cvc(mixed) if v.code == "cover_mixed_tools"]
    assert vs and all(v.kind == "review" for v in vs)


def test_full_draft_validation_covers_the_letter_and_old_drafts_still_pass():
    d = CvDraft.model_validate(draft_dict(cover_letter_md=LETTER, cover_claims=claims()[:2]))
    assert validate(d, PROFILE, GAPS) == []
    bad = CvDraft.model_validate(
        draft_dict(cover_letter_md=LETTER.replace("HubSpot", "Tableau"), cover_claims=claims()[:2])
    )
    assert "cover_gap_term:tableau" in codes(validate(bad, PROFILE, GAPS))
    assert validate(CvDraft.model_validate(draft_dict()), PROFILE, GAPS) == []


def test_schemas_demand_the_letter():
    sections = PROFILE.section_names
    assert {"cover_letter_md", "cover_claims"} <= set(P.json_schema(sections)["required"])
    assert P.cover_json_schema(sections)["properties"]["cover_claims"]["minItems"] == 2
    with pytest.raises(ValidationError):
        CoverDraft.model_validate(cover_dict(cover_claims=[]))
    assert current().cv.version != current().cv.cover_version
    assert "COVER LETTER RULES" in current().cv.build_system_prompt("p", ["s"])
    assert "Known gaps" in current().cv.build_cover_system_prompt("p", ["s"])


# --- full kit: letter written together with the resume ---


def test_kit_gets_a_cover_letter_and_its_pdf(store, tmp_path):
    ((rid, _, cid),) = queue_request(store)
    r = FakeRenderer()
    dd = draft_dict(cover_letter_md=LETTER, cover_claims=claims()[:2])
    st = run(store, FakeClient(ok_draft(dd)), tmp_path, renderer=r)
    assert st.done == 1 and len(cv_calls(store)) == 1
    kit = tmp_path / "data" / "kits" / str(cid)
    assert (kit / "cover_letter.md").read_text() == LETTER
    assert (kit / "cover_letter.pdf").read_bytes().startswith(b"%PDF")
    assert stat.S_IMODE((kit / "cover_letter.md").stat().st_mode) == 0o600
    assert r.letters == [LETTER]
    assert req_row(store, rid)["status"] == "done"


def test_letter_with_a_gap_is_retried_once_in_the_same_flow(store, tmp_path):
    ((rid, _, cid),) = queue_request(store)
    bad = draft_dict(
        cover_letter_md=LETTER.replace("HubSpot", "Power BI"), cover_claims=claims()[:2]
    )
    client = FakeClient(ok_draft(bad))
    st = run(store, client, tmp_path)
    assert st.requeued == 1 and req_row(store, rid)["status"] == "queued"
    assert "forbidden topic" in req_row(store, rid)["feedback"]
    assert not (tmp_path / "data" / "kits" / str(cid) / "cover_letter.md").exists()


def test_regenerating_a_kit_without_a_letter_keeps_the_existing_letter(tmp_path):
    settings = make_settings(tmp_path)
    K.write_kit(settings.kits_dir, 5, {K.RESUME_MD: "a", K.COVER_MD: "letter", K.COVER_PDF: b"p"})
    K.write_kit(settings.kits_dir, 5, {K.RESUME_MD: "b"})
    d = settings.kits_dir / "5"
    assert (d / "cover_letter.md").read_text() == "letter" and (d / "resume.md").read_text() == "b"
    K.write_kit(settings.kits_dir, 5, {K.RESUME_MD: "c", K.COVER_MD: "new"})
    assert (d / "cover_letter.md").read_text() == "new" and not (d / "cover_letter.pdf").exists()
    assert (d / "prev" / "cover_letter.pdf").read_bytes() == b"p"


# --- cover-only request for an existing kit ---


def finished_kit(store, tmp_path):
    ((rid, _, cid),) = queue_request(store)
    run(store, FakeClient(ok_draft()), tmp_path)
    assert req_row(store, rid)["status"] == "done"
    return rid, cid


def test_cover_request_writes_only_the_letter_and_counts_as_one_opus_call(store, tmp_path):
    from tests.conftest import NOW

    rid, cid = finished_kit(store, tmp_path)
    kit = tmp_path / "data" / "kits" / str(cid)
    before = {n: (kit / n).read_bytes() for n in ("resume.md", "resume.pdf", "fit_notes.md")}
    verdict_before = req_row(store, rid)["recommendation"]
    crid = store.request_cover(cid, now=NOW)
    assert crid and store.request_cover(cid, now=NOW) is None  # one at a time
    client = FakeClient(ok_draft(cover_dict()))
    r = FakeRenderer()
    st = run(store, client, tmp_path, renderer=r)
    assert (st.done, st.calls) == (1, 1)
    assert (kit / "cover_letter.md").read_text() == LETTER
    assert (kit / "cover_letter.pdf").read_bytes().startswith(b"%PDF")
    assert {n: (kit / n).read_bytes() for n in before} == before
    assert r.calls == [] and r.letters == [LETTER]
    row = req_row(store, crid)
    assert row["status"] == "done" and row["kind"] == "cover"
    assert row["prompt_version"].startswith("cover-v1-")
    # the CV request, its verdict and "latest CV" are untouched
    assert req_row(store, rid)["recommendation"] == verdict_before
    assert store.latest_cv_request(cid)["id"] == rid
    assert store.latest_cover_request(cid)["id"] == crid
    calls = cv_calls(store)
    assert len(calls) == 2 and calls[1]["model_alias"] == "opus"
    assert client.requests[-1].json_schema["required"][0] == "cover_letter_md"


def test_cover_request_counts_against_the_opus_cap(store, tmp_path):
    from tests.conftest import NOW

    _, cid = finished_kit(store, tmp_path)
    store.request_cover(cid, now=NOW)
    client = FakeClient(ok_draft(cover_dict()))
    st = run(store, client, tmp_path, cfg=cv_cfg(daily_call_caps={"haiku": 60, "opus": 1}))
    assert st.stop_code == "daily_cap" and client.requests == []


def test_cover_request_needs_a_finished_kit(store, tmp_path):
    from tests.conftest import NOW

    ((_, _, cid),) = queue_request(store)
    assert store.request_cover(cid, now=NOW) is None  # CV request still queued


def test_cover_with_a_gap_is_retried_then_goes_to_review(store, tmp_path):
    from tests.conftest import NOW

    _, cid = finished_kit(store, tmp_path)
    crid = store.request_cover(cid, now=NOW)
    bad = cover_dict(cover_letter_md=LETTER.replace("HubSpot", "Tableau"))
    client = FakeClient(ok_draft(bad), ok_draft(bad))
    run(store, client, tmp_path)
    assert req_row(store, crid)["status"] == "queued"
    run(store, client, tmp_path)
    row = req_row(store, crid)
    assert row["status"] == "needs_review" and "cover_gap_term:tableau" in row["review_reasons"]
    assert (tmp_path / "data" / "kits" / str(cid) / "cover_letter.md").is_file()


def test_cover_injection_flag_goes_to_review(store, tmp_path):
    from tests.conftest import NOW

    _, cid = finished_kit(store, tmp_path)
    crid = store.request_cover(cid, now=NOW)
    run(store, FakeClient(ok_draft(cover_dict(injection_suspected=True))), tmp_path)
    assert req_row(store, crid)["status"] == "needs_review"


def test_cover_without_renderer_still_saves_the_text(store, tmp_path):
    from tests.conftest import NOW

    _, cid = finished_kit(store, tmp_path)
    crid = store.request_cover(cid, now=NOW)
    run(
        store, FakeClient(ok_draft(cover_dict())), tmp_path, renderer=FakeRenderer(unavailable=True)
    )
    kit = tmp_path / "data" / "kits" / str(cid)
    assert (kit / "cover_letter.md").is_file() and not (kit / "cover_letter.pdf").exists()
    assert "cover_pdf_unavailable" in req_row(store, crid)["review_reasons"]


def test_ready_letter_sends_a_telegram_note_and_failed_one_says_cover(store, tmp_path):
    from tests.conftest import NOW

    _, cid = finished_kit(store, tmp_path)
    store.conn.execute("DELETE FROM system_alerts")
    crid = store.request_cover(cid, now=NOW)
    run(store, FakeClient(ok_draft(cover_dict())), tmp_path)
    rows = store.conn.execute("SELECT kind, text FROM system_alerts").fetchall()
    assert len(rows) == 1 and rows[0]["text"].startswith("Cover letter ready:")
    store.conn.execute("UPDATE cv_requests SET status='queued', attempts=2 WHERE id=?", (crid,))
    store.conn.execute("DELETE FROM system_alerts")
    run(store, FakeClient(ok_draft(cover_dict())), tmp_path)
    row = req_row(store, crid)
    assert row["status"] == "failed" and row["prompt_version"].startswith("cover-v1-")
    assert (
        store.conn.execute("SELECT text FROM system_alerts")
        .fetchone()[0]
        .startswith("Cover letter failed")
    )


def test_gap_with_cover_allow_is_not_flagged_in_the_letter():
    """Owner 2026-11-24: a letter may say he has only a little Power BI experience (gaps.yaml
    `cover: allow`); the resume check of the same gap stays `forbid`."""
    from cvboost.cv.profile import GapRule, GapsConfig

    allow = GapsConfig(
        gaps=[
            GapRule(
                id="pbi",
                source="Power BI",
                mode="forbid",
                cover="allow",
                patterns=[r"\bpower\s?bi\b"],
            )
        ]
    )
    forbid = GapsConfig(
        gaps=[GapRule(id="pbi", source="Power BI", mode="forbid", patterns=[r"\bpower\s?bi\b"])]
    )
    text = LETTER + "\nI've only worked with Power BI a little.\n"
    claims2 = [Claim(**c) for c in claims()[:2]]
    assert "cover_gap_term:pbi" not in codes(validate_cover(text, claims2, PROFILE, allow))
    assert "cover_gap_term:pbi" in codes(validate_cover(text, claims2, PROFILE, forbid))
