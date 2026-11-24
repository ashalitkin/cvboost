from __future__ import annotations

import re
from pathlib import Path

import cvboost.web as web

ROOT = Path(web.__file__).parent


def test_no_unsafe_constructs_in_templates():
    for f in (ROOT / "templates").rglob("*.html"):
        t = f.read_text()
        assert not re.search(r"\|\s*safe\b", t), f
        assert "style=" not in t, f
        assert not re.search(r"\son[a-z]+\s*=", t), f
        assert not re.search(r"<script(?![^>]*\ssrc=)", t), f
        assert "autoescape" not in t, f


def test_markup_is_created_only_in_security_module():
    for f in ROOT.glob("*.py"):
        if f.name != "security.py":
            assert "Markup(" not in f.read_text(), f
    assert "mark_safe" not in "".join(f.read_text() for f in ROOT.glob("*.py"))


def test_autoescape_on_and_htmx_pinned(client):
    assert client.app.state.templates.env.autoescape is True
    ver = (ROOT / "static" / "htmx.VERSION").read_text()
    import hashlib

    sha = hashlib.sha256((ROOT / "static" / "htmx.min.js").read_bytes()).hexdigest()
    assert sha in ver and "2.0." in ver
