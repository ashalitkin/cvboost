from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar

import httpx

from cvboost.connectors.base import Connector, FetchOutcome
from cvboost.http import HttpClient
from cvboost.llm.config import LLMConfig
from cvboost.models import Job, RawPosting
from cvboost.score import ScoreStats
from cvboost.worker import WorkerDeps
from cvboost.worker_config import WorkerConfig
from tests.conftest import NOW, PUBLIC_RESOLVER
from tests.rules_helpers import CFG


class FakeClock:
    """Settable clock; each call can advance time by `step` seconds."""

    def __init__(self, start: datetime = NOW, step: float = 0.0):
        self.t = start
        self.step = step

    def __call__(self) -> datetime:
        cur = self.t
        self.t = self.t + timedelta(seconds=self.step)
        return cur

    def advance(self, **kw: float) -> None:
        self.t = self.t + timedelta(**kw)


class FakeConnector(Connector):
    name: ClassVar[str] = "fake"
    kind: ClassVar = "api"
    base_url: ClassVar[str] = "https://example.com"
    interval: ClassVar[timedelta] = timedelta(minutes=10)
    min_delay_s: ClassVar[float] = 0.0
    allowed_hosts: ClassVar = frozenset({"example.com"})
    tos_note: ClassVar[str] = ""
    attribution: ClassVar[str] = ""

    def __init__(self, name: str = "fake", ids: list[str] | None = None, boom: bool = False):
        self.name = name  # type: ignore[misc]
        self.ids = ids or []
        self.boom = boom
        self.fetches = 0

    def requests(self) -> list[str]:
        return []

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        return []

    def fetch_partial(self, http, limit=None) -> FetchOutcome:
        self.fetches += 1
        if self.boom:
            raise RuntimeError("collect exploded")
        return FetchOutcome(
            [RawPosting(source=self.name, source_id=i, payload={}) for i in self.ids]
        )

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        return Job(
            source=self.name,
            source_id=raw.source_id,
            url=f"https://example.com/{self.name}/{raw.source_id}",
            company=f"Company {raw.source_id}",
            title="Senior Analytics Engineer (dbt, BigQuery)",
            description_text="dbt BigQuery Airflow SQL worldwide remote contract",
            location_text="Worldwide",
            tags=["dbt", "bigquery"],
            posted_at=fetched_at,
            fetched_at=fetched_at,
        )


def make_deps(tmp_path: Path, connectors: list[Connector], clock=None, **over) -> WorkerDeps:
    cfg = WorkerConfig.model_validate(over.pop("cfg", {}))
    deps = WorkerDeps(
        cfg=cfg,
        rules=CFG,
        llm_cfg=LLMConfig.model_validate({"daily_call_caps": {"haiku": 60, "opus": 10}}),
        data_dir=tmp_path,
        connectors=connectors,
        http=HttpClient(
            httpx.MockTransport(lambda r: httpx.Response(500)), resolver=PUBLIC_RESOLVER
        ),
        clock=clock or FakeClock(),
        stop=threading.Event(),
        llm_client=object(),  # type: ignore[arg-type]
        lock_wait_s=0,
    )
    for k, v in over.items():
        setattr(deps, k, v)
    return deps


class ScoreRecorder:
    """Fake `score`: records max_calls and returns canned stats."""

    def __init__(self, **stats):
        self.calls: list[dict] = []
        self.stats = stats
        self.on_call = None

    def __call__(self, store, client, cfg, rv, threshold, now, **kw) -> ScoreStats:
        self.calls.append({"now": now, **kw})
        if self.on_call:
            self.on_call()
        return ScoreStats(**self.stats)
