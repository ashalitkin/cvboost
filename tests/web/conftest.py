from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cvboost.config import Settings
from cvboost.llm.prompts.registry import score_version
from cvboost.llm.schema import JobVerdict
from cvboost.storage.db import Store, connect, migrate
from cvboost.web.app import create_app
from tests.conftest import NOW
from tests.llm.helpers import RV
from tests.rules_helpers import RULES_PATH, add, sync

LLM_YAML = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "config" / "llm.yaml"
XSS_HTML = (
    "<script>alert(1)</script><img src=x onerror=alert(1)>"
    '<a href="javascript:alert(1)" hx-get="/sources">x</a>'
    '<p hx-post="/x" hx-trigger="load">t</p><p>kept paragraph</p>'
)


def verdict(**kw) -> JobVerdict:
    d = dict(
        eligible=True,
        eligibility_reason="Remote worldwide contract.",
        fit=80,
        profile="data_engineer",
        seniority="senior",
        employment="contract",
        rate_text=None,
        red_flags=[],
        summary_ru="Контракт, удалённо.",
    )
    d.update(kw)
    return JobVerdict(**d)


def rule_verdict(store: Store, job_id: int, relevance: int = 60) -> None:
    store.insert_verdict(
        job_id=job_id,
        stage="rules",
        decision="keep",
        reasons=[],
        flags=["rate_unknown"],
        relevance=relevance,
        profile="data_engineer",
        details={},
        rule_version=RV,
        input_hash=f"h{job_id}",
        now=NOW,
    )


def llm_verdict(store: Store, job_id: int, v: JobVerdict) -> None:
    store.insert_llm_verdict(
        job_id=job_id,
        verdict=v,
        prompt_version=score_version(),
        item_hash=f"l{job_id}",
        call_id=None,
        model="haiku",
        cached=False,
        now=NOW,
    )


@dataclass
class Ids:
    a_remoteok: int
    a_remotive: int
    b: int
    himalayas_dead: int
    wwr_dead: int
    xss: int
    mixed_alive: int
    mixed_dead: int


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "web.sqlite",
        log_level="INFO",
        rules_path=RULES_PATH,
        llm_config_path=LLM_YAML,
        kits_override=tmp_path / "kits",
        web_hosts=("testserver",),
        web_secret="s" * 40,
    )


@pytest.fixture
def seeded(settings: Settings) -> Ids:
    conn = connect(settings.db_path)
    migrate(conn)
    s = Store(conn)
    sync(s, "remoteok", "remotive", "himalayas", "wwr")
    a1 = add(
        s,
        "remoteok",
        "1",
        company="Acme",
        title="Senior Data Engineer",
        url="https://remoteok.com/remote-jobs/1",
    )
    a2 = add(
        s,
        "remotive",
        "2",
        company="Acme",
        title="Senior Data Engineer",
        url="https://remotive.com/jobs/2",
    )
    conn.execute("UPDATE jobs SET canonical_id = ? WHERE id = ?", (a1, a2))
    b = add(s, "remotive", "3", company="Beta", title="Analytics Engineer")
    dead = (NOW - timedelta(days=2)).timestamp()
    h = add(
        s, "himalayas", "4", company="Gamma", title="Expired Himalayas", raw={"expiryDate": dead}
    )
    w = add(
        s,
        "wwr",
        "5",
        company="Delta",
        title="Expired WWR",
        raw={"expires_at": "Mon, 01 Jan 2026 00:00:00 +0000"},
    )
    x = add(
        s,
        "remotive",
        "6",
        company="Evil <b>Corp</b>",
        title="<script>t</script>",
        description_html=XSS_HTML,
        description_text="plain text body",
    )
    conn.execute("UPDATE jobs SET apply_url = 'javascript:alert(1)' WHERE id = ?", (x,))
    m_ok = add(s, "remotive", "7", company="Mixed", title="Mixed Group Role")
    m_dead = add(
        s,
        "himalayas",
        "8",
        company="Mixed",
        title="Mixed Group Role",
        url="https://www.himalayas.app/jobs/dead-link",
        raw={"expiryDate": dead},
    )
    conn.execute("UPDATE jobs SET canonical_id = ? WHERE id = ?", (m_ok, m_dead))
    for j in (a1, a2, b, h, w, x, m_ok, m_dead):
        rule_verdict(s, j)
    llm_verdict(s, a1, verdict(fit=85))
    llm_verdict(
        s,
        x,
        verdict(
            summary_ru="<b onmouseover=alert(1)>sum</b>",
            red_flags=["<b onmouseover=alert(2)>rf</b>"],
        ),
    )
    run = s.start_run("remotive", NOW)
    s.finish_run(
        run,
        status="error",
        found=0,
        new=0,
        invalid=0,
        error_type="HTTP",
        error_message="<img src=x onerror=alert(1)>",
        now=NOW,
    )
    s.insert_llm_call(
        kind="score",
        model_alias="haiku",
        prompt_version=score_version(),
        n_items=2,
        attempt=1,
        now=NOW,
        local_day="2026-11-06",
        status="ok",
        input_tokens=100,
        output_tokens=50,
        total_cost_usd=0.01,
    )
    conn.close()
    return Ids(a1, a2, b, h, w, x, m_ok, m_dead)


@pytest.fixture
def client(settings: Settings, seeded: Ids) -> TestClient:
    return TestClient(create_app(settings, clock=lambda: NOW), follow_redirects=False)


def csrf(client: TestClient, path: str = "/inbox") -> str:
    r = client.get(path)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
    assert m, r.text[:300]
    return m.group(1)


def db(settings: Settings) -> Store:
    return Store(connect(settings.db_path))
