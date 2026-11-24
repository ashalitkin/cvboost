"""Plan test 1, 7, 12-ish: scored groups -> Telegram messages, once."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from cvboost.notify.wiring import make_notify_step
from cvboost.score import score
from cvboost.storage.db import Store, connect, migrate
from cvboost.worker import run_cycle
from tests.conftest import NOW
from tests.llm.helpers import FakeClient, make_cfg, ok_result, seed
from tests.notify.helpers import OWNER, TOKEN, FakeTelegram
from tests.worker.helpers import FakeClock, FakeConnector, make_deps


@pytest.fixture
def s(tmp_path):
    conn = connect(tmp_path / "n.sqlite")
    migrate(conn)
    st = Store(conn)
    st.state_set("alerts_since", (NOW - timedelta(hours=1)).isoformat(), NOW)
    return st


def deps_for(tmp_path, tg, fits=None, **cfg):
    fits = fits or {}
    client = FakeClient(
        lambda req: ok_result(req, {r: {"fit": fits.get(r, 80)} for r in _refs(req)})
    )
    deps = make_deps(
        tmp_path,
        [FakeConnector("fake")],
        clock=FakeClock(NOW + timedelta(minutes=5)),
        cfg=cfg or {},
    )
    deps.llm_cfg = make_cfg()
    deps.llm_client = client
    deps.score_fn = lambda *a, **k: score(*a, **{**k, "max_calls": 5})  # a[5] is deps.clock
    deps.notify = make_notify_step(deps, tg.client(), OWNER, TOKEN)
    return deps


def _refs(req):
    from tests.llm.helpers import refs_of

    return refs_of(req)


def run(s, deps):
    s.sync_sources({"fake": type(deps.connectors[0])}, NOW)
    return run_cycle(s, deps)


def test_two_suitable_two_messages_then_zero(s, tmp_path):
    seed(s, 3)
    tg = FakeTelegram()
    # order of refs in the batch is newest first_seen first; make exactly one group fit 40
    deps = deps_for(tmp_path, tg, fits={"j2": 40})
    st = run(s, deps)
    sends = tg.of("sendMessage")
    assert len(sends) == 2 and st.alerts_sent == 2
    for p in sends:
        assert "parse_mode" not in p and p["chat_id"] == OWNER
        assert p["link_preview_options"] == {"is_disabled": True}
        buttons = p["reply_markup"]["inline_keyboard"][0]
        assert [b["text"] for b in buttons] == ["Interested", "No", "Prepare CV"]
        assert all(len(b["callback_data"].encode()) <= 64 for b in buttons)
    rows = s.conn.execute("SELECT status, message_id, text, chat_id FROM alerts").fetchall()
    assert sorted(r["status"] for r in rows) == ["sent", "sent"]
    assert all(r["message_id"] and r["text"] and r["chat_id"] == OWNER for r in rows)
    deps.clock.advance(minutes=11)
    run(s, deps)
    assert len(tg.of("sendMessage")) == 2  # nothing new


def test_more_than_max_per_cycle_goes_into_one_summary(s, tmp_path):
    seed(s, 7)
    tg = FakeTelegram()
    st = run(s, deps_for(tmp_path, tg))
    sends = tg.of("sendMessage")
    assert len(sends) == 6 and st.alerts_sent == 5 and st.alerts_summarized == 2
    summary = sends[-1]
    assert "reply_markup" not in summary and summary["text"].startswith("2 more matches")
    assert (
        s.conn.execute("SELECT COUNT(*) FROM alerts WHERE status='summarized'").fetchone()[0] == 2
    )


def test_jobs_seen_before_alerts_since_stay_in_the_panel(s, tmp_path):
    seed(s, 2)
    s.state_set("alerts_since", (NOW + timedelta(hours=1)).isoformat(), NOW)
    tg = FakeTelegram()
    run(s, deps_for(tmp_path, tg))
    assert tg.of("sendMessage") == []
    assert (
        s.conn.execute("SELECT COUNT(*) FROM alerts WHERE status='skipped_backlog'").fetchone()[0]
        == 2
    )


def test_older_than_lag_is_backlog(s, tmp_path):
    seed(s, 1)
    s.state_set("alerts_since", (NOW - timedelta(days=30)).isoformat(), NOW)
    tg = FakeTelegram()
    deps = deps_for(tmp_path, tg)
    deps.clock = FakeClock(NOW + timedelta(hours=49))
    run(s, deps)
    assert tg.of("sendMessage") == []
    assert s.conn.execute("SELECT status FROM alerts").fetchone()[0] in (
        "skipped_backlog",
        "skipped_closed",
    )


def test_quiet_hours_hold_alerts_then_release(s, tmp_path):
    seed(s, 2)
    tg = FakeTelegram()
    quiet = {"alerts": {"quiet_hours": {"start": "11:00", "end": "14:00"}}}  # NOW is 12:05 UTC
    deps = deps_for(tmp_path, tg, **quiet)
    run(s, deps)
    assert tg.of("sendMessage") == []
    assert s.conn.execute("SELECT COUNT(*) FROM alerts WHERE status='pending'").fetchone()[0] == 2
    deps.clock.advance(hours=3)
    run(s, deps)
    assert len(tg.of("sendMessage")) == 2


def test_group_merged_after_alert_is_not_alerted_again(s, tmp_path):
    ids = seed(s, 1)
    tg = FakeTelegram()
    deps = deps_for(tmp_path, tg)
    run(s, deps)
    assert len(tg.of("sendMessage")) == 1
    # a second member joins the alerted group under a new canonical id
    from tests.rules_helpers import add, sync

    sync(s, "other")
    other = add(
        s, "other", "x", company="CompanyA", title="Senior Analytics Engineer (dbt, BigQuery)"
    )
    s.conn.execute("UPDATE jobs SET canonical_id = ? WHERE id = ?", (other, other))
    s.conn.execute("UPDATE jobs SET canonical_id = ? WHERE id = ?", (other, ids[0]))
    deps.clock.advance(minutes=11)
    run(s, deps)
    assert len(tg.of("sendMessage")) == 1


def test_expired_members_are_hidden_and_all_expired_means_no_card(s, tmp_path):
    from tests.rules_helpers import sync

    seed(s, 2)
    # job 1 (CompanyA) is a Himalayas posting that expired; job 2 stays alive
    past = int((NOW - timedelta(days=1)).timestamp())
    for jid, (src, raw) in {1: ("himalayas", {"expiryDate": past}), 2: ("wwr", {})}.items():
        sync(s, src)
        s.conn.execute(
            "UPDATE jobs SET source=?, raw_json=? WHERE id=?", (src, json.dumps(raw), jid)
        )
    tg = FakeTelegram()
    run(s, deps_for(tmp_path, tg))
    sends = tg.of("sendMessage")
    assert len(sends) == 1 and "CompanyB" in sends[0]["text"]
    assert (
        s.conn.execute("SELECT COUNT(*) FROM alerts WHERE status='skipped_closed'").fetchone()[0]
        == 1
    )


def test_card_hides_expired_member_link_but_keeps_live_one(s, tmp_path):
    from tests.rules_helpers import add, sync

    seed(s, 1)
    past = int((NOW - timedelta(days=1)).timestamp())
    sync(s, "himalayas")
    h = add(
        s,
        "himalayas",
        "h1",
        company="CompanyA",
        title="Senior Analytics Engineer (dbt, BigQuery)",
        raw={"expiryDate": past},
    )
    s.conn.execute("UPDATE jobs SET canonical_id = 1 WHERE id = ?", (h,))
    sync(s, "wwr")
    s.conn.execute(
        "UPDATE jobs SET source='wwr', url='https://weworkremotely.com/remote-jobs/a' WHERE id=1"
    )
    s.conn.execute("UPDATE jobs SET url='https://himalayas.app/jobs/h1' WHERE id=?", (h,))
    tg = FakeTelegram()
    run(s, deps_for(tmp_path, tg))
    [msg] = tg.of("sendMessage")
    assert "himalayas" not in msg["text"]
    assert "wwr: https://weworkremotely.com/remote-jobs/a" in msg["text"]


def test_telegram_failure_is_retried_then_given_up(s, tmp_path):
    from tests.notify.helpers import err

    seed(s, 1)
    tg = FakeTelegram()
    tg.queue("sendMessage", *[err(400, "Bad Request: message text is empty")] * 5)
    deps = deps_for(tmp_path, tg, alerts={"max_attempts": 2})
    run(s, deps)
    row = s.conn.execute("SELECT status, attempts, last_error FROM alerts").fetchone()
    assert (row["status"], row["attempts"]) == ("pending", 1) and "text is empty" in row[
        "last_error"
    ]
    deps.clock.advance(minutes=11)
    run(s, deps)
    row = s.conn.execute("SELECT status, attempts FROM alerts").fetchone()
    assert (row["status"], row["attempts"]) == ("failed", 2)


def test_rate_limit_pauses_and_keeps_alerts_pending(s, tmp_path):
    from cvboost.notify.alerts import send_pending
    from tests.notify.helpers import err

    seed(s, 2)
    tg = FakeTelegram()
    deps = deps_for(tmp_path, tg)
    deps.notify = None
    run(s, deps)  # score only
    from cvboost.notify.alerts import select_candidates

    select_candidates(s, deps.cfg, NOW + timedelta(minutes=5))
    tg.queue("sendMessage", err(429, "Too Many Requests: retry after 7", retry_after=7))
    slept = []
    r = send_pending(
        s, tg.client(), deps.cfg, OWNER, NOW + timedelta(minutes=5), sleep=slept.append
    )
    assert slept == [7] and r.sent == 0 and "rate limited" in r.stopped
    assert s.conn.execute("SELECT COUNT(*) FROM alerts WHERE status='pending'").fetchone()[0] == 2
    assert s.conn.execute("SELECT SUM(attempts) FROM alerts").fetchone()[0] == 0


def test_revoked_token_stops_sending(s, tmp_path):
    from cvboost.notify.alerts import select_candidates, send_pending
    from tests.notify.helpers import err

    seed(s, 2)
    tg = FakeTelegram()
    deps = deps_for(tmp_path, tg)
    deps.notify = None
    run(s, deps)
    select_candidates(s, deps.cfg, NOW + timedelta(minutes=5))
    tg.queue("sendMessage", err(401, "Unauthorized"))
    r = send_pending(s, tg.client(), deps.cfg, OWNER, NOW + timedelta(minutes=5))
    assert r.sent == 0 and "refused" in r.stopped
    assert len(tg.of("sendMessage")) == 1  # did not hammer the API


def test_prompt_injection_verdict_is_never_alerted(s, tmp_path):
    seed(s, 2)
    tg = FakeTelegram()
    deps = deps_for(tmp_path, tg)
    deps.llm_client = FakeClient(
        lambda req: ok_result(req, {"j1": {"red_flags": ["Prompt_Injection attempt"]}})
    )
    run(s, deps)
    sends = tg.of("sendMessage")
    assert len(sends) == 1
    assert s.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1  # no row for the other


def test_stale_verdict_waits_for_rescoring(s, tmp_path):
    from cvboost.notify.alerts import select_candidates

    seed(s, 1)
    tg = FakeTelegram()
    deps = deps_for(tmp_path, tg)
    deps.notify = None
    run(s, deps)  # scored, not alerted
    cid = s.conn.execute("SELECT canonical_id FROM jobs").fetchone()[0]
    sg = s.llm_verdicts(__import__("cvboost.llm.prompts.registry", fromlist=["x"]).score_version())[
        0
    ]
    later = NOW + timedelta(minutes=5)
    assert select_candidates(s, deps.cfg, later, kept_hashes={cid: "changed"}).pending == 0
    assert select_candidates(s, deps.cfg, later, kept_hashes={cid: sg.input_hash}).pending == 1
