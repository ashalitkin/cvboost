from __future__ import annotations

from tests.conftest import NOW
from tests.web.conftest import Ids, csrf, db


def post(client, path, **data):
    return client.post(path, data={"csrf_token": csrf(client), **data})


def test_status_change_writes_application_and_event(client, seeded: Ids, settings):
    r = post(client, f"/jobs/{seeded.b}/status", status="applied", note="sent via form", next="job")
    assert r.status_code == 303 and r.headers["location"] == f"/jobs/{seeded.b}"
    s = db(settings)
    a = s.conn.execute("SELECT * FROM applications").fetchone()
    assert (a["canonical_id"], a["status"], a["note"]) == (seeded.b, "applied", "sent via form")
    assert s.conn.execute("SELECT COUNT(*) FROM application_events").fetchone()[0] == 1
    assert "Applied" in client.get("/applications").text
    r = post(client, f"/applications/{seeded.b}/status", status="interview", next="applications")
    assert r.headers["location"] == "/applications"
    assert s.conn.execute("SELECT COUNT(*) FROM application_events").fetchone()[0] == 2


def test_invalid_status_and_long_note(client, seeded: Ids, settings):
    assert post(client, f"/jobs/{seeded.b}/status", status="hired").status_code == 400
    assert (
        post(client, f"/jobs/{seeded.b}/status", status="applied", note="x" * 2001).status_code
        == 400
    )
    assert db(settings).conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


def test_effective_status_derives_from_cv_request(client, seeded: Ids, settings):
    s = db(settings)
    _, req, _ = s.request_cv(seeded.b, channel="panel", now=NOW)
    assert "Preparing CV" in client.get("/applications").text
    s.conn.execute("UPDATE cv_requests SET status='done' WHERE id=?", (req,))
    page = client.get("/applications").text
    assert "<h2>Ready " in page and '<h2>Preparing CV <span class="muted">0</span>' in page
    s.conn.execute("UPDATE cv_requests SET status='failed', error='<b>boom</b>' WHERE id=?", (req,))
    page = client.get("/applications").text
    assert "CV failed" in page and "&lt;b&gt;boom&lt;/b&gt;" in page


def test_cancelled_request_is_not_an_application(client, seeded: Ids, settings):
    s = db(settings)
    _, req, _ = s.request_cv(seeded.b, channel="panel", now=NOW)
    s.record_decision(seeded.b, "undo", channel="panel", now=NOW)
    assert "Analytics Engineer" not in client.get("/applications").text


def test_kit_files_escaped_and_fixed_names_only(client, seeded: Ids, settings):
    assert "The kit appears" in client.get(f"/jobs/{seeded.b}").text
    d = settings.kits_dir / str(seeded.b)
    d.mkdir(parents=True)
    (d / "resume.md").write_text("# CV <script>alert(1)</script>")
    (d / "resume.pdf").write_bytes(b"%PDF-1.4 fake")
    (d / "secret.txt").write_text("nope")
    page = client.get(f"/jobs/{seeded.b}").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert(1)" not in page
    r = client.get(f"/jobs/{seeded.b}/kit/resume.md")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    pdf = client.get(f"/jobs/{seeded.b}/kit/resume.pdf")
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["cache-control"] == "no-store"  # rebuilt in place: no stale browser copy
    assert r.headers["cache-control"] == "no-store"
    for name in ("secret.txt", "..%2F..%2Fetc%2Fpasswd", "../resume.md", "recruiter_note.md"):
        assert client.get(f"/jobs/{seeded.b}/kit/{name}").status_code == 404, name


def test_kit_symlink_out_of_dir_refused(client, seeded: Ids, settings, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    d = settings.kits_dir / str(seeded.b)
    d.mkdir(parents=True)
    (d / "resume.md").symlink_to(outside)
    assert client.get(f"/jobs/{seeded.b}/kit/resume.md").status_code == 404


def test_default_kits_dir_is_next_to_db(tmp_path, monkeypatch):
    from cvboost.config import Settings

    monkeypatch.setenv("CVBOOST_DB", str(tmp_path / "vol" / "x.sqlite"))
    monkeypatch.delenv("CVBOOST_KITS_DIR", raising=False)
    assert Settings.from_env().kits_dir == tmp_path / "vol" / "kits"
