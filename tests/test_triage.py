from __future__ import annotations

import json
from datetime import timedelta

from cvboost import cli
from cvboost.core.rules import rule_version
from cvboost.storage.db import Store, connect, migrate
from cvboost.triage import triage
from tests.conftest import NOW
from tests.rules_helpers import CFG, RULES_PATH, add, sync


def _count(store):
    return store.conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]


def test_two_runs_do_not_duplicate(store):
    sync(store, "fake")
    add(store, "fake", "1")
    add(store, "fake", "2", title="Mortgage Loan Officer", company="Other")
    s1 = triage(store, CFG, NOW)
    n = _count(store)
    s2 = triage(store, CFG, NOW + timedelta(hours=1))
    assert s1.evaluated == 2 and s2.evaluated == 0
    assert _count(store) == n == 2


def test_new_config_new_version_keeps_history(store):
    sync(store, "fake")
    add(store, "fake", "1")
    add(store, "fake", "2", title="Mortgage Loan Officer", company="Other")
    triage(store, CFG, NOW)
    cfg2 = CFG.model_copy(update={"relevance": CFG.relevance.model_copy(update={"threshold": 50})})
    assert rule_version(cfg2) != rule_version(CFG)
    triage(store, cfg2, NOW + timedelta(hours=1))
    assert _count(store) == 4


def test_changed_description_adds_one_verdict(store):
    sync(store, "fake")
    a = add(store, "fake", "1")
    add(store, "fake", "2", title="Mortgage Loan Officer", company="Other")
    triage(store, CFG, NOW)
    later = NOW + timedelta(hours=2)
    run = store.start_run("fake", later)
    from tests.conftest import make_job

    with store.transaction():
        store.upsert_job(make_job("1", source="fake", description_text="W2 only role"), run, later)
    st = triage(store, CFG, later + timedelta(hours=1))
    assert st.evaluated == 1 and st.changed == 1
    assert _count(store) == 3
    v = store.conn.execute(
        "SELECT reasons_json FROM verdicts WHERE job_id=? ORDER BY id DESC", (a,)
    ).fetchone()
    assert "w2_only" in json.loads(v[0])


def test_group_with_us_only_member_is_rejected_and_relevance_is_max(store):
    sync(store, "remotive", "wwr")
    add(store, "remotive", "a", company="Gamma", title="Analytics Engineer", location_text=None)
    add(
        store,
        "wwr",
        "b",
        company="Gamma",
        title="Analytics Engineer",
        location_text="USA Only",
        description_text="different",
    )
    triage(store, CFG, NOW)
    assert store.kept_canonicals(rule_version(CFG), CFG.relevance.threshold) == []

    sync(store, "x1", "x2")
    add(store, "x1", "c", company="Beta", title="Data Analyst", description_text="plain")
    add(
        store,
        "x2",
        "d",
        company="Beta",
        title="Data Analyst",
        description_text="sql dbt python airflow bigquery snowflake looker",
        tags=["sql", "dbt"],
    )
    triage(store, CFG, NOW + timedelta(hours=1))
    groups = store.kept_canonicals(rule_version(CFG), 0)
    beta = [g for g in groups if g.company == "Beta"]
    assert len(beta) == 1
    rels = [
        r[0]
        for r in store.conn.execute(
            "SELECT relevance FROM verdicts v JOIN jobs j ON j.id=v.job_id WHERE j.company='Beta'"
        )
    ]
    assert beta[0].relevance == max(rels) and len(rels) == 2


def _cli_env(tmp_path, monkeypatch, rules=RULES_PATH):
    db = tmp_path / "c.sqlite"
    monkeypatch.setenv("CVBOOST_DB", str(db))
    monkeypatch.setenv("CVBOOST_RULES", str(rules))
    return db


def test_cli_bad_yaml_exit_2(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("rate: [unclosed\n")
    _cli_env(tmp_path, monkeypatch, bad)
    assert cli.main(["triage"]) == 2
    assert "error" in capsys.readouterr().err


def test_cli_why_and_no_verdict(tmp_path, monkeypatch, capsys):
    db = _cli_env(tmp_path, monkeypatch)
    conn = connect(db)
    migrate(conn)
    store = Store(conn)
    sync(store, "fake")
    a = add(store, "fake", "1", title="Analytics Engineer", location_text="USA Only")
    conn.close()
    assert cli.main(["jobs", "--why", str(a)]) == 0
    assert "no verdict yet" in capsys.readouterr().out
    assert cli.main(["triage"]) == 0
    capsys.readouterr()
    assert cli.main(["jobs", "--why", str(a)]) == 0
    out = capsys.readouterr().out
    assert "us_only" in out and "location_text" in out and "data_engineer" in out
    assert "analytics engineer" in out
    assert cli.main(["jobs", "--kept"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 1  # header only


def test_cli_kept_without_verdicts_hint(tmp_path, monkeypatch, capsys):
    _cli_env(tmp_path, monkeypatch)
    assert cli.main(["jobs", "--kept"]) == 0
    assert "run cvboost triage" in capsys.readouterr().out


def test_norms_filled_after_triage(store):
    sync(store, "fake")
    add(store, "fake", "1", company="ACME Inc.")
    assert store.conn.execute("SELECT company_norm FROM jobs").fetchone()[0] is None
    triage(store, CFG, NOW)
    assert store.conn.execute("SELECT company_norm FROM jobs").fetchone()[0] == "acme"
