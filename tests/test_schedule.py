from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cvboost.schedule import is_due, next_due_at

T0 = datetime(2026, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)


def test_never_run_is_due():
    assert is_due(H, None, 0, T0)


def test_no_failures_waits_interval():
    assert next_due_at(H, T0, 0, T0) == T0 + H
    assert not is_due(H, T0, 0, T0 + timedelta(minutes=59))
    assert is_due(H, T0, 0, T0 + H)


def test_backoff_doubles():
    assert next_due_at(H, T0, 1, T0) == T0 + 2 * H
    assert next_due_at(H, T0, 3, T0) == T0 + 8 * H


def test_backoff_capped_at_24h():
    assert next_due_at(H, T0, 10, T0) == T0 + timedelta(hours=24)


def test_huge_failure_count_does_not_overflow():
    assert next_due_at(H, T0, 1000, T0) == T0 + timedelta(hours=24)
