"""The worker's e-mail step: collect -> email_extract -> triage, with its guards."""

from __future__ import annotations

from datetime import timedelta

import pytest

from cvboost.connectors.email_alerts import EmailAlertsConnector
from cvboost.email_extract import EmailExtractStats
from cvboost.worker import run_cycle
from tests.conftest import NOW
from tests.llm.helpers import FakeClient, make_cfg
from tests.mail.helpers import CREDS, FakeMailbox, eml
from tests.mail.test_email_extract import answer
from tests.worker.helpers import FakeClock, FakeConnector, ScoreRecorder, make_deps


def email_conn(*names: str, creds=CREDS):
    mb = FakeMailbox({i + 1: eml(n) for i, n in enumerate(names)})
    return EmailAlertsConnector(creds, mailbox_factory=mb.factory), mb


class Spy:
    """email_fn that records when it ran and what the store looked like."""

    def __init__(self, new_jobs=0, calls=0, stopped=None):
        self.runs = 0
        self.verdicts_seen: list[int] = []
        self.out = EmailExtractStats(new_jobs=new_jobs, calls=calls, stopped=stopped)

    def __call__(self, store, client, cfg, clock):
        self.runs += 1
        self.verdicts_seen.append(store.conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0])
        return self.out


def test_email_step_runs_after_collect_before_triage_and_counts_new_jobs(store, tmp_path):
    fake = FakeConnector(ids=["1"])
    conn, _ = email_conn("recruiter_unknown.eml")
    store.sync_sources({c.name: type(c) for c in (fake, conn)}, NOW)
    spy = Spy(new_jobs=2, calls=1)
    deps = make_deps(tmp_path, [fake, conn], FakeClock(NOW), score_fn=ScoreRecorder(), email_fn=spy)
    stats = run_cycle(store, deps)
    assert spy.runs == 1 and fake.fetches == 1
    assert spy.verdicts_seen == [0]  # collect had stored job 1; triage had not run yet
    assert (
        store.conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] >= 1
    )  # triage ran after
    assert stats.new_jobs == 1 + 2 and stats.email_calls == 1 and stats.email_jobs == 2


def test_email_step_is_skipped_without_imap_credentials(store, tmp_path):
    conn, _ = email_conn(creds=None)
    store.sync_sources({conn.name: type(conn)}, NOW)
    spy = Spy()
    deps = make_deps(tmp_path, [conn], FakeClock(NOW), score_fn=ScoreRecorder(), email_fn=spy)
    stats = run_cycle(store, deps)
    assert spy.runs == 0 and stats.status == "ok" and stats.email_calls == 0


def test_email_step_is_skipped_when_the_cycle_has_no_time_left(store, tmp_path):
    conn, _ = email_conn("recruiter_unknown.eml")
    store.sync_sources({conn.name: type(conn)}, NOW)
    spy = Spy()
    deps = make_deps(tmp_path, [conn], FakeClock(NOW), score_fn=ScoreRecorder(), email_fn=spy)
    deps.llm_cfg = make_cfg(timeout_s=100000)  # two calls would not fit into any cycle
    stats = run_cycle(store, deps)
    assert spy.runs == 0 and stats.email_stopped == "no time left in this cycle"


def test_whole_flow_recruiter_mail_becomes_a_job_in_the_cycle(store, tmp_path):
    conn, _ = email_conn("recruiter_unknown.eml")
    store.sync_sources({conn.name: type(conn)}, NOW)
    client = FakeClient(answer())
    deps = make_deps(tmp_path, [conn], FakeClock(NOW), score_fn=ScoreRecorder(), llm_client=client)
    stats = run_cycle(store, deps)
    assert len(client.requests) == 1 and stats.email_calls == 1 and stats.email_jobs == 1
    assert stats.new_jobs == 1 and not stats.errors
    assert store.conn.execute("SELECT source FROM jobs").fetchone()[0] == "email:recruiter"


def test_email_step_does_not_call_the_model_while_paused(store, tmp_path):
    conn, _ = email_conn("recruiter_unknown.eml")
    store.sync_sources({conn.name: type(conn)}, NOW)
    store.insert_llm_call(
        kind="score",
        model_alias="haiku",
        prompt_version="p",
        n_items=1,
        attempt=1,
        now=NOW - timedelta(minutes=5),
        local_day="2026-11-06",
        status="usage_limit",
        pause_until=NOW + timedelta(hours=1),
    )
    client = FakeClient(answer())
    deps = make_deps(tmp_path, [conn], FakeClock(NOW), score_fn=ScoreRecorder(), llm_client=client)
    stats = run_cycle(store, deps)
    assert (
        client.requests == [] and stats.email_calls == 0 and "paused" in (stats.email_stopped or "")
    )
    (m,) = store.conn.execute("SELECT status FROM email_messages").fetchall()
    assert m[0] == "needs_llm"


@pytest.fixture(autouse=True)
def _nothing():
    yield
