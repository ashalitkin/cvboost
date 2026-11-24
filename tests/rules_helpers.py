from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from cvboost.connectors.base import Connector
from cvboost.core.rules import RuleInput, compile_rules, evaluate, load_config
from cvboost.storage.db import Store
from tests.conftest import NOW, make_job

RULES_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "config" / "rules.yaml"
CFG = load_config(RULES_PATH)
COMPILED = compile_rules(CFG)


def ev(**kw):
    data = dict(
        company="Acme",
        title="Analytics Engineer (dbt, BigQuery)",
        description_text="",
        location_text=None,
        tags=(),
        salary_min=None,
        salary_max=None,
        salary_currency=None,
        salary_period=None,
        salary_text=None,
        employment_type=None,
    )
    data.update(kw)
    return evaluate(RuleInput(**data), COMPILED)


def sync(store: Store, *names: str) -> None:
    classes = {n: type(n, (Connector,), {"name": n, "default_enabled": True}) for n in names}
    store.sync_sources(classes, NOW)  # type: ignore[arg-type]


def add(store: Store, source: str, source_id: str, days_ago: float = 1, **kw) -> int:
    kw.setdefault("posted_at", NOW - timedelta(days=days_ago))
    run = store.start_run(source, NOW)
    with store.transaction():
        job_id, _ = store.upsert_job(make_job(source=source, source_id=source_id, **kw), run, NOW)
    return job_id
