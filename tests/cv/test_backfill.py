from __future__ import annotations

from cvboost.cv.backfill import backfill_notes, parse_fit_notes
from cvboost.cv.step import fit_notes_md
from tests.cv.helpers import draft, queue_request


def test_parse_roundtrips_step_output():
    d = draft(
        fit_notes_ru={
            "matches": ["a"],
            "risks": ["b"],
            "gaps": [],
            "recommendation": "skip",
            "summary": "Два обязательных требования не закрыты.\nШансы низкие.",
        }
    )
    rec, summary = parse_fit_notes(fit_notes_md(d))
    assert rec == "skip" and summary.startswith("Два обязательных") and "## " not in summary


def test_parse_each_recommendation_and_garbage():
    for ru, en in (
        ("откликаться", "apply"),
        ("с оговорками", "apply_with_caveats"),
        ("пропустить", "skip"),
    ):
        assert parse_fit_notes(f"# T\n\nРекомендация: **{ru}**\n\nТекст.\n\n## Риски\n- x\n") == (
            en,
            "Текст.",
        )
    assert parse_fit_notes("nothing here") is None
    assert parse_fit_notes("Рекомендация: **неизвестно**\n\nx\n") is None
    assert parse_fit_notes("Рекомендация: **пропустить**\n\n## Риски\n") is None


def test_backfill_fills_only_empty_and_is_idempotent(store, tmp_path):
    ((r1, _, c1), (r2, _, c2), (r3, _, c3)) = queue_request(store, 3)
    kits = tmp_path / "kits"
    for cid, rid, text in (
        (c1, r1, fit_notes_md(draft())),
        (c2, r2, "garbage"),
    ):
        (kits / str(cid)).mkdir(parents=True)
        (kits / str(cid) / "fit_notes.md").write_text(text)
        store.conn.execute(
            "UPDATE cv_requests SET status='done', kit_dir=? WHERE id=?", (str(cid), rid)
        )
    store.conn.execute("UPDATE cv_requests SET status='done', kit_dir='999' WHERE id=?", (r3,))
    assert backfill_notes(store, kits) == (1, 2)
    row = store.cv_request(r1)
    assert row["recommendation"] == "apply_with_caveats" and row["fit_summary"]
    store.conn.execute("UPDATE cv_requests SET fit_summary='mine' WHERE id=?", (r1,))
    assert backfill_notes(store, kits) == (0, 2)
    assert store.cv_request(r1)["fit_summary"] == "mine"


def test_backfill_refuses_a_path_outside_kits(store, tmp_path):
    ((rid, _, _),) = queue_request(store)
    kits = tmp_path / "kits"
    kits.mkdir()
    store.conn.execute("UPDATE cv_requests SET status='done', kit_dir='../x' WHERE id=?", (rid,))
    assert backfill_notes(store, kits) == (0, 1)


# --- #70b: fit_notes.md in three shapes: old Russian, new English, hybrid ---------------------

OLD_RU = (
    "# Разбор вакансии\n\nРекомендация: **{rec}**\n\n{summary}\n\n"
    "## Что совпало\n- a\n\n## Риски\n- x\n\n## Пробелы\n- нет\n"
)
NEW_EN = (
    "# Job analysis\n\nRecommendation: **{rec}**\n\n{summary}\n\n"
    "## Matches\n- a\n\n## Risks\n- x\n\n## Gaps\n- none\n"
)
HYBRID = (
    "# Job analysis\n\nRecommendation: **{rec}**\n\n{summary}\n\n## Matches\n- a\n\n## Риски\n- x\n"
)

RECS = {
    "apply": ("apply", "откликаться"),
    "apply_with_caveats": ("apply with caveats", "с оговорками"),
    "skip": ("skip", "пропустить"),
}


def test_three_formats_three_recommendations():
    for code, (en, ru) in RECS.items():
        assert parse_fit_notes(OLD_RU.format(rec=ru, summary="Старый текст.")) == (
            code,
            "Старый текст.",
        )
        assert parse_fit_notes(NEW_EN.format(rec=en, summary="New text.")) == (code, "New text.")
        # hybrid: English headings, Russian body; the value is English or Russian
        assert parse_fit_notes(HYBRID.format(rec=en, summary="Тело по-русски.")) == (
            code,
            "Тело по-русски.",
        )
        assert parse_fit_notes(HYBRID.format(rec=ru, summary="Тело по-русски.")) == (
            code,
            "Тело по-русски.",
        )


def test_new_step_output_roundtrips_for_every_recommendation():
    for code in RECS:
        d = draft(
            fit_notes_ru={
                "matches": ["a"],
                "risks": ["b"],
                "gaps": [],
                "recommendation": code,
                "summary": "One.\nTwo.",
            }
        )
        text = fit_notes_md(d)
        assert text.startswith("# Job analysis\n\nRecommendation: **")
        assert parse_fit_notes(text) == (code, "One.\nTwo.")


def test_empty_lists_unknown_recommendation_and_missing_summary():
    empty = draft(
        fit_notes_ru={
            "matches": [],
            "risks": [],
            "gaps": [],
            "recommendation": "skip",
            "summary": "S.",
        }
    )
    text = fit_notes_md(empty)
    assert text.count("- none") == 3 and parse_fit_notes(text) == ("skip", "S.")
    assert parse_fit_notes("Recommendation: **maybe**\n\nx\n") is None
    assert parse_fit_notes("Recommendation: **Apply**\n\nx\n") == ("apply", "x")  # case-insensitive
    assert parse_fit_notes("Recommendation: **skip**\n\n## Risks\n- x\n") is None  # no summary
    assert parse_fit_notes("Recommendation: **skip**\n") is None


def test_crlf_and_huge_input():
    crlf = NEW_EN.format(rec="skip", summary="Text.").replace("\n", "\r\n")
    assert parse_fit_notes(crlf) == ("skip", "Text.")
    body = "x" * 200_000
    got = parse_fit_notes(NEW_EN.format(rec="apply", summary=body))
    assert got is not None and got[0] == "apply" and len(got[1]) == 600


def test_backfill_reads_a_mixed_folder_and_leaves_path_check(store, tmp_path):
    ((r1, _, c1), (r2, _, c2), (r3, _, c3)) = queue_request(store, 3)
    kits = tmp_path / "kits"
    texts = {
        c1: OLD_RU.format(rec="пропустить", summary="Старый."),
        c2: NEW_EN.format(rec="apply with caveats", summary="New."),
        c3: HYBRID.format(rec="apply", summary="Гибрид."),
    }
    for cid, rid in ((c1, r1), (c2, r2), (c3, r3)):
        (kits / str(cid)).mkdir(parents=True)
        (kits / str(cid) / "fit_notes.md").write_text(texts[cid], encoding="utf-8")
        store.conn.execute(
            "UPDATE cv_requests SET status='done', kit_dir=? WHERE id=?", (str(cid), rid)
        )
    assert backfill_notes(store, kits) == (3, 0)
    got = {r: store.cv_request(r)["recommendation"] for r in (r1, r2, r3)}
    assert got == {r1: "skip", r2: "apply_with_caveats", r3: "apply"}
