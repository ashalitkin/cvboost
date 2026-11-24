"""The core never imports an optional plugin (#57): only the plugin's own package may."""

from __future__ import annotations

import ast
from pathlib import Path

from cvboost import plugins

SRC = Path(__file__).resolve().parents[1] / "src" / "cvboost"
# the names the loader looks for, plus any package that carries a plugin.py
PLUGIN_DIRS = set(plugins.PLUGIN_NAMES) | {p.parent.name for p in SRC.glob("*/plugin.py")}


def _imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield node.lineno, a.name
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * node.level) + (node.module or "")
            yield node.lineno, mod
            for a in node.names:  # `from cvboost import example`
                yield node.lineno, f"{mod}.{a.name}"


def _is_plugin(name: str, dirs: set[str] = PLUGIN_DIRS) -> bool:
    parts = name.lstrip(".").split(".")
    if name.startswith("."):
        return parts[0] in dirs
    return len(parts) >= 2 and parts[0] == "cvboost" and parts[1] in dirs


def test_nothing_outside_a_plugin_imports_a_plugin():
    bad = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC)
        if rel.parts[0] in PLUGIN_DIRS:
            continue
        for line, name in _imports(ast.parse(path.read_text(encoding="utf-8"))):
            if _is_plugin(name):
                bad.append(f"{rel}:{line} imports {name}")
    assert not bad, "\n".join(bad)


def test_the_check_itself_sees_a_violation():
    tree = ast.parse("from cvboost.example import sync\nimport cvboost.example.gmail\n")
    assert len([n for _, n in _imports(tree) if _is_plugin(n, {"example"})]) == 3
    assert not [n for _, n in _imports(tree) if _is_plugin(n, {"other"})]
    rel = ast.parse("from .example import sync\n")
    assert [n for _, n in _imports(rel) if _is_plugin(n, {"example"})]
