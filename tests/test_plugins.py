"""Plugins (#57): the core alone, a synthetic plugin `example` (tests/example_plugin), the loader.
No real plugin is needed: the public version ships none. The real optional plugin has its own
tests next to it (tests/outbound, not part of the public export)."""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cvboost import cli, plugins
from cvboost.config import Settings
from cvboost.notify.alerts import send_pending
from cvboost.storage import db
from cvboost.storage.db import Store, _migration_files, connect, migrate
from cvboost.web.app import create_app
from cvboost.worker_config import WorkerConfig
from tests.example_plugin import plugin as example_module
from tests.notify.helpers import OWNER, FakeTelegram
from tests.rules_helpers import RULES_PATH
from tests.web.conftest import LLM_YAML

RESERVED = set(range(20, 28)) | set(range(9000, 10000))  # kept free in the core for plugins
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def _no_env(monkeypatch):
    monkeypatch.delenv(plugins.DISABLE_ENV, raising=False)
    monkeypatch.delenv(plugins.REQUIRE_ENV, raising=False)


@pytest.fixture
def no_plugins(monkeypatch):
    """The core alone: the loader knows no plugin name."""
    _no_env(monkeypatch)
    monkeypatch.setattr(plugins, "PLUGIN_NAMES", ())


@pytest.fixture
def with_example(monkeypatch):
    """The loader knows one plugin, `example`, importable as cvboost.example.plugin."""
    _no_env(monkeypatch)
    monkeypatch.setattr(plugins, "PLUGIN_NAMES", ("example",))
    monkeypatch.setitem(sys.modules, "cvboost.example", sys.modules["tests.example_plugin"])
    monkeypatch.setitem(sys.modules, "cvboost.example.plugin", example_module)


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "web.sqlite",
        log_level="INFO",
        rules_path=RULES_PATH,
        llm_config_path=LLM_YAML,
        kits_override=tmp_path / "kits",
        web_hosts=("testserver",),
        web_secret="s" * 40,
    )


