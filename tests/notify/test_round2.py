"""Review round 2: isolation of system alerts, transport errors, stale verdicts, caps, limits."""

from __future__ import annotations

import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import yaml

from cvboost.llm import client as C
from cvboost.llm.prompts import score_v1 as P
from cvboost.llm.prompts.registry import score_version
from cvboost.llm.schema import JobVerdict
from cvboost.locks import try_lock
from cvboost.notify import telegram as T
from cvboost.notify.alerts import queue_system_alerts, select_candidates, send_pending
from cvboost.notify.wiring import make_notify_step
from cvboost.storage.db import Store
from cvboost.worker import CycleStats, run_cycle
from cvboost.worker_config import WorkerConfig
from tests.conftest import NOW
from tests.llm.helpers import RV, THRESHOLD, make_cfg, seed, verdict_dict
from tests.notify.helpers import OWNER, TOKEN, FakeTelegram, err, ok
from tests.notify.test_cycle_alerts import deps_for, run

LATER = NOW + timedelta(minutes=5)
ROOT = Path(__file__).parent.parent.parent


def scored(s, tmp_path, tg, n=1, **cfg):
    """n scored groups, notify step not run yet. Returns (deps, notify step)."""
    seed(s, n)
    deps = deps_for(tmp_path, tg, **cfg)
    real = None
    deps.notify, real = None, make_notify_step(deps, tg.client(), OWNER, TOKEN)
    run(s, deps)
    return deps, real


def alerts(s):
    return [dict(r) for r in s.conn.execute("SELECT * FROM alerts ORDER BY id")]


# --- 2: credential parsing and isolation of system alerts ---


@pytest.mark.parametrize("expires", ["garbage", "2026-11-08T00:00:00", "", "9999-99-99"])
def test_bad_or_naive_credential_date_never_blocks_cards(s, tmp_path, tg, expires):
    deps, step = scored(s, tmp_path, tg)
    s.insert_llm_call(
        kind="score",
        model_alias="haiku",
        prompt_version="v",
        n_items=1,
        attempt=1,
        now=NOW,
        local_day="2026-11-06",
        status=C.OK,
        credential_expires_at=expires,
    )
    step(s, LATER, CycleStats())
    cards = [m for m in tg.of("sendMessage") if "reply_markup" in m]
    assert len(cards) == 1 and alerts(s)[0]["status"] == "sent"


def test_naive_credential_date_is_taken_as_utc(s):
    s.insert_llm_call(
        kind="score",
        model_alias="haiku",
        prompt_version="v",
        n_items=1,
        attempt=1,
        now=NOW,
        local_day="2026-11-06",
        status=C.OK,
        credential_expires_at="2026-11-08T00:00:00",
    )
    assert queue_system_alerts(s, WorkerConfig(), NOW) == 1  # expired two days ago, UTC


def test_exception_in_system_alerts_does_not_hold_back_cards(s, tmp_path, tg, monkeypatch):
    deps, step = scored(s, tmp_path, tg)

    def boom(*a, **k):
        raise RuntimeError("system alerts exploded")

    monkeypatch.setattr("cvboost.notify.wiring.queue_system_alerts", boom)
    step(s, LATER, CycleStats())
    assert len(tg.of("sendMessage")) == 1


# --- 3 and 9: only a 4xx for the very message counts as an attempt ---


@pytest.mark.parametrize("bad", ["connect", "timeout", "502"])
def test_transport_and_5xx_errors_do_not_burn_attempts(s, tmp_path, tg, bad):
    deps, step = scored(s, tmp_path, tg, alerts={"max_attempts": 2})
    mk = {
        "connect": lambda: httpx.ConnectError("down"),
        "timeout": lambda: httpx.ReadTimeout("slow"),
        "502": lambda: err(502, "Bad Gateway"),
    }[bad]
    tg.queue("sendMessage", mk(), mk(), mk())
    for i in range(3):
        step(s, LATER + timedelta(minutes=10 * i), CycleStats())
        a = alerts(s)[0]
        assert (a["status"], a["attempts"]) == ("pending", 0), i
    step(s, LATER + timedelta(minutes=40), CycleStats())
    assert alerts(s)[0]["status"] == "sent"


def test_a_4xx_for_the_message_counts_and_gives_up(s, tmp_path, tg):
    deps, step = scored(s, tmp_path, tg, alerts={"max_attempts": 2})
    tg.queue("sendMessage", err(400, "Bad Request: nope"), err(400, "Bad Request: nope"))
    step(s, LATER, CycleStats())
    step(s, LATER + timedelta(minutes=10), CycleStats())
    a = alerts(s)[0]
    assert (a["status"], a["attempts"]) == ("failed", 2)


