"""`cvboost doctor`: is this installation ready to run? One report, no secrets.

Every check returns OK / WARN / FAIL. FAIL means something the system cannot run without;
WARN is an optional part that is not set up (Telegram, keyed sources) or a thing to look at.
Secrets are never printed: only the NAMES of settings, and only whether they are set.

Offline by design: the claude CLI check runs `claude auth status` (local), nothing else touches
the network. The database check migrates a COPY in a temporary folder, never the real file.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cvboost.config import Settings
from cvboost.envfile import env_file_path, read_env_file
from cvboost.llm.client import OK as LLM_OK
from cvboost.llm.client import PreflightResult

OK, WARN, FAIL = "ok", "warn", "FAIL"
TOKEN_RE = re.compile(r"\d{5,15}:[A-Za-z0-9_-]{30,50}")  # <bot id>:<secret>, as BotFather issues
EXAMPLE_NAME = "Jordan Example"
MIN_PYTHON = (3, 12)
# Settings whose absence matters, with what breaks without them (names only).
ENV_HINTS: tuple[tuple[str, str], ...] = (
    ("TELEGRAM_BOT_TOKEN", "no Telegram alerts"),
    ("TELEGRAM_CHAT_ID", "no Telegram alerts (run `cvboost telegram chat-id`)"),
)


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    message: str


def _check_python(version: tuple[int, int, int]) -> Check:
    have = ".".join(map(str, version[:3]))
    if version[:2] >= MIN_PYTHON:
        return Check(OK, "python", f"Python {have}")
    return Check(FAIL, "python", f"Python {have}: version {'.'.join(map(str, MIN_PYTHON))}+ needed")


def _check_uv(which: Callable[[str], str | None]) -> Check:
    if which("uv"):
        return Check(OK, "uv", "found")
    return Check(WARN, "uv", "not found (https://docs.astral.sh/uv/); fine when you run in Docker")


def _check_claude(
    settings: Settings,
    which: Callable[[str], str | None],
    preflight: Callable[[], PreflightResult] | None,
    env: dict[str, str],
) -> list[Check]:
    from cvboost.llm.client import make_client
    from cvboost.llm.config import load_llm_config

    try:
        cfg = load_llm_config(settings.llm_config_path)
    except (ValueError, OSError) as exc:
        return [Check(FAIL, "llm config", f"{settings.llm_config_path}: {exc}")]
    out = [Check(OK, "llm config", f"{settings.llm_config_path}: backend {cfg.backend}")]
    if not which(cfg.claude_bin):
        out.append(
            Check(
                FAIL,
                "claude CLI",
                f"`{cfg.claude_bin}` not found: install Claude Code (README, step 2) and run "
                "`claude` once to log in",
            )
        )
        return out
    out.append(Check(OK, "claude CLI", f"`{cfg.claude_bin}` found"))
    if env.get("ANTHROPIC_API_KEY"):
        out.append(
            Check(
                WARN,
                "ANTHROPIC_API_KEY",
                "is set in the environment: cvboost never passes it to claude and refuses an "
                "api_key login; remove it so it cannot confuse other tools",
            )
        )
    result = (preflight or make_client(cfg).preflight)()
    if result.ok and result.status == LLM_OK:
        out.append(Check(OK, "claude login", f"logged in via {result.auth_method} (no API key)"))
    else:
        out.append(
            Check(
                FAIL,
                "claude login",
                f"{result.status}: {result.message or 'not logged in'}. Run `claude` and log in "
                "with your Claude subscription (an API-key login is refused on purpose)",
            )
        )
    return out


def _check_env(settings: Settings, env_file: dict[str, str], env: dict[str, str]) -> list[Check]:
    out: list[Check] = []
    merged = {**env_file, **{k: v for k, v in env.items() if v}}
    for name, effect in ENV_HINTS:
        if merged.get(name):
            where = "environment" if env.get(name) else ".env"
            out.append(Check(OK, name, f"set (in the {where})"))
        else:
            out.append(Check(WARN, name, f"not set: {effect}"))
    token = merged.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        if TOKEN_RE.fullmatch(token):
            out.append(Check(OK, "Telegram token format", "looks like <bot id>:<secret>"))
        else:
            out.append(
                Check(WARN, "Telegram token format", "TELEGRAM_BOT_TOKEN does not look like a "
                      "BotFather token (<digits>:<35 characters>); copy it again")
            )  # fmt: skip
    return out


def _check_env_file_mode(path: Path) -> Check | None:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return None
    if mode & 0o077:
        return Check(
            WARN, ".env permissions", f"{oct(mode)}: readable by others; run chmod 600 .env"
        )
    return None


def _check_config(settings: Settings) -> list[Check]:
    """config/ exists, worker.yaml is valid, the CV photo is there."""
    from cvboost.worker_config import load_worker_config

    paths = (settings.profile_path, settings.rules_path, settings.worker_config_path)
    if not all(p.exists() for p in paths):
        hint = (
            "run `uv run cvboost init` (copies config.example/ to config/), then /setup"
            if Path("config.example").is_dir()
            else "restore your config files"
        )
        missing = ", ".join(str(p) for p in paths if not p.exists())
        return [Check(FAIL, "config", f"missing: {missing}. {hint}")]
    out = [Check(OK, "config", f"{settings.profile_path.parent}/ found")]
    try:
        load_worker_config(settings.worker_config_path)
    except (ValueError, OSError) as exc:
        out.append(Check(FAIL, "worker config", f"{settings.worker_config_path}: {exc}"))
    else:
        out.append(Check(OK, "worker config", str(settings.worker_config_path)))
    if not (settings.resume_dir / "photo.jpg").is_file():
        out.append(
            Check(
                WARN,
                "photo",
                f"{settings.resume_dir}/photo.jpg is missing: the CV PDF cannot be built "
                "(`cvboost init` makes a placeholder; put a square JPEG there)",
            )
        )
    return out


def _check_db(settings: Settings) -> Check:
    from cvboost.storage.db import connect, migrate

    with tempfile.TemporaryDirectory(prefix="cvboost-doctor-") as tmp:
        target = Path(tmp) / "copy.sqlite"
        try:
            if settings.db_path.exists():
                src = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
                try:
                    dst = sqlite3.connect(target)
                    src.backup(dst)
                    dst.close()
                finally:
                    src.close()
                what = f"{settings.db_path} (a copy was migrated)"
            else:
                what = f"{settings.db_path} does not exist yet (an empty database was migrated)"
            conn = connect(target)
            version = migrate(conn)
            conn.close()
        except (sqlite3.Error, ValueError, OSError) as exc:
            return Check(FAIL, "database", f"{settings.db_path}: {type(exc).__name__}: {exc}")
    return Check(OK, "database", f"{what}; schema version {version}")


def _check_profile(settings: Settings) -> list[Check]:
    from cvboost.cli import cmd_profile_check
    from cvboost.profile import load_user_profile

    out_buf, err_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        rc = cmd_profile_check(settings, None)  # type: ignore[arg-type]
    checks: list[Check] = []
    problems = [ln.removeprefix("PROBLEM: ") for ln in err_buf.getvalue().splitlines()
                if ln.startswith("PROBLEM: ")]  # fmt: skip
    if rc == 0:
        checks.append(
            Check(OK, "profile", "profile.yaml, prompt blocks, profile.md and gaps.yaml agree")
        )
        lang = next(
            (ln for ln in out_buf.getvalue().splitlines() if ln.startswith("languages: ")), ""
        )
        if lang:
            checks.append(Check(OK, "languages", lang.removeprefix("languages: ")))
    else:
        for p in problems or ["see `cvboost profile check`"]:
            checks.append(Check(FAIL, "profile", p))
    for ln in err_buf.getvalue().splitlines():
        if ln.startswith("WARNING: "):
            checks.append(Check(WARN, "profile", ln.removeprefix("WARNING: ")))
    try:
        if load_user_profile(settings.profile_path).name == EXAMPLE_NAME:
            checks.append(
                Check(
                    WARN,
                    "profile",
                    f'still the example profile "{EXAMPLE_NAME}": run /setup in Claude Code',
                )
            )
    except ValueError:
        pass
    return checks


def _check_sources(settings: Settings, env: dict[str, str]) -> list[Check]:
    from cvboost import companies as co
    from cvboost import connectors
    from cvboost.connectors.ats import ATSConnector
    from cvboost.connectors.keyed import KeyedAPIConnector

    out: list[Check] = []
    try:
        listed, rejected = co.load_companies(settings.companies_path)
    except ValueError as exc:
        listed, rejected = [], [str(exc)]
    per_ats = co.per_ats(listed)
    if rejected:
        out.append(
            Check(
                WARN,
                "companies",
                f"{settings.companies_path}: {len(rejected)} bad entr(y/ies) ignored",
            )
        )
    for cls in connectors.REGISTRY.values():
        name = cls.name
        if not cls.default_enabled:
            out.append(Check(OK, f"source {name}", "off by default"))
        elif issubclass(cls, KeyedAPIConnector):
            missing = [v for v in cls.env_vars if not env.get(v, "").strip()]
            if missing:
                out.append(
                    Check(
                        WARN,
                        f"source {name}",
                        f"needs {', '.join(missing)} in .env (optional, see SOURCES.md)",
                    )
                )
            else:
                out.append(Check(OK, f"source {name}", "key set"))
        elif name == "email_alerts":
            if settings.imap_creds() is None:
                out.append(
                    Check(
                        WARN,
                        f"source {name}",
                        "needs CVBOOST_IMAP_USER and CVBOOST_IMAP_PASSWORD (optional)",
                    )
                )
            else:
                out.append(
                    Check(
                        OK,
                        f"source {name}",
                        f"mailbox settings present (host {settings.imap_host})",
                    )
                )
        elif issubclass(cls, ATSConnector):
            n = per_ats.get(name, 0)
            if n:
                out.append(
                    Check(OK, f"source {name}", f"{n} companies in {settings.companies_path.name}")
                )
            else:
                out.append(
                    Check(WARN, f"source {name}", f"no companies in {settings.companies_path}")
                )
        else:
            out.append(Check(OK, f"source {name}", "no setup needed"))
    return out


def run_checks(
    settings: Settings,
    *,
    env: dict[str, str] | None = None,
    env_file: Path | None = None,
    which: Callable[[str], str | None] | None = None,
    preflight: Callable[[], PreflightResult] | None = None,
    python_version: tuple[int, int, int] | None = None,
) -> list[Check]:
    env = dict(os.environ) if env is None else env
    which = which or shutil.which
    env_path = env_file if env_file is not None else env_file_path()
    parsed = read_env_file(env_path)
    file_values = parsed.values
    checks = [_check_python(python_version or sys.version_info[:3]), _check_uv(which)]
    checks += _check_claude(settings, which, preflight, {**file_values, **env})
    if env_path.exists():
        mode = _check_env_file_mode(env_path)
        if mode:
            checks.append(mode)
    else:
        checks.append(Check(WARN, ".env", f"{env_path} not found (copy .env.example to .env)"))
    for key in parsed.risky:
        checks.append(
            Check(
                WARN,
                key,
                "value has a space, $ or backtick without quotes: put it in 'single quotes' "
                "in .env, or scripts/local.sh will misread it",
            )
        )
    for key in parsed.dq_risky:
        checks.append(
            Check(
                WARN,
                key,
                'value in "double quotes" has a $ or backtick: the shell loader expands it; '
                "put it in 'single quotes' in .env",
            )
        )
    checks += _check_env(settings, file_values, env)
    checks += _check_config(settings)
    checks.append(_check_db(settings))
    checks += _check_profile(settings)
    checks += _check_sources(settings, {**file_values, **env})
    return checks


def format_report(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = [f"[{c.status:>4}] {c.name.ljust(width)}  {c.message}" for c in checks]
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    lines.append("")
    lines.append(f"{len(checks) - fails - warns} ok, {warns} warning(s), {fails} failure(s)")
    if fails:
        lines.append("Fix the FAIL lines first, then run `cvboost doctor` again.")
    else:
        lines.append("Ready. Start with: scripts/local.sh up   (warnings are optional parts)")
    return "\n".join(lines)


def cmd_doctor(settings: Settings, _args: object = None) -> int:
    checks = run_checks(settings)
    print(format_report(checks))
    print(f"(checked {datetime.now(UTC):%Y-%m-%d %H:%M} UTC)")
    return 1 if any(c.status == FAIL for c in checks) else 0
