from __future__ import annotations

from fastapi.testclient import TestClient

from cvboost.cv import kit as K
from cvboost.web.app import create_app
from tests.conftest import NOW
from tests.cv.helpers import FakeRenderer
from tests.web.conftest import Ids, csrf, db


def make(settings, renderer=None) -> TestClient:
    app = create_app(settings, clock=lambda: NOW, renderer=renderer or FakeRenderer())
    return TestClient(app, follow_redirects=False)


def seed_kit(settings, cid, status="done", reasons=None, md="# CV\n", via=None):
    s = db(settings)
    _, req, _ = s.request_cv(via or cid, channel="panel", now=NOW)
    s.conn.execute("UPDATE cv_requests SET status=?, kit_dir=? WHERE id=?", (status, str(cid), req))
    if reasons:
        s.conn.execute(
            "UPDATE cv_requests SET review_reasons=? WHERE id=?",
            (__import__("json").dumps(reasons), req),
        )
    K.write_kit(
        settings.kits_dir,
        cid,
        {
            K.RESUME_MD: md,
            K.RESUME_PDF: b"%PDF-old",
            K.RECRUITER_NOTE: "Hi <b>there</b>",
            K.FIT_NOTES: "notes <script>x</script>",
        },
    )
    return req


def post(c, path, **data):
    return c.post(path, data={"csrf_token": csrf(c), **data})


def edit(c, cid, req, md="# x"):
    return post(c, f"/jobs/{cid}/kit/resume", resume_md=md, request_id=str(req))


def test_edit_rewrites_file_and_rebuilds_pdf(settings, seeded: Ids):
    r = FakeRenderer()
    c = make(settings, r)
    req = seed_kit(settings, seeded.b)
    resp = edit(c, seeded.b, req, "# New\r\ntext")
    assert resp.status_code == 303
    d = settings.kits_dir / str(seeded.b)
    assert (d / "resume.md").read_text() == "# New\ntext"
    assert (d / "resume.pdf").read_bytes() == b"%PDF-1.4 fake"
    assert r.calls == ["# New\ntext"]
    row = db(settings).cv_request(req)
    assert row["pdf_status"] == "ok" and row["edited_at"] and row["status"] == "done"


def test_renderer_unavailable_saves_md(settings, seeded: Ids):
    c = make(settings, FakeRenderer(unavailable=True))
    req = seed_kit(settings, seeded.b)
    assert edit(c, seeded.b, req, "# New").status_code == 303
    d = settings.kits_dir / str(seeded.b)
    assert (d / "resume.md").read_text() == "# New"
    assert db(settings).cv_request(req)["pdf_status"] == "unavailable"
    assert "PDF not built" in c.get(f"/jobs/{seeded.b}").text


def test_no_csrf_403_files_untouched(settings, seeded: Ids):
    r = FakeRenderer()
    c = make(settings, r)
    seed_kit(settings, seeded.b)
    c.get("/inbox")
    resp = c.post(f"/jobs/{seeded.b}/kit/resume", data={"resume_md": "# evil"})
    assert resp.status_code == 403
    assert (settings.kits_dir / str(seeded.b) / "resume.md").read_text() == "# CV\n"
    assert r.calls == []


def test_script_in_markdown_escaped(settings, seeded: Ids):
    c = make(settings)
    seed_kit(settings, seeded.b, md="# CV <script>alert(1)</script>")
    page = c.get(f"/jobs/{seeded.b}").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)" not in page and "<script>x" not in page
    assert "<textarea" in page and "&lt;b&gt;there&lt;/b&gt;" in page


