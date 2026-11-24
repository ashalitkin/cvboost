from __future__ import annotations

import pytest

from cvboost import cli
from cvboost.core.dedupe import assign_canonical
from cvboost.core.normalize import company_norm, title_norm
from cvboost.triage import triage
from tests.conftest import NOW
from tests.rules_helpers import CFG, RULES_PATH, add, sync


def _canon(store, *ids):
    return [
        store.conn.execute("SELECT canonical_id FROM jobs WHERE id=?", (i,)).fetchone()[0]
        for i in ids
    ]


def _two(store, **kw):
    sync(store, "remotive", "wwr")
    a = add(store, "remotive", "a", days_ago=20, company="Lemon.io", title="Senior Vue Developer")
    b = add(
        store,
        "wwr",
        "b",
        days_ago=kw.pop("days_ago", 5),
        **{"company": "Lemon io", "title": "Sr. Vue Developer", **kw},
    )
    return a, b


def test_same_job_on_two_boards_one_canonical(store):
    a, b = _two(store)
    assign_canonical(store.conn, 30)
    assert _canon(store, a, b) == [a, a]
    assert a < b


def test_gap_over_window_separates(store):
    a, b = _two(store, days_ago=-12)  # 20 + 12 = 32 days after
    sync(store, "remotive")
    assign_canonical(store.conn, 30)
    assert _canon(store, a, b) == [a, b]


def test_different_company_separates(store):
    a, b = _two(store, company="Other Corp")
    assign_canonical(store.conn, 30)
    assert _canon(store, a, b) == [a, b]


def test_same_board_different_location_separates(store):
    sync(store, "wwr")
    a = add(store, "wwr", "1", location_text="USA Only", description_text="x1")
    b = add(store, "wwr", "2", location_text="Europe Only", description_text="x2")
    c = add(store, "wwr", "3", location_text="USA Only", description_text="x3")
    assign_canonical(store.conn, 30)
    assert _canon(store, a, b, c) == [a, b, a]


def test_same_content_hash_different_company_merges_not_by_name(store):
    # identical text hash requires identical title/company/description, so force it via SQL
    sync(store, "remotive", "wwr")
    a = add(store, "remotive", "a", company="One", title="Zeta Role")
    b = add(store, "wwr", "b", company="Two", title="Other Role")
    store.conn.execute(
        "UPDATE jobs SET content_hash = (SELECT content_hash FROM jobs WHERE id=?) WHERE id=?",
        (a, b),
    )
    assign_canonical(store.conn, 30)
    assert _canon(store, a, b) == [a, a]


def test_rerun_no_changes(store):
    _two(store)
    assert assign_canonical(store.conn, 30).changed > 0
    assert assign_canonical(store.conn, 30).changed == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Senior Data Engineer (m/w/d)", "senior data engineer"),
        ("Data Engineer (R13923)", "data engineer"),
        ("Sr. Data Engineer", "senior data engineer"),
        ("Data Engineer - US", "data engineer us"),
        ("Remote Data Engineer (Contract)", "data engineer"),
    ],
)
def test_title_norm(raw, expected):
    assert title_norm(raw) == expected


def test_company_norm():
    assert company_norm("Lemon.io") == "lemon"
    assert company_norm("ACME Inc.") == company_norm("acme") == "acme"
    assert company_norm("Acme (YC W24)") == "acme"
    assert company_norm("Café GmbH") == "cafe"
    assert company_norm("Яндекс") == "яндекс"
    assert title_norm("Аналитик данных") == "аналитик данных"


def test_cli_kept_one_row_for_two_boards(tmp_path, monkeypatch, capsys):
    db = tmp_path / "cli.sqlite"
    monkeypatch.setenv("CVBOOST_DB", str(db))
    monkeypatch.setenv("CVBOOST_RULES", str(RULES_PATH))
    monkeypatch.setattr(cli.connectors, "REGISTRY", cli.connectors.REGISTRY)
    from cvboost.storage.db import Store, connect, migrate

    conn = connect(db)
    migrate(conn)
    store = Store(conn)
    sync(store, "remotive", "wwr")
    cli_title = "Analytics Engineer"
    add(store, "remotive", "a", days_ago=20, company="Lemon.io", title=cli_title)
    add(store, "wwr", "b", days_ago=5, company="Lemon io", title=cli_title)
    conn.close()
    assert cli.main(["triage"]) == 0
    capsys.readouterr()
    assert cli.main(["jobs", "--kept"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2  # header + one group
    assert "remotive,wwr" in lines[1]


def test_triage_stats_groups(store):
    _two(store)
    st = triage(store, CFG, NOW)
    assert st.merged == 1 and st.groups == 1
