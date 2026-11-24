"""SOURCES.md is generated from connector metadata + docs/sources_meta.yaml (task #62)."""

from __future__ import annotations

from cvboost import cli, connectors, sources_doc


def test_every_connector_has_complete_meta():
    assert sources_doc.missing_meta(sources_doc.load_meta()) == []


def test_missing_entry_is_reported():
    meta = sources_doc.load_meta()
    del meta["reed"]
    meta["ghost"] = dict(meta["itjobs"])
    meta["itjobs"]["price"] = ""
    problems = sources_doc.missing_meta(meta)
    assert "no entry for connector reed" in problems
    assert "entry ghost has no connector" in problems
    assert "itjobs: empty 'price'" in problems


def test_keyed_source_needs_where():
    meta = sources_doc.load_meta()
    meta["adzuna"]["where"] = ""
    assert any("adzuna" in p for p in sources_doc.missing_meta(meta))


def test_render_mentions_every_source_key_and_tos():
    text = sources_doc.render_markdown()
    for name, cls in connectors.REGISTRY.items():
        assert f"## {name}\n" in text
        assert cls.tos_note in text
    for var in ("ADZUNA_APP_ID", "ADZUNA_APP_KEY", "REED_API_KEY", "ITJOBS_API_KEY"):
        assert var in text
    assert "CVBOOST_IMAP_PASSWORD" in text


def test_incomplete_meta_refuses_to_render():
    meta = sources_doc.load_meta()
    del meta["gem"]
    try:
        sources_doc.render_markdown(meta)
    except ValueError as exc:
        assert "gem" in str(exc)
    else:
        raise AssertionError("rendered an incomplete document")


def test_markdown_command_touches_no_database_and_no_env(tmp_path, monkeypatch, capsys):
    db = tmp_path / "nope" / "x.sqlite"
    monkeypatch.setenv("CVBOOST_DB", str(db))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "not-a-number")  # a bad env must not matter
    assert cli.main(["sources", "--markdown"]) == 0
    assert capsys.readouterr().out.startswith("# Sources")
    assert not db.exists() and not db.parent.exists()
