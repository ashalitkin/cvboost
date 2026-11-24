"""`claude -p` backend: headless Claude Code on the owner's subscription.

Hard rules: never ANTHROPIC_API_KEY (env whitelist + assert, preflight refuses api_key auth),
never `--bare` (it ignores OAuth/keychain), no tools, empty temp cwd, job text only via stdin.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from cvboost.llm import client as C
from cvboost.llm.client import LLMRequest, LLMResult, PreflightResult
from cvboost.llm.config import LLMConfig

ENV_WHITELIST = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ", "TMPDIR")
ENV_OPTIONAL_SECRET = "CLAUDE_CODE_OAUTH_TOKEN"
ENV_CONSTANTS = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
FORBIDDEN_EXACT = frozenset(
    {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDECODE"}
    | {"CLAUDE_CODE_ENTRYPOINT"}
)
FORBIDDEN_PREFIXES = ("CVBOOST_", "TELEGRAM_", "ANTHROPIC_", "CLAUDE_CODE_SESSION")

LIMIT_RE = re.compile(
    r"(?i)usage limit|hit your (?:usage )?limit|limit (?:reached|will reset)"
    r"|out of (?:extra )?usage|resets? (?:at |in )?\d"
)
AUTH_RE = re.compile(
    r"(?i)not logged in|please run /login|invalid (?:api key|bearer|oauth)"
    r"|oauth token (?:has )?expired|authentication (?:failed|error)|unauthori[sz]ed"
)


def minimal_env(src: Mapping[str, str], claude_path: str) -> dict[str, str]:
    env = {k: src[k] for k in ENV_WHITELIST if k in src}
    env["PATH"] = f"{Path(claude_path).parent}:/usr/bin:/bin"
    if src.get(ENV_OPTIONAL_SECRET):
        env[ENV_OPTIONAL_SECRET] = src[ENV_OPTIONAL_SECRET]
    env.update(ENV_CONSTANTS)
    for k in env:
        assert k not in FORBIDDEN_EXACT and not k.startswith(FORBIDDEN_PREFIXES), k
    return env


def _int(d: Mapping[str, Any], key: str) -> int | None:
    v = d.get(key)
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def classify(returncode: int | None, stdout: str, stderr: str) -> LLMResult:
    """Map the CLI's JSON envelope to an LLMResult. The exit code is recorded, never trusted."""
    err_tail = (stderr or "")[:300]
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict):
        # no envelope: a limit or login notice may still be in the plain text
        text = f"{stderr or ''}\n{(stdout or '')[:300]}"
        status = C.ERROR
        if LIMIT_RE.search(text):
            status = C.USAGE_LIMIT
        elif AUTH_RE.search(text):
            status = C.AUTH_ERROR
        msg = (err_tail or (stdout or "")[:300] or "bad json")[:300]
        return LLMResult(status=status, exit_code=returncode, error_message=msg)

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    mu = data.get("modelUsage")
    cost = data.get("total_cost_usd")
    common: dict[str, Any] = {
        "model": next(iter(mu)) if isinstance(mu, dict) and mu else None,
        "input_tokens": _int(usage, "input_tokens"),
        "output_tokens": _int(usage, "output_tokens"),
        "cache_read_tokens": _int(usage, "cache_read_input_tokens"),
        "cache_creation_tokens": _int(usage, "cache_creation_input_tokens"),
        "total_cost_usd": float(cost) if isinstance(cost, int | float) else None,
        "duration_ms": _int(data, "duration_ms"),
        "exit_code": returncode,
        "credential_expires_at": data.get("credential_expires_at")
        if isinstance(data.get("credential_expires_at"), str)
        else None,
    }
    is_error = data.get("is_error")
    result_text = data.get("result") if isinstance(data.get("result"), str) else ""
    if is_error is False:
        so = data.get("structured_output")
        if isinstance(so, dict):
            return LLMResult(status=C.OK, structured=so, **common)
        return LLMResult(status=C.INVALID_OUTPUT, error_message="no structured_output", **common)
    if is_error is True:
        status_code = data.get("api_error_status")
        if (
            status_code == 429 and data.get("api_error") == "usage_limit_reached"
        ) or LIMIT_RE.search(result_text):
            status = C.USAGE_LIMIT
        elif status_code in (401, 403) or AUTH_RE.search(result_text):
            status = C.AUTH_ERROR
        elif data.get("subtype") == "error_max_budget_usd":
            status = C.BUDGET
        else:
            status = C.ERROR
        return LLMResult(status=status, error_message=(result_text or err_tail)[:300], **common)
    return LLMResult(status=C.ERROR, error_message="no is_error in response", **common)


