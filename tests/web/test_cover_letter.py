from __future__ import annotations

from cvboost.cv import kit as K
from tests.cv.helpers import FakeRenderer
from tests.web.conftest import Ids, db
from tests.web.test_kit_edit import make, post, seed_kit

LETTER = "Jordan Example, PhD\nBerlin\n\nDear team,\n\nHello <script>x</script>\n"


def with_cover(settings, cid, **kw):
    req = seed_kit(settings, cid, **kw)
    K.write_one(settings.kits_dir, cid, K.COVER_MD, LETTER)
    K.write_one(settings.kits_dir, cid, K.COVER_PDF, b"%PDF-old-letter")
    return req


def test_block_shows_the_letter_escaped_and_links(settings, seeded: Ids):
    c = make(settings)
    with_cover(settings, seeded.b)
    page = c.get(f"/jobs/{seeded.b}").text
    assert 'id="cover"' in page and "Cover letter" in page
    assert "&lt;script&gt;x&lt;/script&gt;" in page and "<script>x" not in page
    assert f"/jobs/{seeded.b}/kit/cover_letter.pdf" in page
    assert "Make a cover letter" not in page


def test_files_are_served_as_attachments(settings, seeded: Ids):
    c = make(settings)
    with_cover(settings, seeded.b)
    pdf = c.get(f"/jobs/{seeded.b}/kit/cover_letter.pdf")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert 'filename="cover_letter.pdf"' in pdf.headers["content-disposition"]
    md = c.get(f"/jobs/{seeded.b}/kit/cover_letter.md")
    assert md.status_code == 200 and "Dear team" in md.text
    assert c.get(f"/jobs/{seeded.b}/kit/cover_letter.txt").status_code == 404


def test_edit_saves_markdown_and_rebuilds_pdf(settings, seeded: Ids):
    r = FakeRenderer()
    c = make(settings, r)
    req = with_cover(settings, seeded.b)
    resp = post(c, f"/jobs/{seeded.b}/kit/cover", cover_md="New\r\ntext", request_id=str(req))
    assert resp.status_code == 303 and resp.headers["location"].endswith("#cover")
    d = settings.kits_dir / str(seeded.b)
    assert (d / "cover_letter.md").read_text() == "New\ntext"
    assert (d / "cover_letter.pdf").read_bytes() == b"%PDF-1.4 fake letter"
    assert r.letters == ["New\ntext"] and r.calls == []
    assert (d / "resume.md").read_text() == "# CV\n"


def test_pdf_button_rebuilds_from_the_saved_text(settings, seeded: Ids):
    r = FakeRenderer()
    c = make(settings, r)
    req = with_cover(settings, seeded.b)
    assert post(c, f"/jobs/{seeded.b}/kit/cover/pdf", request_id=str(req)).status_code == 303
    assert r.letters == [LETTER]


def test_renderer_down_retires_the_stale_pdf(settings, seeded: Ids):
    c = make(settings, FakeRenderer(unavailable=True))
    req = with_cover(settings, seeded.b)
    assert (
        post(c, f"/jobs/{seeded.b}/kit/cover", cover_md="x", request_id=str(req)).status_code == 303
    )
    assert c.get(f"/jobs/{seeded.b}/kit/cover_letter.pdf").status_code == 404
    assert "The letter PDF is not built" in c.get(f"/jobs/{seeded.b}").text


def test_edit_needs_csrf_valid_request_id_size_and_lock(settings, seeded: Ids):
    r = FakeRenderer()
    c = make(settings, r)
    req = with_cover(settings, seeded.b)
    c.get("/inbox")
    path = f"/jobs/{seeded.b}/kit/cover"
    assert c.post(path, data={"cover_md": "evil", "request_id": str(req)}).status_code == 403
    assert post(c, path, cover_md="x", request_id="999").status_code == 409
    assert post(c, path, cover_md="  ", request_id=str(req)).status_code == 400
    assert post(c, path, cover_md="ж" * 6001, request_id=str(req)).status_code == 400
    with K.kit_lock(settings.kits_dir, seeded.b):
        assert post(c, path, cover_md="x", request_id=str(req)).status_code == 409
    assert (settings.kits_dir / str(seeded.b) / "cover_letter.md").read_text() == LETTER
    assert r.letters == []


