"""Tests for the dispatcher reclaim-loop circuit breaker.

A worker that dies immediately is reclaimed by the dead-pid guard and then
re-spawned on the next tick forever, because ``reclaim_task`` clears
``consecutive_failures`` (``kanban.failure_limit`` never trips). The breaker in
``gateway/kanban_watchers.py`` blocks the card for an exponential backoff and
hard-blocks it after ``RECLAIM_BLOCK_AFTER`` strikes. These tests pin the pure
pieces: the backoff curve and the profile-scoped strike ledger.
"""
from gateway.kanban_watchers import (
    RECLAIM_BACKOFF_BASE_S,
    RECLAIM_BACKOFF_MAX_S,
    RECLAIM_BLOCK_AFTER,
    _load_reclaim_backoff,
    _reclaim_backoff_path,
    _reclaim_backoff_seconds,
    _save_reclaim_backoff,
)


def test_backoff_curve_is_exponential():
    assert _reclaim_backoff_seconds(1) == RECLAIM_BACKOFF_BASE_S      # 5m
    assert _reclaim_backoff_seconds(2) == 2 * RECLAIM_BACKOFF_BASE_S  # 10m
    assert _reclaim_backoff_seconds(3) == 4 * RECLAIM_BACKOFF_BASE_S  # 20m


def test_backoff_is_capped():
    assert _reclaim_backoff_seconds(50) == RECLAIM_BACKOFF_MAX_S


def test_block_threshold_is_at_least_two():
    # A single dead worker (one strike) must never hard-block: gateway-restart
    # orphans are legitimately reclaimed once.
    assert RECLAIM_BLOCK_AFTER >= 2


def test_ledger_roundtrips_in_the_sandbox_home():
    assert _load_reclaim_backoff() == {}
    _save_reclaim_backoff({"t_x": {"count": 2, "until": 123.0, "board": "b"}})
    got = _load_reclaim_backoff()
    assert got["t_x"]["count"] == 2
    assert got["t_x"]["board"] == "b"


def test_ledger_path_is_inside_the_active_home():
    # Profile-safe: resolved from HERMES_HOME per call, never a hardcoded path.
    import os

    assert str(_reclaim_backoff_path()).startswith(
        os.environ.get("HERMES_HOME", "")
    )


def test_corrupt_ledger_fails_open():
    p = _reclaim_backoff_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    assert _load_reclaim_backoff() == {}
