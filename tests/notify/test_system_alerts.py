from __future__ import annotations

from datetime import timedelta

from cvboost.llm import client as C
from cvboost.notify.alerts import queue_system_alerts, send_pending, worker_started_alert
from cvboost.worker_config import WorkerConfig
from tests.conftest import NOW
from tests.notify.helpers import OWNER

CFG = WorkerConfig()


def llm_call(s, status, *, pause_hours=1, expires=None, day="2026-11-06"):
    s.insert_llm_call(
        kind="score",
        model_alias="haiku",
        prompt_version="v",
        n_items=1,
        attempt=1,
        now=NOW,
        local_day=day,
        status=status,
        pause_until=NOW + timedelta(hours=pause_hours) if pause_hours else None,
        credential_expires_at=expires,
    )


def kinds(s):
    return [r["kind"] for r in s.conn.execute("SELECT kind FROM system_alerts ORDER BY id")]


def test_auth_error_once_then_silent(s, tg):
    llm_call(s, C.AUTH_ERROR)
    assert queue_system_alerts(s, CFG, NOW, score_code=None) == 1
    r = send_pending(s, tg.client(), CFG, OWNER, NOW)
    assert r.system == 1
    [msg] = tg.of("sendMessage")
    assert "Claude authorisation error" in msg["text"] and "CLAUDE_CODE_OAUTH_TOKEN" in msg["text"]
    # next cycles: same pause, nothing new, nothing sent
    later = NOW + timedelta(minutes=10)
    assert queue_system_alerts(s, CFG, later, score_code=None) == 0
    send_pending(s, tg.client(), CFG, OWNER, later)
    assert len(tg.of("sendMessage")) == 1
    assert s.conn.execute("SELECT status FROM system_alerts").fetchone()[0] == "sent"


def test_refused_auth_method_is_an_auth_alert_and_expired_pause_is_ignored(s):
    llm_call(s, C.REFUSED_AUTH_METHOD)
    queue_system_alerts(s, CFG, NOW, score_code=None)
    assert kinds(s) == ["llm_auth"]
    queue_system_alerts(s, CFG, NOW + timedelta(hours=3), score_code=None)
    assert kinds(s) == ["llm_auth"]  # the pause ended: no second alert
    # a pause that already ended raises nothing new on a fresh day
    s.conn.execute("DELETE FROM system_alerts")
    assert queue_system_alerts(s, CFG, NOW + timedelta(days=2), score_code=None) == 0


def test_usage_limit_once_a_day_even_if_the_pause_moves(s):
    llm_call(s, C.USAGE_LIMIT, pause_hours=1)
    queue_system_alerts(s, CFG, NOW, score_code=None)
    llm_call(s, C.USAGE_LIMIT, pause_hours=2)
    queue_system_alerts(s, CFG, NOW + timedelta(minutes=30), score_code=None)
    assert kinds(s) == ["llm_usage_limit"]


def test_credential_expiring_daily(s):
    llm_call(
        s, C.OK, pause_hours=0, expires=(NOW + timedelta(days=3)).isoformat().replace("+00:00", "Z")
    )
    assert queue_system_alerts(s, CFG, NOW, score_code=None) == 1
    assert queue_system_alerts(s, CFG, NOW + timedelta(hours=5), score_code=None) == 0
    assert queue_system_alerts(s, CFG, NOW + timedelta(days=1), score_code=None) == 1
    s.conn.execute("DELETE FROM llm_calls")
    llm_call(
        s,
        C.OK,
        pause_hours=0,
        expires=(NOW + timedelta(days=30)).isoformat().replace("+00:00", "Z"),
    )
    assert queue_system_alerts(s, CFG, NOW + timedelta(days=2), score_code=None) == 0


def test_daily_cap_once_a_day(s):
    stop = "daily cap haiku 60/60 reached"
    assert queue_system_alerts(s, CFG, NOW, score_code="daily_cap", score_text=stop) == 1
    assert (
        queue_system_alerts(
            s, CFG, NOW + timedelta(hours=1), score_code="daily_cap", score_text=stop
        )
        == 0
    )
    assert (
        queue_system_alerts(
            s, CFG, NOW, score_code="run_cap", score_text="per-run cap 1 reached; 3 groups left"
        )
        == 0
    )
    assert (
        queue_system_alerts(
            s, CFG, NOW + timedelta(days=1), score_code="daily_cap", score_text=stop
        )
        == 1
    )


