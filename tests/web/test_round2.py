from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from cvboost.config import Settings
from cvboost.web.app import create_app
from tests.conftest import NOW
from tests.web.conftest import Ids, csrf, db, llm_verdict, verdict


def post(client, path, **data):
    return client.post(path, data={"csrf_token": csrf(client), **data})


# --- effective status ---


def test_note_on_derived_row_then_cv_done_shows_ready(client, seeded: Ids, settings):
    s = db(settings)
    _, req, _ = s.request_cv(seeded.b, channel="panel", now=NOW)
    assert (
        post(client, f"/applications/{seeded.b}/status", status="", note="call John").status_code
        == 303
    )
    assert s.conn.execute("SELECT COUNT(*) FROM application_events").fetchone()[0] == 0
    s.conn.execute("UPDATE cv_requests SET status='done' WHERE id=?", (req,))
    page = client.get("/applications").text
    assert (
        "<h2>Ready " in page
        and '<h2>Preparing CV <span class="muted">0</span>' in page
        and "call John" in page
    )


def test_manual_early_status_does_not_override_cv(client, seeded: Ids, settings):
    s = db(settings)
    s.set_application_status(seeded.b, "ready", None, NOW)
    _, req, _ = s.request_cv(seeded.b, channel="panel", now=NOW)
    assert "<h2>Preparing CV" in client.get("/applications").text
    s.set_application_status(seeded.b, "applied", None, NOW)  # later stages win over the CV
    page = client.get("/applications").text
    assert "<h2>Applied" in page and '<h2>Preparing CV <span class="muted">0</span>' in page


def test_status_select_has_auto_option_and_job_defaults_to_it(client, seeded: Ids):
    job = client.get(f"/jobs/{seeded.b}").text
    assert '<option value="" selected>— auto —</option>' in job
    s = client.get(f"/jobs/{seeded.b}").text
    assert "not started" in s


def test_needs_review_and_unknown_statuses_are_shown(client, seeded: Ids, settings):
    s = db(settings)
    _, req, _ = s.request_cv(seeded.b, channel="panel", now=NOW)
    s.conn.execute("PRAGMA ignore_check_constraints=ON")
    s.conn.execute("UPDATE cv_requests SET status='needs_review' WHERE id=?", (req,))
    page = client.get("/applications").text
    assert "<h2>Ready — review " in page
    s.conn.execute("UPDATE cv_requests SET status='weird' WHERE id=?", (req,))
    assert "<h2>weird " in client.get("/applications").text
    s.conn.execute("UPDATE cv_requests SET status='cancelled' WHERE id=?", (req,))
    assert "Analytics Engineer" not in client.get("/applications").text


# --- kits ---


def test_kit_dir_from_cv_request(client, seeded: Ids, settings):
    s = db(settings)
    _, req, _ = s.request_cv(seeded.a_remoteok, channel="panel", now=NOW)
    d = settings.kits_dir / str(seeded.a_remotive)  # another member's id
    d.mkdir(parents=True)
    (d / "resume.md").write_text("member kit")
    assert "member kit" not in client.get(f"/jobs/{seeded.a_remoteok}").text
    s.conn.execute("UPDATE cv_requests SET kit_dir=? WHERE id=?", (str(seeded.a_remotive), req))
    assert "member kit" in client.get(f"/jobs/{seeded.a_remoteok}").text
    assert client.get(f"/jobs/{seeded.a_remoteok}/kit/resume.md").status_code == 200


def test_kit_dir_must_be_digits(client, seeded: Ids, settings, tmp_path):
    s = db(settings)
    _, req, _ = s.request_cv(seeded.b, channel="panel", now=NOW)
    (tmp_path / "evil").mkdir()
    (tmp_path / "evil" / "resume.md").write_text("evil")
    for bad in ("../evil", "../../evil", "/etc", "1/../../evil"):
        s.conn.execute("UPDATE cv_requests SET kit_dir=? WHERE id=?", (bad, req))
        assert client.get(f"/jobs/{seeded.b}/kit/resume.md").status_code == 404, bad


def test_md_kit_is_attachment_and_size_capped(client, seeded: Ids, settings):
    d = settings.kits_dir / str(seeded.b)
    d.mkdir(parents=True)
    (d / "resume.md").write_text("x" * 300_000)
    r = client.get(f"/jobs/{seeded.b}/kit/resume.md")
    assert r.headers["content-disposition"].startswith("attachment")
    assert len(r.content) == 200_000


def test_settings_kits_dir_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("CVBOOST_DB", str(tmp_path / "v" / "x.sqlite"))
    monkeypatch.setenv("CVBOOST_KITS_DIR", str(tmp_path / "k"))
    monkeypatch.setenv("CVBOOST_PANEL_URL", "https://panel.example.com/")
    st = Settings.from_env()
    assert st.kits_override == tmp_path / "k" and st.kits_dir == tmp_path / "k"
    assert st.panel_url == "https://panel.example.com"
    monkeypatch.setenv("CVBOOST_PANEL_URL", "javascript:x")
    with pytest.raises(ValueError):
        Settings.from_env()


# --- inbox: live members only, filters ---


def test_closed_representative_with_live_member(client, seeded: Ids, settings):
    s = db(settings)
    s.conn.execute("UPDATE verdicts SET relevance = 95 WHERE job_id = ?", (seeded.mixed_dead,))
    s.conn.execute(
        "UPDATE jobs SET title='Dead Title', salary_text='$999/h' WHERE id = ?",
        (seeded.mixed_dead,),
    )
    inbox = client.get("/inbox").text
    assert "Mixed Group Role" in inbox and "Dead Title" not in inbox and "$999/h" not in inbox
    job = client.get(f"/jobs/{seeded.mixed_alive}").text
    assert "Dead Title" not in job and "$999/h" not in job and "Mixed Group Role" in job


