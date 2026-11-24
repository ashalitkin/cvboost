from __future__ import annotations

from pathlib import Path

import pytest

from cvboost.config import Settings
from cvboost.worker_config import WorkerConfig, load_worker_config

SHIPPED = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "config" / "worker.yaml"


def test_shipped_config_loads_with_expected_defaults():
    cfg = load_worker_config(SHIPPED)
    assert cfg.tick_minutes == 10 and cfg.alerts.fit_threshold == 60
    # quiet hours 23:00-08:00 (local time)
    assert cfg.alerts.quiet_hours is not None
    assert (cfg.alerts.quiet_hours.start, cfg.alerts.quiet_hours.end) == ("23:00", "08:00")
    assert cfg.score_max_calls_per_cycle == 1
    # everything except the quiet hours matches the code defaults
    assert cfg.timezone == "UTC"
    defaults = WorkerConfig(timezone="UTC")
    assert (
        cfg.model_copy(update={"alerts": cfg.alerts.model_copy(update={"quiet_hours": None})})
        == defaults
    )


@pytest.mark.parametrize(
    "text",
    [
        "tick_minutes: 0",
        "unknown_key: 1",
        "timezone: Mars/Base",
        "backfill: {window: ['2am', '06:00']}",
        "alerts: {quiet_hours: {start: '25:00', end: '08:00'}}",
        "collect_budget_s: 600\ncycle_deadline_s: 540",
    ],
)
def test_bad_config_is_a_value_error(tmp_path, text):
    p = tmp_path / "w.yaml"
    p.write_text(text)
    with pytest.raises(ValueError, match="bad worker config"):
        load_worker_config(p)


def test_settings_from_env_never_shows_the_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:FAKE_TOKEN_for_tests_0000000000000000")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("CVBOOST_DB", "/data/x/cvboost.sqlite")
    s = Settings.from_env()
    assert s.telegram_chat_id == 42 and s.telegram_token
    assert "FAKE_TOKEN" not in repr(s) and "FAKE_TOKEN" not in str(s)
    assert s.data_dir == Path("/data/x")
