"""#70b guard: the core UI and every string the code writes is English.

Scans the web templates, static JS/CSS and the string constants (docstrings included) of
`src/cvboost/**/*.py` except the outbound plugin (#70c). Cyrillic may live only in data and in two
files that must read Russian text on purpose. Output names `file:line` of every offender.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

CYRILLIC = re.compile("[Ѐ-ӿ]")
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "cvboost"
# exact relative paths (to the scanned root) allowed to hold Cyrillic constants:
# language detection of job text, and the parser of old Russian fit_notes.md files
ALLOWLIST = frozenset({"core/language.py", "cv/backfill.py"})
EXCLUDED_DIRS = ("outbound",)


def _excluded(rel: Path) -> bool:
    return rel.parts[0] in EXCLUDED_DIRS


def find_cyrillic(root: Path) -> list[str]:
    """`rel/path:line: snippet` for every Cyrillic line of the UI files and string constant."""
    hits: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root)
        if _excluded(rel) or rel.as_posix() in ALLOWLIST:
            continue
        if path.suffix in {".html", ".js", ".css"}:
            if path.name.endswith(".min.js"):
                continue  # vendored htmx
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if CYRILLIC.search(line):
                    hits.append(f"{rel.as_posix()}:{n}: {line.strip()[:100]}")
        elif path.suffix == ".py":
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and CYRILLIC.search(node.value)
                ):
                    hits.append(f"{rel.as_posix()}:{node.lineno}: {node.value.strip()[:100]!r}")
    return hits


def test_core_ui_and_string_constants_have_no_cyrillic():
    hits = find_cyrillic(SRC)
    assert hits == [], "Cyrillic in the English UI:\n" + "\n".join(hits)


def test_base_template_declares_english():
    html = (SRC / "web" / "templates" / "base.html").read_text(encoding="utf-8")
    assert '<html lang="en">' in html


def test_allowlist_is_exactly_two_existing_files():
    assert ALLOWLIST == {"core/language.py", "cv/backfill.py"}
    for rel in ALLOWLIST:
        assert (SRC / rel).is_file()
        assert CYRILLIC.search((SRC / rel).read_text(encoding="utf-8")), rel  # still needed


# --- self-test: the scanner itself must catch what it claims to ---


def _tree(tmp_path: Path) -> Path:
    files = {
        "web/templates/page.html": "<p>ok</p>\n<p>Привет</p>\n",
        "web/templates/clean.html": "<p>ok</p>\n",
        "web/static/app.js": "var a = 1;\nvar b = 'Скопировано';\n",
        "web/static/app.css": "/* Привет */\n",
        "web/static/htmx.min.js": "var x='Привет';\n",
        "mod.py": '"""Doc."""\nA = "fine"\nB = "Привет"  # comment Привет\n',
        "docstring.py": 'def f():\n    """Привет"""\n',
        "comment_only.py": "x = 1  # Привет\n",
        "fstring.py": "name = 'x'\ns = f'Привет {name}'\n",
        "core/language.py": "R = 'а-я'\n",
        "cv/backfill.py": "R = 'откликаться'\n",
        "other/language.py": "R = 'а-я'\n",  # same name, not the allowlisted path
        "outbound/mod.py": "R = 'Привет'\n",
        "outbound/templates/t.html": "<p>Привет</p>\n",
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_scanner_finds_what_it_should_and_nothing_else(tmp_path):
    hits = find_cyrillic(_tree(tmp_path))
    found = sorted({h.split(": ", 1)[0] for h in hits})
    assert found == [
        "docstring.py:2",
        "fstring.py:2",
        "mod.py:3",
        "other/language.py:1",
        "web/static/app.css:1",
        "web/static/app.js:2",
        "web/templates/page.html:2",
    ]


def test_scanner_reports_file_and_line(tmp_path):
    hits = find_cyrillic(_tree(tmp_path))
    assert any(h.startswith("web/templates/page.html:2: ") and "Привет" in h for h in hits)
