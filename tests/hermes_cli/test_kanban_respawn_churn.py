"""D-128 §8.5 — dispatcher respawn churn protection (ported onto v0.21.5).

Covers the two churn layers re-sited onto upstream's dispatch guard:

  1. Exponential respawn backoff keyed on ``consecutive_failures`` /
     ``last_failure_at`` (guard reason ``"backoff"``).
  2. Windowed max-respawn cap counting ``spawned`` events over 24h
     (guard reason ``"respawn_churn"``).

Layer 3 (identical-failure quarantine) is NOT ported here: upstream already
sticky-blocks systemic same-error crashes (>= 3 identical fingerprints in one
detect tick) in ``_account_crashes``.

Run via scripts/run_tests.sh so the hermetic conftest applies.
"""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture(autouse=True)
def _clean_knobs(monkeypatch):
    for name in (
        "HERMES_KANBAN_RESPAWN_BACKOFF_BASE_SECONDS",
        "HERMES_KANBAN_RESPAWN_BACKOFF_MAX_SECONDS",
        "HERMES_KANBAN_MAX_RESPAWNS_PER_WINDOW",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture()
def conn(kanban_home):
    c = kbc.connect()
    yield c
    c.close()


def _make_ready_task(c, assignee="worker-x") -> str:
    tid = kb.create_task(
        c, title="churn probe", body="probe", assignee=assignee, created_by="test",
    )
    row = c.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "ready", row["status"]
    return tid


def _fail_task(c, tid, error="boom", *, limit=99):
    blocked = kb._record_task_failure(
        c, tid, error, outcome="crashed", failure_limit=limit,
    )
    assert blocked is False
    return tid


def _insert_spawned_events(c, tid, n, *, age_seconds=0):
    for i in range(n):
        kb._append_event(c, tid, "spawned", {"pid": 4000 + i})
    c.commit()
    if age_seconds:
        c.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? "
            "AND kind = 'spawned'",
            (int(time.time()) - age_seconds, tid),
        )
        c.commit()


# ── pure backoff ────────────────────────────────────────────────────────────

def test_backoff_delay_scales_and_caps():
    assert kbd._respawn_backoff_delay(0) == 0
    assert kbd._respawn_backoff_delay(-3) == 0
    assert kbd._respawn_backoff_delay(1) == 60
    assert kbd._respawn_backoff_delay(2) == 120
    assert kbd._respawn_backoff_delay(3) == 240
    assert kbd._respawn_backoff_delay(50) == 3600  # capped at the max


def test_backoff_layer_disabled_by_zero_base(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_RESPAWN_BACKOFF_BASE_SECONDS", "0")
    assert kbd._respawn_backoff_delay(5) == 0


# ── guard: exponential backoff ──────────────────────────────────────────────

def test_guard_returns_backoff_after_a_recent_failure(conn):
    tid = _make_ready_task(conn)
    _fail_task(conn, tid)
    assert kbd.check_respawn_guard(conn, tid) == "backoff"


def test_guard_allows_spawn_once_backoff_elapses(conn):
    tid = _make_ready_task(conn)
    _fail_task(conn, tid)
    conn.execute(
        "UPDATE tasks SET last_failure_at = ? WHERE id = ?",
        (int(time.time()) - 3600, tid),
    )
    conn.commit()
    assert kbd.check_respawn_guard(conn, tid) is None


# ── guard: windowed max-respawn cap ─────────────────────────────────────────

def test_guard_trips_windowed_spawn_cap(conn):
    tid = _make_ready_task(conn)
    _insert_spawned_events(conn, tid, 6)
    reason = kbd.check_respawn_guard(conn, tid)
    assert reason is not None and reason.startswith("respawn_churn:")


def test_guard_ignores_spawns_outside_the_window(conn):
    tid = _make_ready_task(conn)
    _insert_spawned_events(conn, tid, 6, age_seconds=24 * 3600 + 60)
    assert kbd.check_respawn_guard(conn, tid) is None


def test_churn_layer_disabled_by_zero_cap(conn, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MAX_RESPAWNS_PER_WINDOW", "0")
    tid = _make_ready_task(conn)
    _insert_spawned_events(conn, tid, 20)
    assert kbd.check_respawn_guard(conn, tid) is None
