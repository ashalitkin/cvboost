from __future__ import annotations

from datetime import timedelta

import pytest

from cvboost.notify.alerts import select_candidates, send_pending
from cvboost.score import score
from cvboost.storage.db import Store, connect, migrate
from cvboost.worker_config import WorkerConfig
from tests.conftest import NOW
from tests.llm.helpers import RV, THRESHOLD, FakeClient, make_cfg, seed
from tests.notify.helpers import OWNER, TOKEN, FakeTelegram

LATER = NOW + timedelta(minutes=5)


@pytest.fixture
def s(tmp_path) -> Store:
    conn = connect(tmp_path / "n.sqlite")
    migrate(conn)
    st = Store(conn)
    st.state_set("alerts_since", (NOW - timedelta(hours=1)).isoformat(), NOW)
    return st


@pytest.fixture
def tg() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def sent(s, tg):
    """One scored group, alerted: returns (alert row, message_id)."""
    seed(s, 1)
    score(s, FakeClient(), make_cfg(), RV, THRESHOLD, lambda: NOW, max_calls=3)
    cfg = WorkerConfig()
    select_candidates(s, cfg, LATER)
    send_pending(s, tg.client(), cfg, OWNER, LATER, token=TOKEN)
    a = s.conn.execute("SELECT * FROM alerts").fetchone()
    assert a["status"] == "sent"
    tg.calls.clear()
    return a
