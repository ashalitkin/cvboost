from __future__ import annotations

import re
from collections.abc import Callable

from cvboost.core.rules import rule_version
from cvboost.llm.client import OK, LLMRequest, LLMResult, PreflightResult
from cvboost.llm.config import LLMConfig
from cvboost.storage.db import Store
from cvboost.triage import triage
from tests.conftest import NOW
from tests.rules_helpers import CFG, add, sync

DESC = (
    "We are hiring a senior analytics engineer to own our BigQuery warehouse, dbt models and "
    "Airflow pipelines. You will work with SQL and Python every day, build reliable data "
    "models for the product and finance teams, and help us improve the quality of our data. "
    "This is a fully remote contract role open to candidates anywhere in the world."
)
THRESHOLD = CFG.relevance.threshold
RV = rule_version(CFG)


def verdict_dict(**kw) -> dict:
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
    return d


def refs_of(req: LLMRequest) -> list[str]:
    return re.findall(r'<job ref="(j\d+)">', req.stdin_text)


def ok_result(req: LLMRequest, per_ref: dict[str, dict] | None = None, **kw) -> LLMResult:
    per_ref = per_ref or {}
    items = [{"ref": r, **verdict_dict(**per_ref.get(r, {}))} for r in refs_of(req)]
    args = dict(
        status=OK,
        structured={"verdicts": items},
        model="claude-haiku-5-5",
        input_tokens=100,
        output_tokens=50,
        total_cost_usd=0.01,
        duration_ms=1500,
        exit_code=0,
    )
    args.update(kw)
    return LLMResult(**args)


class FakeClient:
    """Answers from a queue of LLMResult or callables(req) -> LLMResult; records requests."""

    def __init__(
        self, *answers: LLMResult | BaseException | Callable[[LLMRequest], LLMResult], default=None
    ):
        self.answers = list(answers)
        self.default = default or ok_result
        self.requests: list[LLMRequest] = []
        self.preflight_result = PreflightResult(True, OK, "claude.ai", None, "2026-11-26T00:00:00Z")
        self.preflights = 0

    def run(self, req: LLMRequest) -> LLMResult:
        self.requests.append(req)
        a = self.answers.pop(0) if self.answers else self.default
        if isinstance(a, BaseException):
            raise a
        return a(req) if callable(a) else a

    def preflight(self) -> PreflightResult:
        self.preflights += 1
        return self.preflight_result


def make_cfg(**over) -> LLMConfig:
    # the code default is UTC (#59); these tests were written for the owner's zone
    data = {"daily_call_caps": {"haiku": 60, "opus": 10}, "timezone": "Europe/London"}
    data.update(over)
    return LLMConfig.model_validate(data)


def seed(store: Store, n: int, **kw) -> list[int]:
    """n distinct kept groups (different companies so dedupe keeps them apart)."""
    sync(store, "fake")
    ids = []
    for i in range(n):
        ids.append(
            add(
                store,
                "fake",
                str(i),
                company=f"Company{chr(65 + i)}",
                title="Senior Analytics Engineer (dbt, BigQuery)",
                description_text=DESC + f" Team number {i}.",
                location_text="Worldwide",
                tags=["dbt", "bigquery", "sql"],
                **kw,
            )
        )
    triage(store, CFG, NOW)
    assert len(store.kept_canonicals(RV, THRESHOLD)) == n
    return ids


def block_of(req: LLMRequest, ref: str) -> str:
    m = re.search(rf'<job ref="{ref}">(.*?)</job>', req.stdin_text, re.S)
    assert m
    return m.group(1)