def test_system_alert_attempts_and_failure(s, tg):
    cfg = WorkerConfig.model_validate({"alerts": {"max_attempts": 2}})
    s.insert_system_alert("worker_started", "k", "hello", NOW)
    tg.queue("sendMessage", httpx.ConnectError("down"), err(400, "Bad Request"), err(400, "Bad"))
    send_pending(s, tg.client(), cfg, OWNER, NOW)
    row = s.conn.execute("SELECT status, attempts FROM system_alerts").fetchone()
    assert (row["status"], row["attempts"]) == ("pending", 0)  # transport error: no attempt
    send_pending(s, tg.client(), cfg, OWNER, NOW)
    send_pending(s, tg.client(), cfg, OWNER, NOW)
    row = s.conn.execute("SELECT status, attempts, last_error FROM system_alerts").fetchone()
    assert (row["status"], row["attempts"]) == ("failed", 2) and "Bad" in row["last_error"]
    assert send_pending(s, tg.client(), cfg, OWNER, NOW).system == 0  # no more tries


# --- 5: cap stops are classified by reason code ---


@pytest.mark.parametrize(
    "over,kind,word",
    [
        ({"daily_call_caps": {"haiku": 0, "opus": 10}}, "llm_daily_cap", "call limit"),
        ({"daily_cost_cap_usd": 0.0}, "llm_cost_cap", "cost limit"),
    ],
)
def test_cap_stops_raise_a_system_alert_through_the_cycle(s, tmp_path, tg, over, kind, word):
    seed(s, 1)
    deps = deps_for(tmp_path, tg)
    deps.llm_cfg = make_cfg(**over)
    deps.notify = make_notify_step(deps, tg.client(), OWNER, TOKEN)
    st = run(s, deps)
    assert st.score_code in ("daily_cap", "daily_cost_cap")
    [row] = s.conn.execute("SELECT kind, text FROM system_alerts").fetchall()
    assert row["kind"] == kind and word in row["text"]
    assert any(word in p["text"] for p in tg.of("sendMessage"))


def test_per_run_cap_is_not_a_system_alert(s):
    assert queue_system_alerts(s, WorkerConfig(), NOW, score_code="run_cap", score_text="x") == 0


def test_score_stats_carry_a_stop_code(s):
    from cvboost.score import score
    from tests.llm.helpers import FakeClient

    seed(s, 1)
    st = score(s, FakeClient(), make_cfg(daily_call_caps={"haiku": 0}), RV, THRESHOLD, lambda: NOW)
    assert st.stop_code == "daily_cap"
    st = score(s, FakeClient(), make_cfg(daily_cost_cap_usd=0.0), RV, THRESHOLD, lambda: NOW)
    assert st.stop_code == "daily_cost_cap"


# --- 8: stale verdicts at send time, card built from llm_verdict_id ---


def test_alert_stores_its_verdict_and_waits_when_the_text_changed_but_is_not_rescored(
    s, tmp_path, tg
):
    deps, step = scored(s, tmp_path, tg)
    sg = s.llm_verdicts(score_version())[0]
    select_candidates(s, deps.cfg, LATER)
    [a] = alerts(s)
    assert a["llm_verdict_id"] == s.llm_verdict_row_id(sg.job_id, score_version(), sg.input_hash)
    r = send_pending(
        s, tg.client(), deps.cfg, OWNER, LATER, kept_hashes={a["canonical_id"]: "text-changed"}
    )
    assert r.sent == 0 and tg.of("sendMessage") == []
    assert alerts(s)[0]["status"] == "pending"  # no verdict for the new text yet: not lost


def _new_verdict(s, sg, hash_, now, **kw):
    v = JobVerdict.model_validate(verdict_dict(**kw))
    with s.transaction():
        s.insert_llm_verdict(
            job_id=sg.job_id,
            verdict=v,
            prompt_version=score_version(),
            item_hash=hash_,
            call_id=None,
            model=None,
            cached=False,
            now=now,
        )


