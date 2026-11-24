from __future__ import annotations

import json
import os
import stat

from cvboost.cv import kit as K
from cvboost.cv.step import run_cv_step
from cvboost.llm import client as C
from cvboost.llm.client import LLMResult
from tests.conftest import NOW
from tests.cv.helpers import (
    FakeRenderer,
    cv_cfg,
    draft_dict,
    last_stdin,
    make_settings,
    ok_draft,
    queue_request,
    with_md,
)
from tests.llm.helpers import FakeClient
from tests.worker.helpers import FakeClock


def run(store, client, tmp_path, *, cfg=None, renderer=None, clock=None, settings=None, **kw):
    kw.setdefault("lock_wait_s", 0)
    return run_cv_step(
        store,
        client,
        cfg or cv_cfg(),
        settings or make_settings(tmp_path),
        renderer or FakeRenderer(),
        clock or FakeClock(),
        **kw,
    )


def req_row(store, rid):
    return store.conn.execute("SELECT * FROM cv_requests WHERE id=?", (rid,)).fetchone()


def cv_calls(store):
    return store.conn.execute("SELECT * FROM llm_calls WHERE kind='cv' ORDER BY id").fetchall()


def alerts(store):
    return store.conn.execute("SELECT kind, text FROM system_alerts ORDER BY id").fetchall()


def test_ac1_request_becomes_a_kit_and_cv_ready_alert(store, tmp_path):
    ((rid, _, cid),) = queue_request(store)
    client = FakeClient(ok_draft())
    renderer = FakeRenderer()
    st = run(store, client, tmp_path, renderer=renderer)
    assert (st.done, st.calls) == (1, 1)
    kit = tmp_path / "data" / "kits" / str(cid)
    for name in ("resume.md", "resume.pdf", "recruiter_note.md", "fit_notes.md", "claims.json"):
        f = kit / name
        assert f.is_file() and stat.S_IMODE(f.stat().st_mode) == 0o600, name
    assert stat.S_IMODE(kit.stat().st_mode) == 0o700
    assert (kit / "resume.pdf").read_bytes().startswith(b"%PDF")
    assert renderer.calls == [(kit / "resume.md").read_text()]
    row = req_row(store, rid)
    assert row["status"] == "done" and row["kit_dir"] == str(cid)
    assert row["pdf_status"] == "ok" and row["attempts"] == 1 and row["review_reasons"] is None
    assert row["recommendation"] == "apply_with_caveats"
    assert row["fit_summary"] == "Хорошее совпадение по стеку."
    assert row["prompt_version"].startswith("cv-v1-") and len(row["profile_sha"]) == 64
    calls = cv_calls(store)
    assert len(calls) == 1 and calls[0]["model_alias"] == "opus" and calls[0]["status"] == "ok"
    assert calls[0]["id"] == row["last_call_id"]
    ((kind, text),) = alerts(store)
    assert kind == "cv_ready" and text.startswith("CV ready:")
    assert f"https://panel.example.com/jobs/{cid}" in text
    assert "Recommendation: apply with caveats" in text
    fit_notes = (kit / "fit_notes.md").read_text()
    assert "Хорошее совпадение" in fit_notes  # the LLM body is data, unchanged
    assert fit_notes.startswith("# Job analysis\n\nRecommendation: **apply with caveats**\n\n")
    assert "## Matches\n" in fit_notes and "## Risks\n" in fit_notes and "## Gaps\n" in fit_notes
    assert "LinkedIn connection note" in (kit / "recruiter_note.md").read_text()
    assert len(json.loads((kit / "claims.json").read_text())) == 5


def test_alert_without_panel_url_names_the_job(store, tmp_path):
    ((_, _, cid),) = queue_request(store)
    run(store, FakeClient(ok_draft()), tmp_path, settings=make_settings(tmp_path, panel_url=None))
    assert f"Panel → job #{cid}" in alerts(store)[0]["text"]


