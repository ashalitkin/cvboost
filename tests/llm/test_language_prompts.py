"""#70: explain_language reaches the prompts only through the fixed LANGUAGE_NAMES."""

from __future__ import annotations

from pathlib import Path

from cvboost.llm.prompts import registry
from cvboost.profile import load_user_profile

FIXTURE = Path(__file__).parent.parent / "fixtures" / "profile"


def ps_for(lang: str):
    prof = load_user_profile(FIXTURE / "profile.yaml").model_copy(update={"explain_language": lang})
    blocks, problems = registry.read_blocks(FIXTURE / "prompts")
    assert problems == []
    return registry.build(prof, blocks, ("analytics",))


def test_english_prompts():
    ps = ps_for("en")
    assert "1-2 sentences in English:" in ps.score.system
    assert "in Russian" not in ps.score.system
    assert "fit_notes_ru (the fit analysis) is written in English" in ps.cv.rules
    assert "in Russian" not in ps.cv.rules


def test_german_prompts():
    ps = ps_for("de")
    assert "1-2 sentences in German:" in ps.score.system
    assert "is written in German" in ps.cv.rules


def test_versions_differ_per_language_and_are_deterministic():
    v = {c: ps_for(c).versions() for c in ("en", "ru", "de")}
    assert len({v[c]["score"] for c in v}) == 3
    assert len({v[c]["cv"] for c in v}) == 3
    assert ps_for("de").versions() == v["de"]
    for c in v:  # unrelated prompts do not move
        assert v[c]["cover"] == v["en"]["cover"] and v[c]["email"] == v["en"]["email"]
        assert v[c]["reply"] == v["en"]["reply"]


def test_field_names_are_unchanged():
    ps = ps_for("de")
    props = ps.score.schema["properties"]["verdicts"]["items"]["properties"]
    assert "summary_ru" in props
    from cvboost.cv.schema import CvDraft

    assert "fit_notes_ru" in CvDraft.model_json_schema()["properties"]


def test_no_block_uses_the_key():
    for f in (Path(__file__).parents[2] / "config" / "prompts").glob("*.md"):
        assert "explain_language" not in f.read_text()
    for f in (FIXTURE / "prompts").glob("*.md"):
        assert "explain_language" not in f.read_text()