def test_changed_text_with_a_qualifying_new_verdict_rebinds_and_sends(s, tmp_path, tg):
    deps, step = scored(s, tmp_path, tg)
    sg = s.llm_verdicts(score_version())[0]
    select_candidates(s, deps.cfg, LATER)
    cid = alerts(s)[0]["canonical_id"]
    _new_verdict(s, sg, "h2", LATER, summary_ru="ОБНОВЛЁННОЕ ОПИСАНИЕ", fit=91)
    send_pending(s, tg.client(), deps.cfg, OWNER, LATER, kept_hashes={cid: "h2"})
    [msg] = tg.of("sendMessage")
    assert "ОБНОВЛЁННОЕ ОПИСАНИЕ" in msg["text"] and "Fit 91" in msg["text"]
    a = alerts(s)[0]
    assert a["status"] == "sent" and a["fit"] == 91
    assert s.verdict_by_id(a["llm_verdict_id"])[1] == "h2"


def test_pending_until_the_new_verdict_exists_then_sent_next_cycle(s, tmp_path, tg):
    deps, step = scored(s, tmp_path, tg)
    sg = s.llm_verdicts(score_version())[0]
    select_candidates(s, deps.cfg, LATER)
    cid = alerts(s)[0]["canonical_id"]
    send_pending(s, tg.client(), deps.cfg, OWNER, LATER, kept_hashes={cid: "h2"})
    assert tg.of("sendMessage") == [] and alerts(s)[0]["status"] == "pending"
    _new_verdict(s, sg, "h2", LATER + timedelta(minutes=1))
    send_pending(
        s, tg.client(), deps.cfg, OWNER, LATER + timedelta(minutes=10), kept_hashes={cid: "h2"}
    )
    assert len(tg.of("sendMessage")) == 1 and alerts(s)[0]["status"] == "sent"


def test_pending_that_never_gets_a_new_verdict_ends_as_backlog(s, tmp_path, tg):
    deps, step = scored(s, tmp_path, tg)
    select_candidates(s, deps.cfg, LATER)
    cid = alerts(s)[0]["canonical_id"]
    late = LATER + timedelta(hours=deps.cfg.alerts.max_alert_lag_h + 1)
    send_pending(s, tg.client(), deps.cfg, OWNER, late, kept_hashes={cid: "h2"})
    assert alerts(s)[0]["status"] == "skipped_backlog"


def test_changed_text_whose_new_verdict_no_longer_qualifies_is_skipped_stale(s, tmp_path, tg):
    deps, step = scored(s, tmp_path, tg)
    sg = s.llm_verdicts(score_version())[0]
    select_candidates(s, deps.cfg, LATER)
    cid = alerts(s)[0]["canonical_id"]
    _new_verdict(s, sg, "h2", LATER, fit=40)
    send_pending(s, tg.client(), deps.cfg, OWNER, LATER, kept_hashes={cid: "h2"})
    assert tg.of("sendMessage") == [] and alerts(s)[0]["status"] == "skipped_stale"


def test_group_no_longer_kept_is_skipped_stale(s, tmp_path, tg):
    deps, step = scored(s, tmp_path, tg)
    select_candidates(s, deps.cfg, LATER)
    send_pending(s, tg.client(), deps.cfg, OWNER, LATER, kept_hashes={})
    assert alerts(s)[0]["status"] == "skipped_stale"


# --- round 3: "chat not found" is systemic, identical 4xx for different messages too ---


def test_chat_not_found_never_burns_attempts_over_three_cycles(s, tmp_path, tg):
    deps, step = scored(s, tmp_path, tg, alerts={"max_attempts": 2})
    tg.queue("sendMessage", *[err(400, "Bad Request: CHAT NOT FOUND")] * 3)
    for i in range(3):
        step(s, LATER + timedelta(minutes=10 * i), CycleStats())
        a = alerts(s)[0]
        assert (a["status"], a["attempts"]) == ("pending", 0), i
    step(s, LATER + timedelta(minutes=40), CycleStats())
    assert alerts(s)[0]["status"] == "sent"


def test_same_4xx_for_two_different_messages_stops_the_cycle_without_counting(s, tmp_path, tg):
    deps, step = scored(s, tmp_path, tg, n=3)
    tg.queue("sendMessage", *[err(400, "Bad Request: something odd")] * 5)
    step(s, LATER, CycleStats())
    assert len(tg.of("sendMessage")) == 2  # the second identical rejection ends the cycle
    assert [(a["status"], a["attempts"]) for a in alerts(s)] == [("pending", 0)] * 3


def test_different_4xx_descriptions_are_counted_per_message(s, tmp_path, tg):
    deps, step = scored(s, tmp_path, tg, n=2)
    tg.queue("sendMessage", err(400, "Bad Request: A"), err(400, "Bad Request: B"))
    step(s, LATER, CycleStats())
    assert sorted(a["attempts"] for a in alerts(s)) == [1, 1]