def test_ac2_forbidden_skill_twice_needs_review(store, tmp_path):
    ((rid, _, cid),) = queue_request(store)
    bad = ok_draft(with_md("Dashboards in Power BI."))
    client = FakeClient(bad, bad)
    st1 = run(store, client, tmp_path)
    assert st1.requeued == 1 and req_row(store, rid)["status"] == "queued"
    assert list((tmp_path / "data" / "kits" / str(cid)).iterdir()) == []  # nothing published yet
    st2 = run(store, client, tmp_path)
    row = req_row(store, rid)
    assert st2.needs_review == 1 and row["status"] == "needs_review"
    assert "gap_term:power_bi" in json.loads(row["review_reasons"])
    assert (tmp_path / "data" / "kits" / str(cid) / "resume.md").is_file()  # kept for the owner
    kinds = [a["kind"] for a in alerts(store)]
    assert kinds == ["cv_review"] and "needs review" in alerts(store)[0]["text"]
    assert "gap_term:power_bi" in alerts(store)[0]["text"]
    assert len(client.requests) == 2


def test_retry_with_feedback_then_clean_draft_is_done(store, tmp_path):
    ((rid, _, _),) = queue_request(store)
    client = FakeClient(ok_draft(with_md("Reports in Tableau.")), ok_draft())
    run(store, client, tmp_path)
    assert "<feedback>" not in client.requests[0].stdin_text
    run(store, client, tmp_path)
    second = last_stdin(client)
    assert "<feedback>" in second and "forbidden topic" in second and "Tableau" in second
    assert "Reports in Tableau" not in second  # the model's own text is not echoed back
    row = req_row(store, rid)
    assert row["status"] == "done" and row["attempts"] == 2 and row["feedback"] is None
    assert [c["attempt"] for c in cv_calls(store)] == [1, 2]
    assert [a["kind"] for a in alerts(store)] == ["cv_ready"]


def test_review_term_goes_to_review_without_retry(store, tmp_path):
    ((rid, _, _),) = queue_request(store)
    client = FakeClient(ok_draft(with_md("Built HIPAA compliant reporting.")), ok_draft())
    st = run(store, client, tmp_path)
    row = req_row(store, rid)
    assert st.needs_review == 1 and len(client.requests) == 1
    assert row["status"] == "needs_review" and "gap_review:medical_data" in row["review_reasons"]


def test_injection_suspected_goes_to_review(store, tmp_path):
    ((rid, _, _),) = queue_request(store)
    run(store, FakeClient(ok_draft(draft_dict(injection_suspected=True))), tmp_path)
    row = req_row(store, rid)
    assert row["status"] == "needs_review" and "injection_suspected" in row["review_reasons"]


def test_pdf_unavailable_keeps_markdown_and_asks_for_review(store, tmp_path):
    ((rid, _, cid),) = queue_request(store)
    run(store, FakeClient(ok_draft()), tmp_path, renderer=FakeRenderer(unavailable=True))
    row = req_row(store, rid)
    kit = tmp_path / "data" / "kits" / str(cid)
    assert row["status"] == "needs_review" and row["pdf_status"] == "unavailable"
    assert "pdf_unavailable" in row["review_reasons"]
    assert (kit / "resume.md").is_file() and not (kit / "resume.pdf").exists()


def test_daily_cap_zero_does_not_call_claude(store, tmp_path):
    ((rid, _, _),) = queue_request(store)
    client = FakeClient(ok_draft())
    st = run(store, client, tmp_path, cfg=cv_cfg(daily_call_caps={"haiku": 60, "opus": 0}))
    assert st.stop_code == "daily_cap" and client.requests == [] and client.preflights == 0
    row = req_row(store, rid)
    assert row["status"] == "queued" and row["attempts"] == 0
    assert [a["kind"] for a in alerts(store)] == ["cv_stopped"]


def test_usage_limit_pauses_everything_and_refunds_the_attempt(store, tmp_path):
    ((rid, _, _),) = queue_request(store)
    clock = FakeClock()
    st = run(store, FakeClient(LLMResult(status=C.USAGE_LIMIT, error_message="limit")), tmp_path,
             clock=clock)  # fmt: skip
    assert st.stop_code == "paused" and st.requeued == 1
    row = req_row(store, rid)
    assert row["status"] == "queued" and row["attempts"] == 0
    pause = store.active_pause(clock.t)
    assert pause is not None and pause[1] == "usage_limit"  # scoring sees it too
    again = FakeClient(ok_draft())
    assert run(store, again, tmp_path, clock=clock).stop_code == "paused"
    assert again.requests == []