def core_numbers() -> list[int]:
    folder = Path(db.__file__).parent / "migrations"
    return sorted(int(p.name[:4]) for p in folder.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def ins(conn, kind: str) -> None:
    conn.execute(
        "INSERT INTO llm_calls (kind, model_alias, n_items, started_at, local_day, status) "
        "VALUES (?, 'haiku', 1, 't', 'd', 'ok')",
        (kind,),
    )


CALL = dict(
    model_alias="haiku", prompt_version="p", n_items=1, attempt=1, now=NOW,
    local_day="2026-11-13", status="ok",
)  # fmt: skip


def alert_texts(tmp_path, kind: str, text: str) -> list[str]:
    """Queue one system alert of `kind` and return the Telegram texts sent for it."""
    conn = connect(tmp_path / f"{kind}.sqlite")
    migrate(conn)
    store = Store(conn)
    store.insert_system_alert(kind, "k-1", text, NOW)
    tg = FakeTelegram()
    assert send_pending(store, tg.client(), WorkerConfig(), OWNER, NOW).system == 1
    return [p["text"] for m, p in tg.calls if m == "sendMessage"]


# --- the core alone ---


def test_core_migration_files_stay_out_of_the_reserved_ranges():
    assert core_numbers() == [*range(1, 20), 28]
    assert not RESERVED & set(core_numbers())


def test_core_without_plugin_fresh_db_migrates_to_28(tmp_path, no_plugins):
    assert plugins.load_plugins() == ()
    assert [v for v, _ in _migration_files()] == core_numbers()
    conn = connect(tmp_path / "c.sqlite")
    assert migrate(conn) == 28
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "jobs" in tables and not {t for t in tables if t.startswith("example_")}
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrate(conn) == 28


def test_core_without_plugin_web_has_no_plugin_page(tmp_path, no_plugins):
    client = TestClient(create_app(settings_for(tmp_path)), follow_redirects=False)
    assert client.get("/example").status_code == 404
    page = client.get("/applications")
    assert page.status_code == 200
    assert "/example" not in page.text
    assert client.get("/static/example/example.css").status_code == 404


def test_core_without_plugin_cli_has_no_plugin_command(no_plugins, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["example"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_core_without_plugin_unknown_kind_is_rejected(tmp_path, no_plugins):
    conn = connect(tmp_path / "c.sqlite")
    migrate(conn)
    store = Store(conn)
    store.insert_llm_call(kind="score", **CALL)
    for kind in ("ex_reply", "bogus"):
        with pytest.raises(ValueError, match="unknown llm call kind"):
            store.insert_llm_call(kind=kind, **CALL)
    assert store.consecutive_limit_hits() == 0


def test_alert_of_a_plugin_kind_is_untrusted_text_without_the_plugin(tmp_path, no_plugins):
    """Without the plugin the kind is not trusted: links in the text are neutralised."""
    (text,) = alert_texts(
        tmp_path, "ex_reply", "Reply from Sam. Leads: https://panel.example.com/x"
    )
    assert "https://" not in text and "Reply from Sam" in text


# --- the loader ---


def test_required_plugin_missing_is_an_error(monkeypatch, with_example):
    monkeypatch.setenv(plugins.REQUIRE_ENV, "example")
    monkeypatch.setitem(sys.modules, "cvboost.example.plugin", None)  # import fails
    with pytest.raises(plugins.PluginError, match="required"):
        plugins.load_plugins()
    monkeypatch.delenv(plugins.REQUIRE_ENV)
    assert plugins.load_plugins() == ()  # not required: silently absent


def test_disable_wins_over_require(monkeypatch, with_example):
    monkeypatch.setenv(plugins.REQUIRE_ENV, "example")
    monkeypatch.setenv(plugins.DISABLE_ENV, "example")
    assert plugins.load_plugins() == ()


def test_broken_dependency_inside_the_plugin_is_not_swallowed(monkeypatch, with_example):
    real = plugins.importlib.import_module

    def boom(name):
        if name == "cvboost.example.plugin":
            raise ModuleNotFoundError("No module named 'yaml'", name="yaml")
        return real(name)

    monkeypatch.setattr(plugins.importlib, "import_module", boom)
    with pytest.raises(ModuleNotFoundError, match="yaml"):
        plugins.load_plugins()


def test_unknown_plugin_name_is_an_error(monkeypatch, with_example):
    monkeypatch.setenv(plugins.REQUIRE_ENV, "exmaple")
    with pytest.raises(plugins.PluginError, match="unknown plugin"):
        plugins.load_plugins()


def test_a_plugin_object_with_the_wrong_name_or_kind_is_an_error(monkeypatch, with_example):
    bad_name = plugins.Plugin(name="other")
    monkeypatch.setattr(example_module, "PLUGIN", bad_name)
    with pytest.raises(plugins.PluginError, match="must expose"):
        plugins.load_plugins()
    bad_kind = plugins.Plugin(name="example", llm_kinds=("Bad Kind",))
    monkeypatch.setattr(example_module, "PLUGIN", bad_kind)
    with pytest.raises(plugins.PluginError, match="bad llm kind"):
        plugins.load_plugins()


def test_duplicate_migration_number_is_an_error(monkeypatch, with_example):
    monkeypatch.setattr(plugins, "migration_packages", lambda: ("cvboost.storage.migrations",))
    with pytest.raises(ValueError, match="used twice"):
        _migration_files()


# --- a plugin (synthetic) ---


def test_plugin_combined_migrations_are_core_plus_own_without_duplicates(with_example):
    (p,) = plugins.load_plugins()
    assert p.name == "example"
    nums = [v for v, _ in _migration_files()]
    assert nums == [*range(1, 20 + 2), 28] and nums == sorted(set(nums))
    own = sorted(
        int(f.name[:4])
        for f in (Path(example_module.__file__).parent / "migrations").glob("[0-9]*.sql")
    )
    assert own == [20, 21] and all(n in RESERVED for n in own)


def test_plugin_registers_kinds_and_pause_kinds(tmp_path, with_example):
    assert plugins.llm_kinds() == {"ex_classify", "ex_reply"}
    assert plugins.trusted_alert_kinds() == ("ex_reply",)
    assert db._pause_kinds_sql() == "'score','cv','ex_classify'"
    conn = connect(tmp_path / "p.sqlite")
    migrate(conn)
    store = Store(conn)
    for kind in ("ex_classify", "ex_reply"):
        store.insert_llm_call(kind=kind, **CALL)
    with pytest.raises(ValueError):
        store.insert_llm_call(kind="bogus", **CALL)


def test_plugin_tables_are_created_and_foreign_keys_hold(tmp_path, with_example):
    conn = connect(tmp_path / "t.sqlite")
    assert migrate(conn) == 28
    conn.execute("INSERT INTO example_items (name) VALUES ('a')")
    conn.execute("INSERT INTO example_log (item_id, note) VALUES (1, 'n')")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrate(conn) == 28  # idempotent


def test_db_migrated_without_kind_open_gets_only_0028_and_keeps_rows(tmp_path, with_example):
    conn = connect(tmp_path / "old.sqlite")
    files = _migration_files()
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version, sql in files:
        if version == 28:
            continue
        if sql.lstrip().startswith(db.FK_OFF_MARKER):
            conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT INTO schema_migrations VALUES (?, 'x')", (version,))
    for kind in ("score", "cv", "email"):
        ins(conn, kind)
    conn.execute("INSERT INTO example_items (name) VALUES ('keep me')")
    before = [tuple(r) for r in conn.execute("SELECT * FROM llm_calls ORDER BY id")]
    applied_before = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    assert applied_before == {*range(1, 20), 20, 21}

    assert migrate(conn) == 28

    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    assert applied - applied_before == {28}
    assert [tuple(r) for r in conn.execute("SELECT * FROM llm_calls ORDER BY id")] == before
    assert conn.execute("SELECT name FROM example_items").fetchall()[0][0] == "keep me"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    ins(conn, "some_new_kind")  # the CHECK is open now
    with pytest.raises(sqlite3.IntegrityError):
        ins(conn, "Bad Kind")
    with pytest.raises(sqlite3.IntegrityError):
        ins(conn, "x" * 33)


def test_plugin_nav_static_and_routes_are_served(tmp_path, with_example):
    client = TestClient(create_app(settings_for(tmp_path)), follow_redirects=False)
    page = client.get("/applications")
    assert 'href="/example"' in page.text and "Example" in page.text
    # the item sits right after CRM, before Sources
    assert page.text.index('href="/crm"') < page.text.index('href="/example"')
    assert page.text.index('href="/example"') < page.text.index('href="/sources"')
    css = client.get("/static/example/example.css")
    assert css.status_code == 200 and ".ex-head" in css.text
    assert ".ex-head" not in client.get("/static/app.css").text
    assert client.get("/example").text == "example page"


def test_plugin_cli_command_is_registered_and_dispatched(
    with_example, capsys, tmp_path, monkeypatch
):
    monkeypatch.setenv("CVBOOST_DB", str(tmp_path / "x.sqlite"))
    assert cli.main(["example"]) == 0
    assert "example plugin ran" in capsys.readouterr().out


def test_alert_of_a_trusted_plugin_kind_keeps_its_text(tmp_path, with_example):
    text = "Reply from Sam. Leads: https://panel.example.com/x"
    (sent,) = alert_texts(tmp_path, "ex_reply", text)
    assert sent == text  # built in code by the plugin: not neutralised
    (other,) = alert_texts(tmp_path, "ex_classify", text)
    assert "https://" not in other  # an untrusted kind still is