def test_source_failing_once_per_episode(s):
    from cvboost.connectors import REGISTRY

    s.sync_sources({"remoteok": REGISTRY["remoteok"]}, NOW)
    for i in range(6):
        rid = s.start_run("remoteok", NOW + timedelta(minutes=i))
        s.finish_run(
            rid,
            status="error",
            found=0,
            new=0,
            invalid=0,
            error_type="FetchError",
            error_message="HTTP 500 after 3 attempts",
            now=NOW + timedelta(minutes=i),
        )
    assert queue_system_alerts(s, CFG, NOW, score_code=None) == 1
    assert queue_system_alerts(s, CFG, NOW + timedelta(hours=1), score_code=None) == 0
    rid = s.start_run("remoteok", NOW + timedelta(hours=2))
    s.finish_run(rid, status="ok", found=1, new=0, invalid=0, now=NOW + timedelta(hours=2))
    for i in range(6):
        rid = s.start_run("remoteok", NOW + timedelta(hours=3, minutes=i))
        s.finish_run(
            rid,
            status="error",
            found=0,
            new=0,
            invalid=0,
            error_type="X",
            error_message="e",
            now=NOW + timedelta(hours=3, minutes=i),
        )
    assert (
        queue_system_alerts(s, CFG, NOW + timedelta(hours=4), score_code=None) == 1
    )  # new episode


def test_worker_started_once_per_hour_and_quiet_hours_hold_system_alerts(s, tg):
    worker_started_alert(s, CFG, NOW)
    worker_started_alert(s, CFG, NOW + timedelta(minutes=5))
    assert kinds(s) == ["worker_started"]
    worker_started_alert(s, CFG, NOW + timedelta(hours=1))
    assert len(kinds(s)) == 2
    quiet = WorkerConfig.model_validate(
        {"alerts": {"quiet_hours": {"start": "11:00", "end": "14:00"}}}
    )
    assert send_pending(s, tg.client(), quiet, OWNER, NOW).system == 0
    assert send_pending(s, tg.client(), CFG, OWNER, NOW).system == 2


# --- #70b: the zone in a pause alert comes from the worker config, not "(Лиссабон)" ---------


def _pause_text(s, tz, now, status=C.USAGE_LIMIT):
    s.insert_llm_call(
        kind="score",
        model_alias="haiku",
        prompt_version="v",
        n_items=1,
        attempt=1,
        now=now,
        local_day="2026-01-01",
        status=status,
        pause_until=now + timedelta(hours=2),
    )
    cfg = WorkerConfig(timezone=tz)
    assert queue_system_alerts(s, cfg, now, score_code=None) == 1
    return s.conn.execute("SELECT text FROM system_alerts").fetchone()[0]


def test_pause_alert_uses_the_configured_zone_winter_and_summer(s):
    from datetime import UTC, datetime

    winter = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
    assert "until 13:00 (Europe/Madrid)" in _pause_text(s, "Europe/Madrid", winter)  # UTC+1
    s.conn.execute("DELETE FROM system_alerts")
    s.conn.execute("DELETE FROM llm_calls")
    summer = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    assert "until 14:00 (Europe/Madrid)" in _pause_text(s, "Europe/Madrid", summer)  # UTC+2 (CEST)


def test_pause_alert_other_zones_and_default_utc(s):
    from datetime import UTC, datetime

    now = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
    text = _pause_text(s, "America/New_York", now)
    assert "until 07:00 (America/New_York)" in text  # UTC-5 in January
    s.conn.execute("DELETE FROM system_alerts")
    s.conn.execute("DELETE FROM llm_calls")
    text = _pause_text(s, "UTC", now)
    assert "until 12:00 (UTC)" in text
    assert WorkerConfig().timezone == "UTC"


def test_auth_pause_alert_has_zone_and_no_russian(s):
    from datetime import UTC, datetime

    now = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
    text = _pause_text(s, "Europe/Madrid", now, status=C.AUTH_ERROR)
    assert "no earlier than 13:00 (Europe/Madrid)" in text
    assert "Лиссабон" not in text and not any("Ѐ" <= ch <= "ӿ" for ch in text)


def test_old_russian_alert_of_the_same_day_blocks_a_second_one_on_deploy_day(s):
    # the alert written by the pre-70b worker today: same (kind, day) key, Russian text
    s.insert_system_alert(
        "llm_usage_limit",
        "2026-11-06",
        "Лимит подписки Claude исчерпан. LLM-оценка на паузе до 21:00 (Лиссабон). "
        "Сбор продолжается.",
        NOW,
    )
    llm_call(s, C.USAGE_LIMIT, pause_hours=1)
    assert queue_system_alerts(s, CFG, NOW, score_code=None) == 0
    assert kinds(s) == ["llm_usage_limit"]
    text = s.conn.execute("SELECT text FROM system_alerts").fetchone()[0]
    assert "Лиссабон" in text  # the old row is kept as it was, nothing is rewritten
