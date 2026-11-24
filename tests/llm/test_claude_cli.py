from __future__ import annotations

import json
import os
import stat
import sys
import time

import pytest

from cvboost.llm import client as C
from cvboost.llm.claude_cli import ClaudeCliBackend, classify, minimal_env
from cvboost.llm.client import LLMRequest, make_client
from cvboost.llm.config import LLMConfig

OK_JSON = {
    "type": "result",
    "is_error": False,
    "structured_output": {"verdicts": []},
    "total_cost_usd": 0.0009,
    "usage": {
        "input_tokens": 5,
        "output_tokens": 7,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 2,
    },
    "modelUsage": {"claude-haiku-5-5": {}},
    "duration_ms": 6600,
    "credential_expires_at": "2026-11-26T00:00:00Z",
}


def fake_claude(tmp_path, record, stdout="", sleep=0.0, auth=None, stderr_bytes=b""):
    """An executable `claude` that records argv/env/cwd/stdin and prints a canned reply."""
    script = tmp_path / "bin" / "claude"
    script.parent.mkdir(exist_ok=True)
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        f"rec = {str(record)!r}\n"
        f"time.sleep({sleep})\n"
        "stdin = sys.stdin.read()\n"
        "open(rec + '.' + (sys.argv[1] if len(sys.argv) > 1 else 'x'), 'w').write(json.dumps("
        "{'argv': sys.argv, 'env': dict(os.environ), 'cwd': os.getcwd(), "
        "'cwd_entries': os.listdir('.'), 'stdin': stdin}))\n"
        f"sys.stderr.buffer.write({stderr_bytes!r})\n"
        f"auth = {auth!r}\n"
        "print(auth if (auth and sys.argv[1:2] == ['auth']) else " + repr(stdout) + ")\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def req(**kw):
    data = dict(
        model_alias="haiku",
        system_prompt="SYS",
        json_schema={"type": "object"},
        task="TASK",
        stdin_text="JOB TEXT SECRET-DESCRIPTION",
        timeout_s=20,
        max_budget_usd=0.25,
    )
    data.update(kw)
    return LLMRequest(**data)


def backend(tmp_path, script, environ, **cfg):
    config = LLMConfig.model_validate({"claude_bin": str(script), **cfg})
    return ClaudeCliBackend(config, environ=environ)


