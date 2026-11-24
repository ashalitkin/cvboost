"""Owner 2026-11-10: Spark / streaming in requirements -> rules reject; optional mentions stay."""

from __future__ import annotations

import pytest

from tests.rules_helpers import ev

BASE = "Analytics Engineer to build dbt models on BigQuery and Looker dashboards for marketing.\n"


@pytest.mark.parametrize(
    "desc",
    [
        "Requirements\n- 5+ years SQL\n- Spark\n",
        "Experience with big data technologies such as Spark, Kafka, Hadoop.",
        "You will build streaming pipelines on Flink.",
        "Hands-on PySpark and Databricks.",
        "Strong expertise with event streaming (Kafka, Kinesis).",
    ],
)
def test_required_stack_rejects(desc):
    r = ev(description_text=BASE + desc)
    assert "unwanted_stack" in r.reasons and r.decision == "reject"


def test_title_mention_rejects():
    assert "unwanted_stack" in ev(title="Spark Data Engineer").reasons


@pytest.mark.parametrize(
    "desc",
    [
        "Nice to have\n- Kafka\n- Flink experience is fine too\n",
        "Nice-to-have\n\nEvent streams from Kafka; streaming anomaly detection (Dataflow/Flink).",
        "Bonus points\nSpark or Databricks.\nRequirements\nSQL and dbt.",
        "Experience with Kafka is a plus.",
        "Familiarity with Spark is preferred.",
        "Preferred qualifications\nApache Beam, Kinesis",
    ],
)
def test_optional_mentions_keep(desc):
    assert "unwanted_stack" not in ev(description_text=BASE + desc).reasons


def test_required_section_after_optional_resets():
    desc = BASE + "Nice to have\nLooker Studio\nRequirements\nKafka in production"
    assert "unwanted_stack" in ev(description_text=desc).reasons


def test_unrelated_words_keep():
    for d in ["We sparkle at data.", "Video streaming company, SQL and dbt.", "Sparkling water"]:
        assert "unwanted_stack" not in ev(description_text=BASE + d).reasons


# --- owner 2026-11-10: full-time salary OK when the top of the range is >= $7k/month ---------


def test_monthly_salary_upper_bound_7k_passes():
    r = ev(salary_text="$5,000–7,000 per month")
    assert "rate_below_min" not in r.reasons


def test_monthly_salary_below_7k_rejects():
    assert "rate_below_min" in ev(salary_text="$4,000–6,500 per month").reasons


def test_annual_84k_passes_and_hourly_min_unchanged():
    assert "rate_below_min" not in ev(salary_text="$70,000 - $84,000 per year").reasons
    assert "rate_below_min" in ev(salary_text="$40/hour").reasons