class ClaudeCliBackend:
    def __init__(self, cfg: LLMConfig, environ: Mapping[str, str] | None = None):
        path = shutil.which(cfg.claude_bin)
        if path is None:
            raise ValueError(f"claude binary {cfg.claude_bin!r} not found on PATH")
        self.cfg = cfg
        self.bin = path
        self._environ = environ

    def _env(self) -> dict[str, str]:
        return minimal_env(self._environ if self._environ is not None else os.environ, self.bin)

    def argv(self, req: LLMRequest) -> list[str]:
        argv = [
            self.bin,
            "-p",
            "--model",
            req.model_alias,
            "--tools",
            "",
            "--no-session-persistence",
            "--safe-mode",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--output-format",
            "json",
        ]
        if req.max_budget_usd:
            argv += ["--max-budget-usd", str(req.max_budget_usd)]
        argv += [
            "--system-prompt",
            req.system_prompt,
            "--json-schema",
            json.dumps(req.json_schema, separators=(",", ":")),
            req.task,
        ]
        return argv

    def _spawn(
        self, argv: list[str], stdin_text: str, timeout_s: float
    ) -> tuple[int | None, str, str, bool]:
        """Returns (returncode, stdout, stderr, timed_out). The child's whole group is killed
        on timeout. cwd is an empty temp dir, removed afterwards."""
        with tempfile.TemporaryDirectory(prefix="cvboost-llm-") as cwd:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=self._env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
            try:
                out, err = proc.communicate(stdin_text, timeout=timeout_s)
                return proc.returncode, out, err, False
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                return None, "", "", True
            except BaseException:
                # KeyboardInterrupt, decoding bug, anything: never leave the child running
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise

    def run(self, req: LLMRequest) -> LLMResult:
        t0 = time.monotonic()
        rc, out, err, timed_out = self._spawn(self.argv(req), req.stdin_text, req.timeout_s)
        ms = int((time.monotonic() - t0) * 1000)
        if timed_out:
            return LLMResult(status=C.TIMEOUT, duration_ms=ms, error_message="timeout")
        res = classify(rc, out, err)
        if res.duration_ms is None:
            res = replace(res, duration_ms=ms)
        return res

    def preflight(self) -> PreflightResult:
        rc, out, err, timed_out = self._spawn([self.bin, "auth", "status"], "", 15)
        if timed_out:
            return PreflightResult(False, C.ERROR, message="claude auth status timed out")
        try:
            data = json.loads(out)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return PreflightResult(False, C.ERROR, message=(err or "bad auth status")[:300])
        method = data.get("authMethod") if isinstance(data.get("authMethod"), str) else None
        expires = (
            data.get("loginExpiresAt") if isinstance(data.get("loginExpiresAt"), str) else None
        )
        if data.get("loggedIn") is not True:
            return PreflightResult(False, C.AUTH_ERROR, method, "not logged in", expires)
        if method == "api_key" or method not in self.cfg.allowed_auth_methods:
            return PreflightResult(
                False, C.REFUSED_AUTH_METHOD, method, f"auth method {method!r} refused", expires
            )
        return PreflightResult(True, C.OK, method, None, expires)