def test_two_invalid_outputs_fail_with_short_error(store, tmp_path):
    ((rid, _, _),) = queue_request(store)
    garbage = LLMResult(status=C.OK, structured={"resume_md": "x"}, total_cost_usd=0.1)
    client = FakeClient(garbage, garbage)
    run(store, client, tmp_path)
    assert req_row(store, rid)["status"] == "queued"
    st = run(store, client, tmp_path)
    row = req_row(store, rid)
    assert st.failed == 1 and row["status"] == "failed" and len(row["error"]) <= 300
    assert row["error"].startswith("invalid_output")
    assert [c["status"] for c in cv_calls(store)] == ["invalid_output", "invalid_output"]
    assert [a["kind"] for a in alerts(store)] == ["cv_failed"]


def test_timeout_uses_the_attempt(store, tmp_path):
    ((rid, _, _),) = queue_request(store)
    run(store, FakeClient(LLMResult(status=C.TIMEOUT)), tmp_path)
    row = req_row(store, rid)
    assert row["status"] == "queued" and row["attempts"] == 1


def test_backend_exception_is_an_accounted_error(store, tmp_path):
    ((rid, _, _),) = queue_request(store)
    run(store, FakeClient(RuntimeError("boom")), tmp_path)
    assert cv_calls(store)[0]["status"] == "error" and req_row(store, rid)["attempts"] == 1


def test_stale_in_progress_request_is_requeued_and_processed(store, tmp_path):
    ((rid, _, _),) = queue_request(store)
    store.claim_next_cv_request(NOW, 2)  # a worker died holding it
    assert req_row(store, rid)["status"] == "in_progress"
    clock = FakeClock()
    clock.advance(seconds=2 * 300 + 60)
    st = run(store, FakeClient(ok_draft()), tmp_path, clock=clock)
    row = req_row(store, rid)
    assert st.done == 1 and row["status"] == "done" and row["attempts"] == 2


def test_stale_request_out_of_attempts_fails(store, tmp_path):
    ((rid, _, _),) = queue_request(store)
    store.claim_next_cv_request(NOW, 2)
    store.requeue_cv_request(rid, feedback=None, refund=False)
    store.claim_next_cv_request(NOW, 2)  # attempts = 2, in progress
    clock = FakeClock()
    clock.advance(hours=1)
    run(store, FakeClient(ok_draft()), tmp_path, clock=clock)
    assert req_row(store, rid)["status"] == "failed"


def test_cancel_during_the_call_is_not_overwritten(store, tmp_path):
    ((rid, _, cid),) = queue_request(store)

    def cancel_then_answer(req):
        store.conn.execute("UPDATE cv_requests SET status='cancelled' WHERE id=?", (rid,))
        return ok_draft()

    st = run(store, FakeClient(cancel_then_answer), tmp_path)
    assert st.done == 0 and req_row(store, rid)["status"] == "cancelled"
    assert list((tmp_path / "data" / "kits" / str(cid)).iterdir()) == []
    assert alerts(store) == []


def test_symlinked_kit_dir_is_refused(store, tmp_path):
    ((rid, _, cid),) = queue_request(store)
    kits = tmp_path / "data" / "kits"
    kits.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (kits / str(cid)).symlink_to(outside)
    client = FakeClient(ok_draft())
    st = run(store, client, tmp_path)
    assert st.failed == 1 and req_row(store, rid)["status"] == "failed"
    assert list(outside.iterdir()) == []
    assert req_row(store, rid)["error"] == "kit path refused"
    assert client.requests == []  # refused before the paid call
    assert [a["kind"] for a in alerts(store)] == ["cv_failed"]


def test_write_failure_after_the_call_fails_the_request(store, tmp_path, monkeypatch):
    ((rid, _, _),) = queue_request(store)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(K, "write_kit", boom)
    st = run(store, FakeClient(ok_draft()), tmp_path)
    assert st.failed == 1 and req_row(store, rid)["error"] == "kit write failed"


def test_regeneration_keeps_one_previous_generation(store, tmp_path):
    ((rid, jid, cid),) = queue_request(store)
    run(store, FakeClient(ok_draft()), tmp_path)
    kit = tmp_path / "data" / "kits" / str(cid)
    first = (kit / "resume.md").read_text()
    store.request_cv(jid, channel="panel", now=NOW)  # the owner asks again
    second_md = draft_dict()["resume_md"].replace(
        "Senior Analytics Engineer", "Lead Analytics Engineer"
    )
    run(store, FakeClient(ok_draft(draft_dict(resume_md=second_md))), tmp_path)
    assert "Lead Analytics" in (kit / "resume.md").read_text()
    assert (kit / "prev" / "resume.md").read_text() == first
    assert stat.S_IMODE((kit / "prev").stat().st_mode) == 0o700


