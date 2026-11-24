"""config/worker.yaml: loop timing, backfill window, alert thresholds."""

from __future__ import annotations

import re
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_hhmm(v: str) -> str:
    if not _HHMM.match(v):
        raise ValueError(f"{v!r} is not HH:MM")
    return v


class Backfill(_Strict):
    enabled: bool = True
    window: tuple[str, str] = ("02:00", "06:00")
    calls_per_cycle: int = Field(4, ge=1)
    max_calls_per_night: int = Field(20, ge=0)

    @field_validator("window")
    @classmethod
    def _window(cls, v: tuple[str, str]) -> tuple[str, str]:
        return (_check_hhmm(v[0]), _check_hhmm(v[1]))


class QuietHours(_Strict):
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def _t(cls, v: str) -> str:
        return _check_hhmm(v)


class AlertsCfg(_Strict):
    fit_threshold: int = Field(60, ge=0, le=100)
    max_per_cycle: int = Field(5, ge=1)
    max_age_days: int = Field(21, ge=1)
    max_alert_lag_h: int = Field(48, ge=1)
    max_attempts: int = Field(5, ge=1)
    quiet_hours: QuietHours | None = None
    summary_max_lines: int = Field(15, ge=1)
    credential_warn_days: int = Field(7, ge=1)
    source_fail_alert_after: int = Field(6, ge=1)


class TelegramCfg(_Strict):
    poll_timeout_s: int = Field(25, ge=1, le=50)


class WorkerConfig(_Strict):
    timezone: str = "UTC"  # config/worker.yaml says where the owner lives
    tick_minutes: int = Field(10, ge=1)
    collect_budget_s: int = Field(360, ge=10)
    cycle_deadline_s: int = Field(540, ge=30)
    score_max_calls_per_cycle: int = Field(1, ge=1)
    backfill: Backfill = Backfill()
    alerts: AlertsCfg = AlertsCfg()
    telegram: TelegramCfg = TelegramCfg()

    @model_validator(mode="after")
    def _check(self) -> WorkerConfig:
        ZoneInfo(self.timezone)  # raises on an unknown zone
        if self.collect_budget_s >= self.cycle_deadline_s:
            raise ValueError("collect_budget_s must be below cycle_deadline_s")
        return self


def load_worker_config(path: Path | str) -> WorkerConfig:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return WorkerConfig.model_validate(data or {})
    except (yaml.YAMLError, ValidationError, OSError, ValueError, KeyError) as exc:
        raise ValueError(f"bad worker config {path}: {exc}") from exc
