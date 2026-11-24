"""Optional plugins (#57): the core works without them.

A plugin is a package `cvboost.<name>` with a module `cvboost.<name>.plugin` that exposes
`PLUGIN: Plugin`. The core never imports a plugin package itself (tests/test_core_imports.py):
everything a plugin adds reaches the core through this one object -- migrations, LLM call kinds,
CLI commands, web routes, navigation, templates and static files.

Environment:
- `CVBOOST_DISABLE_PLUGINS=outbound`  skip these plugins (tests, a deliberately slim install);
- `CVBOOST_REQUIRE_PLUGINS=outbound`  a missing plugin is an error, not a silent skip (so a broken
  checkout cannot quietly switch a feature off). DISABLE wins over REQUIRE: it is an explicit ask.
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLUGIN_NAMES = ("outbound",)  # plugins the core knows how to look for

DISABLE_ENV = "CVBOOST_DISABLE_PLUGINS"
REQUIRE_ENV = "CVBOOST_REQUIRE_PLUGINS"


class PluginError(RuntimeError):
    pass


@dataclass(frozen=True)
class NavItem:
    key: str
    href: str
    label: str
    after: str | None = None  # key of the core item it follows; None = before the last one


@dataclass(frozen=True)
class Plugin:
    name: str
    migrations_package: str | None = None  # dotted package holding NNNN_*.sql
    llm_kinds: tuple[str, ...] = ()  # kinds the plugin writes to llm_calls
    pause_kinds: tuple[str, ...] = ()  # of them, those that can hit the usage limit
    trusted_alert_kinds: tuple[str, ...] = ()  # system_alerts kinds whose text is built in code
    nav: tuple[NavItem, ...] = ()
    templates_dir: Path | None = None
    static_dir: Path | None = None  # served at /static/<name>/
    register_cli: Callable[[argparse._SubParsersAction], None] | None = None
    dispatch_cli: Callable[[Any, argparse.Namespace], int | None] | None = None
    register_web: Callable[[Any], None] | None = None  # (FastAPI app)


_KIND_RE = re.compile(r"[a-z_]{1,32}")


def _names(raw: str | None) -> set[str]:
    return {x.strip() for x in (raw or "").split(",") if x.strip()}


def load_plugins() -> tuple[Plugin, ...]:
    disabled = _names(os.environ.get(DISABLE_ENV))
    required = _names(os.environ.get(REQUIRE_ENV))
    unknown = (required | disabled) - set(PLUGIN_NAMES)
    if unknown:
        raise PluginError(f"unknown plugin name(s): {', '.join(sorted(unknown))}")
    found: list[Plugin] = []
    for name in PLUGIN_NAMES:
        if name in disabled:
            continue
        modname = f"cvboost.{name}.plugin"
        try:
            mod = importlib.import_module(modname)
        except ModuleNotFoundError as exc:
            # only "the plugin is not installed"; a missing dependency inside it must surface
            if exc.name not in (f"cvboost.{name}", modname):
                raise
            if name in required:
                raise PluginError(
                    f"plugin {name!r} is required ({REQUIRE_ENV}) but cvboost.{name} is missing"
                ) from exc
            continue
        plugin = getattr(mod, "PLUGIN", None)
        if not isinstance(plugin, Plugin) or plugin.name != name:
            raise PluginError(f"{modname} must expose PLUGIN = Plugin(name={name!r}, ...)")
        for kind in (*plugin.llm_kinds, *plugin.pause_kinds):
            if not _KIND_RE.fullmatch(kind):
                raise PluginError(f"plugin {name!r}: bad llm kind {kind!r}")
        found.append(plugin)
    return tuple(found)


def llm_kinds() -> frozenset[str]:
    """Every kind a plugin may write to llm_calls."""
    return frozenset(k for p in load_plugins() for k in p.llm_kinds)


def pause_kinds() -> tuple[str, ...]:
    return tuple(k for p in load_plugins() for k in p.pause_kinds)


def trusted_alert_kinds() -> tuple[str, ...]:
    return tuple(k for p in load_plugins() for k in p.trusted_alert_kinds)


def migration_packages() -> tuple[str, ...]:
    return tuple(p.migrations_package for p in load_plugins() if p.migrations_package)


def nav_items(core: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """The core navigation with the plugins' items placed after the key they name."""
    out = list(core)
    for p in load_plugins():
        for item in p.nav:
            row = (item.key, item.href, item.label)
            idx = next((i for i, r in enumerate(out) if r[0] == item.after), None)
            if idx is None:
                out.insert(max(len(out) - 1, 0), row)
            else:
                out.insert(idx + 1, row)
    return out