def test_too_long_and_empty_400_and_body_limits(settings, seeded: Ids):
    r = FakeRenderer()
    c = make(settings, r)
    req = seed_kit(settings, seeded.b)
    assert edit(c, seeded.b, req, "ж" * 20001).status_code == 400  # not 413
    assert edit(c, seeded.b, req, "  ").status_code == 400
    assert edit(c, seeded.b, req, "ж" * 20000).status_code == 303
    assert len(r.calls) == 1
    assert edit(c, seeded.b, req, "x" * (260 * 1024)).status_code == 413
    # other routes keep the 64 KB limit
    big = c.post(
        f"/jobs/{seeded.b}/status",
        data={"csrf_token": csrf(c), "status": "applied", "note": "x" * (70 * 1024)},
    )
    assert big.status_code == 413


def test_lock_busy_409(settings, seeded: Ids):
    r = FakeRenderer()
    c = make(settings, r)
    req = seed_kit(settings, seeded.b)
    with K.kit_lock(settings.kits_dir, seeded.b):
        resp = edit(c, seeded.b, req, "# x")
    assert resp.status_code == 409 and "generated right now" in resp.text
    assert (settings.kits_dir / str(seeded.b) / "resume.md").read_text() == "# CV\n"
    assert r.calls == []


def test_unopenable_lock_is_not_500(settings, seeded: Ids):
    c = make(settings)
    req = seed_kit(settings, seeded.b)
    lock = settings.kits_dir / f"{seeded.b}.lock"
    lock.unlink(missing_ok=True)
    lock.symlink_to(settings.kits_dir / "elsewhere")
    resp = edit(c, seeded.b, req, "# x")
    assert resp.status_code == 503 and "not writable" in resp.text
    assert (settings.kits_dir / str(seeded.b) / "resume.md").read_text() == "# CV\n"


def test_edit_refused_without_finished_request(settings, seeded: Ids):
    c = make(settings)
    assert edit(c, seeded.b, 1, "# x").status_code == 409
    req = seed_kit(settings, seeded.b, status="in_progress")
    assert edit(c, seeded.b, req, "# x").status_code == 409


def test_stale_request_id_409(settings, seeded: Ids):
    r = FakeRenderer()
    c = make(settings, r)
    req = seed_kit(settings, seeded.b)
    resp = edit(c, seeded.b, req + 100, "# x")
    assert resp.status_code == 409 and "was updated" in resp.text
    assert post(c, f"/jobs/{seeded.b}/kit/resume", resume_md="# x").status_code == 409
    assert r.calls == []
    assert (settings.kits_dir / str(seeded.b) / "resume.md").read_text() == "# CV\n"


def test_edit_in_needs_review_keeps_status(settings, seeded: Ids):
    c = make(settings)
    req = seed_kit(settings, seeded.b, status="needs_review", reasons=["x"])
    assert edit(c, seeded.b, req, "# fixed").status_code == 303
    row = db(settings).cv_request(req)
    assert row["status"] == "needs_review" and row["edited_at"] and row["pdf_status"] == "ok"


class Failing:
    def __init__(self, exc):
        self.exc = exc

    def render(self, md):
        raise self.exc


def test_render_error_keeps_text_and_retires_old_pdf(settings, seeded: Ids):
    from cvboost.cv.render import RenderError

    c = make(settings, Failing(RenderError("boom")))
    req = seed_kit(settings, seeded.b)
    assert edit(c, seeded.b, req, "# New").status_code == 303
    d = settings.kits_dir / str(seeded.b)
    assert (d / "resume.md").read_text() == "# New"
    assert not (d / "resume.pdf").exists()
    assert (d / "prev" / "resume.pdf").read_bytes() == b"%PDF-old"
    assert db(settings).cv_request(req)["pdf_status"] == "failed"
    assert c.get(f"/jobs/{seeded.b}/kit/resume.pdf").status_code == 404
    page = c.get(f"/jobs/{seeded.b}").text
    assert "PDF not built — the text was changed" in page and "Download PDF" not in page


