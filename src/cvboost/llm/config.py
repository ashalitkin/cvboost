"""config/llm.yaml: models, caps, pauses, scoring parameters (separate from rules.yaml)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LimitPause(_Strict):
    first_minutes: int = Field(30, ge=1)
    max_minutes: int = Field(300, ge=1)
    auth_minutes: int = Field(60, ge=1)


class DescriptionCfg(_Strict):
    max_chars: int = Field(6000, ge=500)
    head_chars: int = Field(4500, ge=100)
    tail_chars: int = Field(1500, ge=100)

    @model_validator(mode="after")
    def _fits(self) -> DescriptionCfg:
        if self.head_chars + self.tail_chars > self.max_chars:
            raise ValueError("head_chars + tail_chars must not exceed max_chars")
        return self


class ScoringCfg(_Strict):
    model_role: str = "cheap"
    batch_size: int = Field(8, ge=1, le=10)
    max_calls_per_run: int = Field(3, ge=1)
    max_age_days: int = Field(30, ge=1)
    description: DescriptionCfg = DescriptionCfg()


class CvCfg(_Strict):
    enabled: bool = True
    model_role: str = "strong"
    timeout_s: int = Field(300, ge=30)
    max_budget_usd_per_call: float | None = 1.5
    max_attempts: int = Field(2, ge=1, le=3)
    description: DescriptionCfg = DescriptionCfg(max_chars=12000, head_chars=9000, tail_chars=3000)


class EmailExtractCfg(_Strict):
    """Haiku fallback for alert / recruiter e-mails the deterministic parsers cannot read (#17)."""

    enabled: bool = True
    model_role: str = "cheap"
    daily_calls: int = Field(30, ge=0)  # owner decision 2026-11-08: up to 30 of haiku's 200
    max_calls_per_cycle: int = Field(1, ge=1)
    batch_size: int = Field(3, ge=1, le=5)
    pending_ttl_days: int = Field(3, ge=1)  # a mail waiting longer is dropped (status expired)


class LLMConfig(_Strict):
    backend: Literal["claude_cli"] = "claude_cli"
    claude_bin: str = "claude"
    # 'api_key' is refused regardless of what is listed here
    allowed_auth_methods: list[str] = ["claude.ai", "oauth_token"]
    models: dict[str, str] = {"cheap": "haiku", "strong": "opus"}
    timezone: str = "UTC"  # config/llm.yaml says where the owner lives
    timeout_s: int = Field(180, ge=5)
    max_budget_usd_per_call: float | None = 0.25
    daily_call_caps: dict[str, int] = {"haiku": 60, "opus": 10}
    daily_cost_cap_usd: float = 2.0
    limit_pause: LimitPause = LimitPause()
    scoring: ScoringCfg = ScoringCfg()
    cv: CvCfg = CvCfg()
    email_extract: EmailExtractCfg = EmailExtractCfg()

    @model_validator(mode="after")
    def _check(self) -> LLMConfig:
        if self.scoring.model_role not in self.models:
            raise ValueError(f"scoring.model_role {self.scoring.model_role!r} not in models")
        if self.cv.model_role not in self.models:
            raise ValueError(f"cv.model_role {self.cv.model_role!r} not in models")
        if self.email_extract.model_role not in self.models:
            raise ValueError(
                f"email_extract.model_role {self.email_extract.model_role!r} not in models"
            )
        self.allowed_auth_methods = [m for m in self.allowed_auth_methods if m != "api_key"]
        return self

    @property
    def score_alias(self) -> str:
        return self.models[self.scoring.model_role]

    @property
    def email_alias(self) -> str:
        return self.models[self.email_extract.model_role]

    @property
    def cv_alias(self) -> str:
        return self.models[self.cv.model_role]


def load_llm_config(path: Path | str) -> LLMConfig:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return LLMConfig.model_validate(data or {})
    except (yaml.YAMLError, ValidationError, OSError) as exc:
        raise ValueError(f"bad LLM config {path}: {exc}") from exc