def test_card_comes_from_the_alerts_own_verdict_not_a_newer_one(s, tmp_path, tg):
    deps, step = scored(s, tmp_path, tg)
    sg = s.llm_verdicts(score_version())[0]
    select_candidates(s, deps.cfg, LATER)
    newer = JobVerdict.model_validate(verdict_dict(summary_ru="НОВЫЙ ВЕРДИКТ", fit=95))
    with s.transaction():
        s.insert_llm_verdict(
            job_id=sg.job_id,
            verdict=newer,
            prompt_version=score_version(),
            item_hash="h2",
            call_id=None,
            model=None,
            cached=False,
            now=LATER + timedelta(minutes=1),
        )
    cid = alerts(s)[0]["canonical_id"]
    send_pending(s, tg.client(), deps.cfg, OWNER, LATER, kept_hashes={cid: sg.input_hash})
    [msg] = tg.of("sendMessage")
    assert "Контракт, удалённо." in msg["text"] and "НОВЫЙ" not in msg["text"]


@pytest.mark.parametrize(
    "patch",
    [{"eligible": False}, {"fit": 10}, {"red_flags": ["prompt_injection"]}],
)
def test_pending_alert_with_a_bad_verdict_is_skipped_stale(s, tmp_path, tg, patch):
    deps, step = scored(s, tmp_path, tg)
    select_candidates(s, deps.cfg, LATER)
    [a] = alerts(s)
    stored = s.verdict_by_id(a["llm_verdict_id"])[0].model_dump() | patch
    s.conn.execute(
        "UPDATE verdicts SET details_json = ? WHERE id = ?",
        (__import__("json").dumps({"verdict": stored}), a["llm_verdict_id"]),
    )
    send_pending(s, tg.client(), deps.cfg, OWNER, LATER)
    assert tg.of("sendMessage") == [] and alerts(s)[0]["status"] == "skipped_stale"


def test_alert_without_a_verdict_row_is_stale(s, tmp_path, tg):
    deps, step = scored(s, tmp_path, tg)
    select_candidates(s, deps.cfg, LATER)
    s.conn.execute("UPDATE alerts SET llm_verdict_id = NULL")
    send_pending(s, tg.client(), deps.cfg, OWNER, LATER)
    assert alerts(s)[0]["status"] == "skipped_stale"


# --- 12: eligible + prompt_injection inserted directly ---


def test_directly_inserted_injection_verdict_is_never_alerted(s, tmp_path, tg):
    ids = seed(s, 1)
    deps = deps_for(tmp_path, tg)
    deps.score_fn = lambda *a, **k: __import__("cvboost.score", fromlist=["x"]).ScoreStats()
    deps.notify = make_notify_step(deps, tg.client(), OWNER, TOKEN)
    group = s.kept_canonicals(RV, THRESHOLD)[0]
    job = s.job_for_llm(group.representative_id, RV)
    h = P.item_hash(P.render_body(job, group, deps.llm_cfg.scoring.description))
    bad = JobVerdict.model_validate(
        verdict_dict(eligible=True, fit=99, red_flags=["Prompt_Injection in text"])
    )
    with s.transaction():
        s.insert_llm_verdict(
            job_id=ids[0],
            verdict=bad,
            prompt_version=score_version(),
            item_hash=h,
            call_id=None,
            model=None,
            cached=False,
            now=NOW,
        )
    run(s, deps)
    assert tg.of("sendMessage") == [] and alerts(s) == []


# --- 10: cv -> in_progress -> undo -> cv ---


def test_cv_after_undo_while_in_progress_is_recorded_without_a_second_request(s):
    from tests.rules_helpers import add, sync

    sync(s, "a")
    j = add(s, "a", "1", company="X", title="Data Engineer")
    s.request_cv(j, channel="panel", now=NOW)
    s.conn.execute("UPDATE cv_requests SET status='in_progress'")
    s.record_decision(j, "undo", channel="panel", now=NOW + timedelta(minutes=1))
    assert s.conn.execute("SELECT status FROM cv_requests").fetchone()[0] == "in_progress"
    did, rid, created = s.request_cv(j, channel="panel", now=NOW + timedelta(minutes=2))
    assert (
        created is False and s.conn.execute("SELECT COUNT(*) FROM cv_requests").fetchone()[0] == 1
    )
    assert [r[0] for r in s.conn.execute("SELECT action FROM decisions ORDER BY id")] == [
        "cv_requested",
        "undo",
        "cv_requested",
    ]
    assert (
        s.latest_decisions([j])[j]["action"] == "cv_requested"
        and s.latest_decisions([j])[j]["id"] == did
    )


