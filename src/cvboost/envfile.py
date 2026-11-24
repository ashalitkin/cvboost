"""The one `.env` parser: used by the CLI (to load ./.env), by Settings tests and by `doctor`.

Rules (a small, strict subset of dotenv):
- `KEY=value`, optional `export ` prefix, `#` comment lines, blank lines;
- a value in single or double quotes ends at the closing quote (a `#` inside is kept);
- an unquoted value ends at ` #` (whitespace + hash starts an inline comment);
- bad UTF-8 is replaced, never raised (a stray byte must not stop the program);
- an empty value counts as "not set" for the caller.
Nothing here ever prints a value. `risky` lists key NAMES whose unquoted value holds a space, `$`
or a backtick: the shell loader (`scripts/local.sh`) would mis-read or expand such a line.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ENV_FILE_VAR = "CVBOOST_ENV_FILE"  # tests point it at a missing file; default ./.env


@dataclass
class EnvFile:
    values: dict[str, str] = field(default_factory=dict)
    risky: list[str] = field(default_factory=list)  # key names only
    dq_risky: list[str] = field(default_factory=list)  # double-quoted value with $ or backtick


def parse_env(text: str) -> EnvFile:
    out = EnvFile()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, rest = line.removeprefix("export ").partition("=")
        key, rest = key.strip(), rest.strip()
        if not KEY_RE.fullmatch(key):
            continue
        if rest[:1] in ("'", '"'):
            end = rest.find(rest[0], 1)
            value = rest[1:end] if end > 0 else rest[1:]
            if rest[0] == '"' and re.search(r"[$`]", value):
                out.dq_risky.append(key)
        else:
            value = re.split(r"\s#", rest, maxsplit=1)[0].strip()
            if re.search(r"[\s$`]", value):
                out.risky.append(key)
        out.values[key] = value
    return out


def read_env_file(path: Path | str) -> EnvFile:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return EnvFile()
    return parse_env(data.decode("utf-8", errors="replace"))


ROOT_MARKERS = ("config", "config.example", "pyproject.toml")


def project_root(start: Path | None = None) -> Path | None:
    """The nearest directory (walking up from `start`, default cwd) holding config/,
    config.example/ or pyproject.toml; None if there is none."""
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if any((d / m).exists() for m in ROOT_MARKERS):
            return d
    return None


def env_file_path() -> Path:
    """CVBOOST_ENV_FILE if set, else .env in the project root (not blindly the cwd, so a stray
    .env in some subfolder is never picked up), else ./.env."""
    override = os.environ.get(ENV_FILE_VAR)
    if override:
        return Path(override)
    root = project_root()
    return (root / ".env") if root else Path(".env")


def load_dotenv(path: Path | str | None = None) -> list[str]:
    """Put the file's values into os.environ for keys that are unset or empty there (the real
    environment wins). Returns the NAMES loaded."""
    loaded = []
    for key, value in read_env_file(path or env_file_path()).values.items():
        if value and not os.environ.get(key):
            os.environ[key] = value
            loaded.append(key)
    return loaded
