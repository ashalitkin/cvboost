from __future__ import annotations

import json
from datetime import timedelta

from cvboost.explain import short_evidence
from tests.conftest import NOW
from tests.llm.helpers import RV
from tests.rules_helpers import add
from tests.web.conftest import Ids, db, llm_verdict, verdict


def rejected_job(store, reasons, evidence, **kw) -> int:
    j = add(
        store, "remotive", kw.pop("sid", "90"), company="Rej", title=kw.pop("title", "Rej Role")
    )
    store.insert_verdict(
        job_id=j,
        stage="rules",
        decision="reject",
        reasons=reasons,
        flags=[],
        relevance=kw.pop("relevance", 60),
        profile="data_engineer",
        details={"evidence": evidence},
        rule_version=RV,
        input_hash=f"rej{j}",
        now=NOW,
    )
    return j


def finish_cv(store, cid, rec="skip", summary="Нет фрод-опыта. Шансы низкие."):
    _, rid, _ = store.request_cv(cid, channel="panel", now=NOW)
    store.conn.execute(
        "UPDATE cv_requests SET status='done', recommendation=?, fit_summary=? WHERE id=?",
        (rec, summary, rid),
    )
    return rid


def test_skip_badge_and_summary_in_inbox_applications_and_job(client, seeded: Ids, settings):
    s = db(settings)
    finish_cv(s, seeded.b)
    s.record_decision(seeded.b, "interested", channel="panel", now=NOW)  # stays in the default list
    for url, in_page in (
        ("/inbox?decision=all", "Нет фрод-опыта. Шансы низкие."),
        ("/applications", "Нет фрод-опыта. Шансы низкие."),
        (f"/jobs/{seeded.b}", "Нет фрод-опыта. Шансы низкие."),
    ):
        page = client.get(url).text
        assert in_page in page, url
        assert "rec-skip" in page and "Skip" in page, url
    assert f'href="/jobs/{seeded.b}#kit"' in client.get("/applications").text


def test_rules_ok_line_and_haiku_ok(client, seeded: Ids):
    page = client.get("/inbox").text
    assert "Rules:</strong> passed" in page
    assert "Eligible" in page and "fit 85" in page and "Контракт, удалённо." in page


def test_haiku_not_eligible_shown_in_rejected_filter_only(client, seeded: Ids, settings):
    s = db(settings)
    llm_verdict(s, seeded.b, verdict(eligible=False, fit=30, summary_ru="Только для граждан США."))
    assert "Только для граждан США." not in client.get("/inbox").text
    page = client.get("/inbox?eligible=rejected").text
    assert "Not eligible" in page and "fit 30" in page and "Только для граждан США." in page
    assert "Screened out" in page
    assert "Senior Data Engineer" not in page  # eligible groups are not "screened out"


def test_rule_reject_shows_label_and_evidence(client, seeded: Ids, settings):
    s = db(settings)
    j = rejected_job(
        s,
        ["us_only", "onsite", "ruritania_no_rate", "region_eastern_europe"],
        {
            "us_only": "location 'US only'",
            "onsite": "description: <script>alert(1)</script>" + "x" * 300,
        },
    )
    assert "Rej Role" not in client.get("/inbox").text  # hidden by default
    page = client.get("/inbox?eligible=rejected").text
    assert "Rej Role" in page
    for label in ("US only", "Office / hybrid", "Ruritania, no rate stated"):
        assert label in page
    assert "Eastern Europe only" in page
    assert "location &#39;US only&#39;" in page
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page
    assert "x" * 121 not in page
    job_page = client.get(f"/jobs/{j}").text
    assert "US only" in job_page and "❌" in job_page


def test_low_relevance_only_rejects_when_best_is_under_threshold(client, seeded: Ids, settings):
    s = db(settings)
    rejected_job(s, ["low_relevance"], {"low_relevance": "relevance 10 < 40"}, relevance=10,
                 title="Weak Role", sid="91")  # fmt: skip
    page = client.get("/inbox?eligible=rejected").text
    assert "Weak Role" in page and "Low relevance" in page and "relevance 10 &lt; 40" in page


def test_unknown_reason_falls_back_to_code(client, seeded: Ids, settings):
    s = db(settings)
    rejected_job(s, ["brand_new_rule"], {}, title="Odd Role", sid="92")
    assert "brand_new_rule" in client.get("/inbox?eligible=rejected").text


def test_rate_evidence_is_readable():
    def rate(period, mx, cur="USD"):
        d = {"source": "fields", "currency": cur, "period": period, "max": mx, "usd_equiv": 3672}
        return short_evidence("rate_below_min", json.dumps(d))

    assert rate("hour", 40) == "40 USD/h"
    assert rate("month", 5000) == "5000 USD/mo"
    assert rate("year", 52000, "EUR") == "52000 EUR/yr"
    assert rate("day", 300) == "300 USD/day"
    assert short_evidence("rate_below_min", "not json") == "not json"
    assert short_evidence("onsite", None) == ""
    assert len(short_evidence("onsite", "y" * 500)) == 120


def test_cv_verdict_is_escaped(client, seeded: Ids, settings):
    s = db(settings)
    finish_cv(s, seeded.b, rec="apply", summary="<img src=x onerror=alert(1)>")
    page = client.get(f"/jobs/{seeded.b}").text
    assert "&lt;img src=x onerror=alert(1)&gt;" in page and "<img src=x" not in page
    assert "Apply" in page


def test_job_without_any_verdict_has_placeholder(client, seeded: Ids, settings):
    s = db(settings)
    j = add(s, "remotive", "99", company="New", title="Fresh")
    assert "has not been scored" in client.get(f"/jobs/{j}").text


def test_htmx_decision_on_rejected_row_rerenders_it(client, seeded: Ids, settings):
    from tests.web.conftest import csrf

    s = db(settings)
    j = rejected_job(
        s, ["onsite"], {"onsite": "description: office"}, title="Office Role", sid="95"
    )
    r = client.post(
        f"/jobs/{j}/decision",
        data={"csrf_token": csrf(client), "action": "interested"},
        headers={"hx-request": "true"},
    )
    assert r.status_code == 200 and "Office Role" in r.text and "Office / hybrid" in r.text


def test_rejected_cap_note_and_since_days(settings, seeded: Ids):
    s = db(settings)
    for n in range(3):
        rejected_job(s, ["onsite"], {}, title=f"R{n}", sid=f"8{n}")
    s.conn.execute(
        "UPDATE jobs SET posted_at = ? WHERE title = 'R0'", ("2020-01-01T00:00:00+00:00",)
    )
    kept, rej, capped = s.classified_groups(RV, 40, rejected_limit=2)
    assert capped and len(rej) == 2 and kept
    _, rej, capped = s.classified_groups(RV, 40, rejected_since=NOW - timedelta(days=30))
    assert not capped and {g.title for g in rej} == {"R1", "R2"}


def test_reasons_follow_rule_order_low_relevance_last():
    from cvboost.storage.db import rule_outcome

    assert rule_outcome(["low_relevance", "onsite", "us_only", "zzz"], 5, 40) == [
        "us_only",
        "onsite",
        "zzz",
        "low_relevance",
    ]
    assert rule_outcome(["low_relevance"], 50, 40) == []