def test_edit_refused_when_there_is_no_letter(settings, seeded: Ids):
    c = make(settings)
    req = seed_kit(settings, seeded.b)
    assert (
        post(c, f"/jobs/{seeded.b}/kit/cover", cover_md="x", request_id=str(req)).status_code == 409
    )


def test_make_button_queues_one_cover_request_for_an_existing_kit(settings, seeded: Ids):
    c = make(settings)
    seed_kit(settings, seeded.b)
    page = c.get(f"/jobs/{seeded.b}").text
    assert "Make a cover letter" in page
    assert post(c, f"/jobs/{seeded.b}/kit/cover/make").status_code == 303
    row = db(settings).latest_cover_request(seeded.b)
    assert row["status"] == "queued" and row["kind"] == "cover"
    assert db(settings).latest_cv_request(seeded.b)["kind"] == "cv"
    page = c.get(f"/jobs/{seeded.b}").text
    assert "being generated (Opus)" in page and "Make a cover letter" not in page
    assert post(c, f"/jobs/{seeded.b}/kit/cover/make").status_code == 409  # already queued


def test_make_refused_without_kit_or_when_letter_exists(settings, seeded: Ids):
    c = make(settings)
    assert post(c, f"/jobs/{seeded.b}/kit/cover/make").status_code == 409
    with_cover(settings, seeded.b)
    assert post(c, f"/jobs/{seeded.b}/kit/cover/make").status_code == 409
    assert db(settings).latest_cover_request(seeded.b) is None


def _letter_request(settings, cid, status, reasons):
    s = db(settings)
    cur = s.conn.execute(
        "SELECT job_id, decision_id, kit_dir FROM cv_requests WHERE kind='cv' ORDER BY id DESC"
    ).fetchone()
    s.conn.execute(
        "INSERT INTO cv_requests (canonical_id, job_id, decision_id, status, requested_at, kind, "
        "review_reasons) VALUES (?,?,?,?,'x','cover',?)",
        (cid, cur["job_id"], cur["decision_id"], status, reasons),
    )


def test_cover_warnings_only_for_a_letter_newer_than_the_cv_and_can_be_approved(
    settings, seeded: Ids
):
    c = make(settings)
    with_cover(settings, seeded.b)
    _letter_request(settings, seeded.b, "needs_review", '["cover_mixed_tools:BigQuery"]')
    page = c.get(f"/jobs/{seeded.b}").text
    assert "cover_mixed_tools:BigQuery" in page and "kit/cover/approve" in page
    assert post(c, f"/jobs/{seeded.b}/kit/cover/approve").status_code == 303
    assert db(settings).latest_cover_request(seeded.b)["status"] == "done"
    page = c.get(f"/jobs/{seeded.b}").text
    assert "cover_mixed_tools" not in page and "kit/cover/approve" not in page
    assert post(c, f"/jobs/{seeded.b}/kit/cover/approve").status_code == 409


def test_old_letter_warnings_are_hidden_after_a_new_cv(settings, seeded: Ids):
    c = make(settings)
    with_cover(settings, seeded.b)
    s = db(settings)
    s.conn.execute("UPDATE cv_requests SET status='done'")
    _letter_request(settings, seeded.b, "needs_review", '["cover_mixed_tools:BigQuery"]')
    s.conn.execute(
        "INSERT INTO cv_requests (canonical_id, job_id, decision_id, status, requested_at, kind) "
        "SELECT canonical_id, job_id, decision_id, 'done', '2026-11-10T10:00:00+00:00', 'cv' "
        "FROM cv_requests WHERE id=1"
    )
    assert "cover_mixed_tools" not in c.get(f"/jobs/{seeded.b}").text
