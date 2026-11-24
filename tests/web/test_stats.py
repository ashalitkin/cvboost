from __future__ import annotations

from datetime import timedelta

from cvboost.config import Settings
from cvboost.llm.prompts.registry import score_version
from cvboost.stats import compute
from cvboost.storage.db import Store, connect
from tests.conftest import NOW
from tests.llm.helpers import RV
from tests.web.conftest import Ids


def _compute(settings: Settings, period: str):
    conn = connect(settings.db_path)
    try:
        return compute(
            Store(conn),
            period=period,
            now=NOW,
            rule_version=RV,
            relevance_threshold=0,
            prompt_version=score_version(),
            fit_threshold=60,
            batch_size=2,
            per_cycle=1,
            tick_minutes=10,
            max_age_days=30,
        )
    finally:
        conn.close()


def test_funnel_numbers(settings, seeded: Ids):
    s = _compute(settings, "all")
    # 8 jobs, 2 are duplicates of another group (a_remotive, mixed_dead)
    assert s.funnel["collected"] == 8 and s.funnel["unique"] == 6
    assert s.funnel["passed"] == 6 and s.funnel.get("rejected", 0) == 0
    assert s.funnel["scored"] == 2 and s.funnel["eligible"] == 2 and s.funnel["fit"] == 2
    by = {r.source: r for r in s.sources}
    assert by["remoteok"].unique == 1 and by["remoteok"].fit == 1
    assert by["remotive"].collected == 4 and by["remotive"].unique == 3
    assert by["remotive"].useful_pct == 33
    q = s.queue
    # 4 kept groups without a verdict, batch of 2 -> 2 batches -> 2 cycles -> 20 min
    assert (q.waiting, q.batches, q.cycles, q.eta_minutes) == (4, 2, 2, 20)


def test_period_filters_by_first_seen(settings, seeded: Ids):
    conn = connect(settings.db_path)
    old = (NOW - timedelta(days=3)).isoformat()
    conn.execute("UPDATE jobs SET first_seen_at = ? WHERE id = ?", (old, seeded.b))
    conn.commit()
    conn.close()
    assert _compute(settings, "24h").funnel["unique"] == 5
    assert _compute(settings, "7d").funnel["unique"] == 6


def test_reject_reasons_decisions_and_applications(settings, seeded: Ids):
    conn = connect(settings.db_path)
    conn.execute(
        "UPDATE verdicts SET decision='reject', reasons_json='[\"us_only\"]' "
        "WHERE stage='rules' AND job_id=?",
        (seeded.b,),
    )
    store = Store(conn)
    store.record_decision(seeded.a_remoteok, "rejected", reason="rate", channel="panel", now=NOW)
    store.record_decision(seeded.xss, "interested", channel="panel", now=NOW)
    conn.commit()
    conn.close()
    s = _compute(settings, "all")
    assert s.reject_reasons == [("us_only", 1)] and s.rejected_groups == 1
    assert s.no_reasons == [("rate", 1)]
    assert s.decisions == {"rejected": 1, "interested": 1}


def test_stats_page_renders(client, seeded: Ids):
    r = client.get("/stats")
    assert r.status_code == 200
    for text in ("Statistics", "Funnel", "Haiku queue", "LLM calls today", "remoteok"):
        assert text in r.text
    assert 'aria-current="page"' in r.text
    assert "style=" not in r.text
    assert "No rejections." in r.text
    for p in ("24h", "7d", "all", "bogus"):
        assert client.get(f"/stats?period={p}").status_code == 200