def test_busy_kit_costs_nothing_and_gives_the_attempt_back(store, tmp_path):
    ((rid, _, cid),) = queue_request(store)
    settings = make_settings(tmp_path)
    handle = K.kit_lock(settings.kits_dir, cid)  # the panel holds it
    waited: list[float] = []
    client = FakeClient(ok_draft())
    try:
        st = run(store, client, tmp_path, settings=settings, lock_wait_s=0.2, sleep=waited.append)
    finally:
        handle.release()
    row = req_row(store, rid)
    assert st.requeued == 1 and row["status"] == "queued" and row["attempts"] == 0
    assert client.requests == [] and cv_calls(store) == []  # no Opus call, nothing spent
    assert waited  # it did wait for the lock before giving up


def test_lock_released_by_a_waiting_panel_lets_the_call_go_ahead(store, tmp_path):
    ((rid, _, cid),) = queue_request(store)
    settings = make_settings(tmp_path)
    handle = K.kit_lock(settings.kits_dir, cid)
    client = FakeClient(ok_draft())
    st = run(
        store, client, tmp_path, settings=settings, lock_wait_s=5, sleep=lambda s: handle.release()
    )
    assert st.done == 1 and len(client.requests) == 1


def test_the_kit_stays_locked_during_the_call_and_is_released_after(store, tmp_path):
    ((rid, _, cid),) = queue_request(store)
    settings = make_settings(tmp_path)
    seen: list[str] = []

    def during(req):
        try:
            K.kit_lock(settings.kits_dir, cid)
            seen.append("free")
        except K.KitBusy:
            seen.append("busy")
        return ok_draft()

    run(store, FakeClient(during), tmp_path, settings=settings)
    assert seen == ["busy"]
    K.kit_lock(settings.kits_dir, cid).release()


def test_exhausted_requests_are_failed_with_an_alert(store, tmp_path):
    ((rid, _, _),) = queue_request(store)
    store.conn.execute("UPDATE cv_requests SET attempts=2 WHERE id=?", (rid,))  # queued, spent
    st = run(store, FakeClient(ok_draft()), tmp_path)
    row = req_row(store, rid)
    assert st.failed == 1 and row["status"] == "failed" and row["finished_at"]
    assert [a["kind"] for a in alerts(store)] == ["cv_failed"]


def test_broken_resume_config_stops_the_step_and_alerts(store, tmp_path):
    ((rid, _, _),) = queue_request(store)
    rd = tmp_path / "resume"
    rd.mkdir()
    for f in ("profile.md", "photo.jpg", "resume.css"):
        (rd / f).write_bytes((make_settings(tmp_path).resume_dir / f).read_bytes())
    (rd / "gaps.yaml").write_text(
        "gaps:\n  - {id: x, source: Dataform, mode: forbid, patterns: ['dataform']}\n"
    )
    client = FakeClient(ok_draft())
    st = run(store, client, tmp_path, settings=make_settings(tmp_path, resume_dir=rd))
    assert st.stop_code == "config" and client.requests == []
    assert req_row(store, rid)["status"] == "queued"
    assert [a["kind"] for a in alerts(store)] == ["cv_config"]
    assert "Known gaps item has no entry" in alerts(store)[0]["text"]


def test_disabled_and_empty_queue_do_nothing(store, tmp_path):
    client = FakeClient(ok_draft())
    assert run(store, client, tmp_path).calls == 0 and client.preflights == 0
    queue_request(store)
    off = cv_cfg(cv={"enabled": False})
    assert run(store, client, tmp_path, cfg=off).stop_code == "disabled"
    assert client.requests == []


def test_named_request_is_taken_instead_of_the_oldest(store, tmp_path):
    first, second = queue_request(store, 2)
    client = FakeClient(ok_draft())
    run(store, client, tmp_path, request_id=second[0])
    assert req_row(store, first[0])["status"] == "queued"
    assert req_row(store, second[0])["status"] == "done"
    assert os.path.isdir(tmp_path / "data" / "kits" / str(second[2]))
