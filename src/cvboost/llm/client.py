"""LLMClient interface. One backend today: `claude_cli` (subprocess, owner's subscription)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from cvboost.llm.config import LLMConfig

# LLMResult.status values written to llm_calls.status
OK = "ok"
INVALID_OUTPUT = "invalid_output"
ERROR = "error"
TIMEOUT = "timeout"
USAGE_LIMIT = "usage_limit"
AUTH_ERROR = "auth_error"
BUDGET = "budget"
REFUSED_AUTH_METHOD = "refused_auth_method"


@dataclass(frozen=True)
class LLMRequest:
    model_alias: str
    system_prompt: str
    json_schema: dict[str, Any]
    task: str
    stdin_text: str
    timeout_s: int
    max_budget_usd: float | None = None


@dataclass(frozen=True)
class LLMResult:
    status: str
    structured: dict[str, Any] | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    total_cost_usd: float | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    error_message: str | None = None
    credential_expires_at: str | None = None


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    status: str = OK  # OK | AUTH_ERROR | REFUSED_AUTH_METHOD | ERROR
    auth_method: str | None = None
    message: str | None = None
    credential_expires_at: str | None = field(default=None)


class LLMClient(Protocol):
    def run(self, req: LLMRequest) -> LLMResult: ...

    def preflight(self) -> PreflightResult: ...


def make_client(cfg: LLMConfig) -> LLMClient:
    if cfg.backend != "claude_cli":
        raise ValueError(f"unsupported LLM backend {cfg.backend!r} (only claude_cli)")
    from cvboost.llm.claude_cli import ClaudeCliBackend

    return ClaudeCliBackend(cfg)