def test_subprocess_gets_only_whitelisted_env_and_safe_argv(tmp_path):
    rec = tmp_path / "rec"
    script = fake_claude(tmp_path, rec, stdout=json.dumps(OK_JSON))
    environ = {
        "HOME": "/home/x", "PATH": "/usr/local/bin:/usr/bin", "USER": "me", "LANG": "C",
        "ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_BASE_URL": "http://evil",
        "CVBOOST_DB": "/data/db", "TELEGRAM_BOT_TOKEN": "tg", "CLAUDECODE": "1",
        "CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CLAUDE_CODE_OAUTH_TOKEN": "tok-test", "SECRET_OTHER": "z",
    }  # fmt: skip
    res = backend(tmp_path, script, environ).run(req())
    assert res.status == C.OK and res.model == "claude-haiku-5-5"
    seen = json.loads(open(f"{rec}.-p").read())
    # the macOS python shebang adds LC_CTYPE / __CF_USER_TEXT_ENCODING itself
    assert set(seen["env"]) - {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"} == {
        "HOME", "PATH", "USER", "LANG", "CLAUDE_CODE_OAUTH_TOKEN",
        "DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    }  # fmt: skip
    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-test"
    assert "ANTHROPIC_API_KEY" not in json.dumps(seen["env"])
    argv = seen["argv"]
    i = argv.index("--tools")
    assert argv[i + 1] == ""
    for flag in ("--no-session-persistence", "--safe-mode", "--strict-mcp-config",
                 "--disable-slash-commands", "--json-schema", "-p"):  # fmt: skip
        assert flag in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "haiku"
    assert "--bare" not in argv and "--fallback-model" not in argv
    assert not any("SECRET-DESCRIPTION" in a for a in argv)
    assert seen["stdin"] == "JOB TEXT SECRET-DESCRIPTION"
    assert seen["cwd_entries"] == [] and not os.path.exists(seen["cwd"])


def test_minimal_env_asserts_on_forbidden():
    env = minimal_env({"ANTHROPIC_API_KEY": "x", "HOME": "/h"}, "/opt/bin/claude")
    assert "ANTHROPIC_API_KEY" not in env and env["PATH"].startswith("/opt/bin:")


def test_timeout_kills_process(tmp_path):
    script = fake_claude(tmp_path, tmp_path / "rec", stdout="{}", sleep=30)
    t0 = time.monotonic()
    res = backend(tmp_path, script, {"HOME": "/h"}).run(req(timeout_s=1))
    assert res.status == C.TIMEOUT and time.monotonic() - t0 < 15


def test_make_client_rejects_unknown_backend_and_missing_bin(tmp_path):
    cfg = LLMConfig.model_validate({"claude_bin": "definitely-not-installed-xyz"})
    with pytest.raises(ValueError):
        make_client(cfg)


BARE = {
    "is_error": True, "subtype": "success", "terminal_reason": "api_error",
    "api_error_status": None, "result": "Not logged in · Please run /login",
    "total_cost_usd": 0, "modelUsage": {},
}  # fmt: skip


@pytest.mark.parametrize(
    ("data", "stdout", "rc", "expected"),
    [
        (BARE, None, 1, C.AUTH_ERROR),
        ({"is_error": True, "api_error_status": 429, "api_error": "usage_limit_reached"}, None, 1,
         C.USAGE_LIMIT),
        ({"is_error": True, "subtype": "success", "result": "You've hit your limit · resets 3pm"},
         None, 0, C.USAGE_LIMIT),
        ({"is_error": True, "api_error_status": 429, "api_error": "rate_limit_error",
          "result": "Too many requests"}, None, 1, C.ERROR),
        ({"is_error": True, "subtype": "error_max_budget_usd"}, None, 1, C.BUDGET),
        ({"is_error": True, "api_error_status": 529, "result": "Overloaded"}, None, 1, C.ERROR),
        ({"is_error": False, "result": "hello"}, None, 0, C.INVALID_OUTPUT),
        (None, "garbage", 0, C.ERROR),
        (None, "[1,2]", 0, C.ERROR),
        (OK_JSON, None, 0, C.OK),
    ],
)  # fmt: skip
def test_classify(data, stdout, rc, expected):
    res = classify(rc, stdout if stdout is not None else json.dumps(data), "")
    assert res.status == expected


def test_classify_extracts_usage():
    res = classify(0, json.dumps(OK_JSON), "")
    assert (res.input_tokens, res.output_tokens) == (5, 7)
    assert (res.cache_read_tokens, res.cache_creation_tokens) == (3, 2)
    assert res.total_cost_usd == 0.0009 and res.duration_ms == 6600
    assert res.credential_expires_at == "2026-11-26T00:00:00Z"


@pytest.mark.parametrize(
    ("method", "ok", "status"),
    [
        ("claude.ai", True, C.OK),
        ("oauth_token", True, C.OK),
        ("api_key", False, C.REFUSED_AUTH_METHOD),
        ("something_new", False, C.REFUSED_AUTH_METHOD),
    ],
)
def test_preflight(tmp_path, method, ok, status):
    auth = json.dumps({"loggedIn": True, "authMethod": method, "loginExpiresAt": "2026-11-26"})
    script = fake_claude(tmp_path, tmp_path / "rec", stdout="{}", auth=auth)
    pre = backend(tmp_path, script, {"HOME": "/h"}).preflight()
    assert pre.ok is ok and pre.status == status and pre.auth_method == method
    seen = json.loads(open(f"{tmp_path / 'rec'}.auth").read())
    assert seen["argv"][1:] == ["auth", "status"]  # never a model call


def test_preflight_not_logged_in(tmp_path):
    auth = json.dumps({"loggedIn": False, "authMethod": "none"})
    script = fake_claude(tmp_path, tmp_path / "rec", stdout="{}", auth=auth)
    pre = backend(tmp_path, script, {"HOME": "/h"}).preflight()
    assert not pre.ok and pre.status == C.AUTH_ERROR


def test_non_utf8_stderr_does_not_crash(tmp_path):
    script = fake_claude(
        tmp_path, tmp_path / "rec", stdout=json.dumps(OK_JSON), stderr_bytes=b"bad \xff byte"
    )
    res = backend(tmp_path, script, {"HOME": "/h"}).run(req())
    assert res.status == C.OK


def test_classify_plain_text_limit_and_login_notices():
    res = classify(1, "You've hit your limit \u00b7 resets 3pm", "")
    assert res.status == C.USAGE_LIMIT
    res = classify(1, "", "Error: Please run /login")
    assert res.status == C.AUTH_ERROR
    assert classify(1, "weird", "").status == C.ERROR