def test_pdf_write_failure_is_failed_not_500(settings, seeded: Ids, monkeypatch):
    c = make(settings)
    req = seed_kit(settings, seeded.b)
    real = K.write_one

    def flaky(kits_dir, cid, name, data):
        if name == K.RESUME_PDF:
            raise OSError("disk full")
        return real(kits_dir, cid, name, data)

    monkeypatch.setattr(K, "write_one", flaky)
    assert edit(c, seeded.b, req, "# New").status_code == 303
    d = settings.kits_dir / str(seeded.b)
    assert (d / "resume.md").read_text() == "# New" and not (d / "resume.pdf").exists()
    assert db(settings).cv_request(req)["pdf_status"] == "failed"


def test_resume_md_write_failure_500(settings, seeded: Ids, monkeypatch):
    c = make(settings)
    req = seed_kit(settings, seeded.b)

    def boom(*a, **k):
        raise OSError("ro")

    monkeypatch.setattr(K, "write_one", boom)
    assert edit(c, seeded.b, req, "# New").status_code == 500
    assert db(settings).cv_request(req)["edited_at"] is None


def test_textarea_breakout_stays_inside(settings, seeded: Ids):
    c = make(settings)
    seed_kit(settings, seeded.b, md="a</textarea><script>alert(1)</script>")
    page = c.get(f"/jobs/{seeded.b}").text
    assert "<script>alert(1)" not in page
    assert "a&lt;/textarea&gt;&lt;script&gt;" in page


def test_approve_transitions(settings, seeded: Ids):
    c = make(settings)
    req = seed_kit(settings, seeded.b, status="needs_review", reasons=["gap_term:power_bi"])
    page = c.get(f"/jobs/{seeded.b}").text
    assert "Needs review" in page and "gap_term:power_bi" in page
    assert "kit/approve" in page
    assert post(c, f"/jobs/{seeded.b}/kit/approve").status_code == 303
    row = db(settings).cv_request(req)
    assert row["status"] == "done" and row["reviewed_at"]
    assert post(c, f"/jobs/{seeded.b}/kit/approve").status_code == 409
    assert "kit/approve" not in c.get(f"/jobs/{seeded.b}").text  # no button on done


def test_approve_409_for_queued_and_in_progress(settings, seeded: Ids):
    c = make(settings)
    assert post(c, f"/jobs/{seeded.b}/kit/approve").status_code == 409  # no request
    req = seed_kit(settings, seeded.b, status="queued")
    assert post(c, f"/jobs/{seeded.b}/kit/approve").status_code == 409
    db(settings).conn.execute("UPDATE cv_requests SET status='in_progress' WHERE id=?", (req,))
    assert post(c, f"/jobs/{seeded.b}/kit/approve").status_code == 409
    assert db(settings).cv_request(req)["status"] == "in_progress"


def test_no_textarea_while_in_progress(settings, seeded: Ids):
    c = make(settings)
    seed_kit(settings, seeded.b, status="in_progress")
    page = c.get(f"/jobs/{seeded.b}").text
    assert "<textarea" not in page.split('id="kit"')[1]


def test_group_request_on_member_works_via_canonical(settings, seeded: Ids):
    r = FakeRenderer()
    c = make(settings, r)
    req = seed_kit(settings, seeded.a_remoteok, via=seeded.a_remotive)
    resp = edit(c, seeded.a_remotive, req, "# x")
    assert resp.status_code == 307 and resp.headers["location"].startswith(
        f"/jobs/{seeded.a_remoteok}/"
    )
    assert edit(c, seeded.a_remoteok, req, "# grp").status_code == 303
    assert (settings.kits_dir / str(seeded.a_remoteok) / "resume.md").read_text() == "# grp"
    assert db(settings).cv_request(req)["pdf_status"] == "ok"
    assert post(c, f"/jobs/{seeded.a_remoteok}/kit/approve").status_code == 409  # done


def test_path_traversal_404(settings, seeded: Ids):
    c = make(settings)
    seed_kit(settings, seeded.b)
    for name in ("..%2F..%2Fx", "../x", "x.txt", "claims.json"):
        assert c.get(f"/jobs/{seeded.b}/kit/{name}").status_code == 404, name
    assert post(c, "/jobs/999999/kit/resume", resume_md="# x").status_code == 404
