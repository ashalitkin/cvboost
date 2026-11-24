from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cvboost.core.freshness import closed_reason
from tests.conftest import NOW

DAY = timedelta(days=1)


def reason(source="remotive", posted=NOW - DAY, first_seen=NOW, raw=None, age=21):
    return closed_reason(source, posted, first_seen, raw, NOW, age)


def test_himalayas_expiry_epoch():
    past = int((NOW - DAY).timestamp())
    future = int((NOW + DAY).timestamp())
    assert reason("himalayas", raw={"expiryDate": past}) == "expired"
    assert reason("himalayas", raw={"expiryDate": future}) is None


def test_wwr_expires_at_rfc822():
    assert reason("wwr", raw={"expires_at": "Thu, 05 Nov 2026 10:00:00 +0000"}) == "expired"
    assert reason("wwr", raw={"expires_at": "Sun, 06 Dec 2026 17:00:25 +0000"}) is None


def test_stale_by_posted_at_or_first_seen():
    assert reason(posted=NOW - 30 * DAY) == "stale"
    assert reason(posted=NOW - 20 * DAY) is None
    assert reason(posted=None, first_seen=NOW - 30 * DAY) == "stale"
    assert reason(posted=None, first_seen=NOW - DAY) is None


def test_malformed_expiry_is_not_closed():
    for raw in ({"expiryDate": "soon"}, {"expiryDate": None}, {"expiryDate": True}, {}, None):
        assert reason("himalayas", raw=raw) is None
    assert reason("himalayas", raw={"expiryDate": 10**30}) is None
    assert reason("wwr", raw={"expires_at": "garbage"}) is None
    assert reason("wwr", raw={"expires_at": ""}) is None


def test_other_sources_ignore_expiry_fields():
    assert reason("jobicy", raw={"expiryDate": 1}) is None


def test_boundary_is_exclusive():
    assert reason(posted=NOW - 21 * DAY) is None
    assert closed_reason("x", datetime(2020, 1, 1, tzinfo=UTC), NOW, None, NOW, 21) == "stale"