# --- 15: cycle book-keeping survives database errors ---


def test_cycle_survives_bookkeeping_write_errors(s, tmp_path, tg, monkeypatch):
    from tests.worker.helpers import FakeConnector, ScoreRecorder, make_deps

    deps = make_deps(tmp_path, [FakeConnector(ids=["1"])], score_fn=ScoreRecorder())
    s.sync_sources({"fake": FakeConnector}, NOW)

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Store, "start_cycle", locked)
    monkeypatch.setattr(Store, "finish_cycle", locked)
    monkeypatch.setattr(Store, "state_set", locked)
    st = run_cycle(s, deps)
    assert st.new_jobs == 1  # the pipeline itself ran


def test_worker_connection_waits_15s_for_locks(tmp_path):
    from cvboost.storage.db import connect

    conn = connect(tmp_path / "x.sqlite", busy_ms=15000)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
    assert connect(tmp_path / "y.sqlite").execute("PRAGMA busy_timeout").fetchone()[0] == 5000


# --- SEC-03: small getUpdates batches, 2 MB cap, retry with limit=1 ---


def test_get_updates_asks_for_5_and_falls_back_to_1_when_too_big():
    seen = []

    def handler(req):
        import json

        body = json.loads(req.content)
        seen.append(body["limit"])
        if body["limit"] == 5:
            return httpx.Response(200, content=b"x" * (T.UPDATES_MAX_BYTES + 10))
        return ok([{"update_id": 1}])

    fake = FakeTelegram()
    fake.handler = handler  # type: ignore[method-assign]
    assert fake.client().get_updates(None) == [{"update_id": 1}]
    assert seen == [5, 1]


def test_get_updates_gives_up_if_one_update_is_too_big():
    fake = FakeTelegram()
    fake.handler = lambda req: httpx.Response(200, content=b"x" * (T.UPDATES_MAX_BYTES + 10))  # type: ignore[method-assign]
    with pytest.raises(T.TelegramError) as ei:
        fake.client().get_updates(None)
    assert ei.value.kind == "ResponseTooLarge"


def test_other_methods_keep_the_256kb_cap():
    fake = FakeTelegram()
    fake.handler = lambda req: httpx.Response(200, content=b"x" * (300 * 1024))  # type: ignore[method-assign]
    with pytest.raises(T.TelegramError) as ei:
        fake.client().get_me()
    assert ei.value.kind == "ResponseTooLarge"


# --- SEC-04: redact, then truncate ---


def test_token_straddling_the_truncation_point_is_still_redacted():
    desc = "y" * (T.MAX_DESCRIPTION - 20) + TOKEN + " tail"
    e = T.TelegramError("sendMessage", kind="ApiError", status=400, description=desc, token=TOKEN)
    assert TOKEN not in str(e) and TOKEN[:12] not in str(e)
    # without the exact token (e.g. a different bot's) the shape rule still catches it
    e = T.TelegramError("sendMessage", kind="ApiError", status=400, description=desc)
    assert TOKEN[:12] not in str(e)


# --- SEC-06 / compose, SEC-07 locks ---


def test_compose_worker_is_buildable_hardened_and_given_time_to_stop():
    c = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    w = c["services"]["worker"]
    assert w["build"] == "." and w["image"] == c["services"]["app"]["image"]
    assert w["cap_drop"] == ["ALL"] and "no-new-privileges:true" in w["security_opt"]
    grace = int(str(w["stop_grace_period"]).rstrip("s"))
    llm = yaml.safe_load((ROOT / "tests" / "fixtures" / "config" / "llm.yaml").read_text())
    assert grace >= llm["timeout_s"] + 35


def test_shipped_llm_caps_are_the_owner_defaults():
    llm = yaml.safe_load((ROOT / "tests" / "fixtures" / "config" / "llm.yaml").read_text())
    assert llm["daily_call_caps"]["haiku"] == 200 and llm["daily_cost_cap_usd"] == 6.0


def test_lock_file_is_private_and_a_symlink_is_refused(tmp_path):
    p = tmp_path / "w.lock"
    h = try_lock(p)
    assert h is not None and (os.stat(p).st_mode & 0o777) == 0o600
    h.release()
    target = tmp_path / "victim"
    target.write_text("keep me")
    link = tmp_path / "evil.lock"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="cannot open lock file"):
        try_lock(link)
    assert target.read_text() == "keep me"