def test_filters(client, seeded: Ids, settings):
    s = db(settings)
    b = seeded.b

    def inbox(q=""):
        return client.get("/inbox?" + q).text

    # since_days (posted 10 days ago: alive, but older than 7)
    s.conn.execute("UPDATE jobs SET posted_at=? WHERE id=?", ("2026-10-27T12:00:00+00:00", b))
    assert "Beta" not in inbox("since_days=7") and "Beta" in inbox("since_days=30")
    # rate_unknown
    s.conn.execute("UPDATE verdicts SET flags_json='[]' WHERE job_id=?", (b,))
    assert "Beta" in inbox("rate_unknown=0") and "Acme" not in inbox("rate_unknown=0")
    assert "Beta" not in inbox("rate_unknown=1") and "Acme" in inbox("rate_unknown=1")
    # profile (rule profile, no LLM verdict for Beta)
    s.conn.execute("UPDATE verdicts SET profile='marketing_analytics' WHERE job_id=?", (b,))
    assert "Beta" in inbox("profile=marketing_analytics")
    assert "Beta" not in inbox("profile=data_engineer")
    # eligible
    assert "Beta" in inbox("eligible=unscored") and "Acme" not in inbox("eligible=unscored")
    llm_verdict(s, b, verdict(eligible=False, fit=10))
    assert "Beta" not in inbox() and "Beta" in inbox("eligible=no")
    assert "Acme" not in inbox("eligible=no")
    assert "Beta" in inbox("eligible=any")
    # decision
    post(client, f"/jobs/{b}/decision", action="interested")
    assert "Beta" in inbox("decision=interested&eligible=any")
    assert "Acme" not in inbox("decision=interested&eligible=any")
    assert "Beta" not in inbox("decision=none&eligible=any") and "Acme" in inbox(
        "decision=none&eligible=any"
    )


# --- errors ---


@pytest.fixture
def crashing(settings, seeded) -> TestClient:
    app = create_app(settings, clock=lambda: NOW)

    def boom():
        raise RuntimeError("secret internal detail")

    def locked():
        raise sqlite3.OperationalError("database is locked")

    app.add_api_route("/boom", boom)
    app.add_api_route("/locked", locked)
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


def test_unhandled_error_is_a_clean_500_with_headers(crashing):
    r = crashing.get("/boom")
    assert r.status_code == 500 and "secret internal detail" not in r.text
    assert "Traceback" not in r.text and "Internal error" in r.text
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"


def test_database_locked_is_503(crashing):
    r = crashing.get("/locked")
    assert r.status_code == 503 and "The database is busy" in r.text
    assert "content-security-policy" in r.headers


def test_body_limit(client, seeded: Ids):
    tok = csrf(client)
    big = {"csrf_token": tok, "enabled": "0", "pad": "x" * 70_000}
    assert client.post("/sources/remotive/enabled", data=big).status_code == 413
    small = {"csrf_token": tok, "enabled": "1"}
    assert client.post("/sources/remotive/enabled", data=small).status_code == 303


# --- round 3 ---


def test_note_only_save_is_not_a_cv_card(client, seeded: Ids, settings):
    assert (
        post(client, f"/jobs/{seeded.b}/status", status="", note="think later").status_code == 303
    )
    s = db(settings)
    assert s.application(seeded.b)["note"] == "think later"
    page = client.get("/applications").text
    assert (
        '<h2>Preparing CV <span class="muted">0</span>' in page and "Analytics Engineer" not in page
    )
    job = client.get(f"/jobs/{seeded.b}").text
    assert "think later" in job and '<option value="" selected>— auto —</option>' in job


def test_note_then_cancelled_cv_is_not_a_cv_card(client, seeded: Ids, settings):
    s = db(settings)
    s.request_cv(seeded.b, channel="panel", now=NOW)
    post(client, f"/jobs/{seeded.b}/status", status="", note="n")
    s.record_decision(seeded.b, "undo", channel="panel", now=NOW)  # cancels the queued request
    page = client.get("/applications").text
    assert (
        '<h2>Preparing CV <span class="muted">0</span>' in page and "Analytics Engineer" not in page
    )


def test_explicit_preparing_choice_still_shows(client, seeded: Ids):
    post(client, f"/jobs/{seeded.b}/status", status="preparing", note="")
    assert "<h2>Preparing CV" in client.get("/applications").text


def test_chunked_oversize_body_is_413(client, seeded: Ids):
    tok = csrf(client)

    def body():
        yield f"csrf_token={tok}&enabled=0&pad=".encode()
        for _ in range(80):
            yield b"x" * 1024

    r = client.post(
        "/sources/remotive/enabled",
        content=body(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 413


def test_since_days_uses_live_members_only(client, seeded: Ids, settings):
    s = db(settings)
    s.conn.execute(
        "UPDATE jobs SET posted_at='2026-10-17T12:00:00+00:00' WHERE id=?", (seeded.mixed_alive,)
    )  # 20 days old, alive; the closed member is newer
    assert "Mixed Group Role" not in client.get("/inbox?since_days=7").text
    assert "Mixed Group Role" in client.get("/inbox?since_days=30").text


def test_board_columns_keep_pipeline_order_even_when_empty(client, seeded: Ids, settings):
    """Owner 2026-11-10: columns never move between reloads."""
    s = db(settings)
    s.request_cv(seeded.b, channel="panel", now=NOW)
    page = client.get("/applications").text
    heads = [
        "Preparing CV",
        "Ready — review",
        "Ready ",
        "Applied",
        "Replied",
        "Interview",
        "Offer",
        "Rejected",
    ]
    pos = [page.index(f"<h2>{h}") for h in heads]
    assert pos == sorted(pos)
