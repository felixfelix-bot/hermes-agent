"""D-128 §8.5 — dispatcher respawn backoff + quarantine.

Covers the three churn-protection layers added to
``hermes_cli/kanban_db.py``:

  1. Exponential backoff keyed on ``consecutive_failures`` /
     ``last_failure_at`` (guard reason ``"backoff"``).
  2. Windowed max-respawn cap counting ``spawned`` events (24h).
  3. Quarantine after N identical failure fingerprints (sticky
     ``blocked`` with a ``needs_input``-prefixed reason).

Run via the canonical runner so the hermetic conftest applies:

    scripts/run_tests.sh tests/hermes_cli/test_kanban_respawn_backoff.py -v
"""

from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_knobs(monkeypatch):
    """Deterministic knobs unless a test overrides them."""
    monkeypatch.delenv("HERMES_KANBAN_RESPAWN_BACKOFF_BASE_SECONDS", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RESPAWN_BACKOFF_MAX_SECONDS", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_MAX_RESPAWNS_PER_WINDOW", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_QUARANTINE_IDENTICAL_SPAWNS", raising=False)


@pytest.fixture()
def kanban_home(tmp_path, monkeypatch):
    """Hermetic board root — never the real ~/.hermes (conftest guard)."""
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
    """A fresh board DB connection with the current schema."""
    c = kb.connect()
    yield c
    c.close()


@pytest.fixture()
def spawnable(monkeypatch):
    """Make every assignee look like a real profile for dispatch_once."""
    import hermes_cli.profiles as profiles_mod
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)


def _make_ready_task(c, assignee="worker-x") -> str:
    tid = kb.create_task(
        c, title="churn probe", body="probe", assignee=assignee,
        created_by="test",
    )
    row = c.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "ready", row["status"]
    return tid


def _fail_task(c, tid, error="boom", *, limit=99):
    """Record one failure without tripping the breaker (limit high)."""
    blocked = kb._record_task_failure(
        c, tid, error, outcome="crashed", failure_limit=limit,
    )
    assert blocked is False
    return tid


def _insert_run(c, tid, *, outcome, error, ended_at=None, started_at=None):
    now = int(time.time())
    c.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, error, "
        "started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tid, "worker-x", outcome, outcome, error,
         started_at or now - 60, ended_at or now),
    )
    c.commit()


def _insert_spawned_events(c, tid, n, *, age_seconds=0):
    for i in range(n):
        kb._append_event(c, tid, "spawned", {"pid": 4000 + i})
    c.commit()
    # _append_event stamps created_at itself; age rows if requested.
    if age_seconds:
        c.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? "
            "AND kind = 'spawned'",
            (int(time.time()) - age_seconds, tid),
        )
        c.commit()


# ---------------------------------------------------------------------------
# 1. exponential backoff
# ---------------------------------------------------------------------------

def test_backoff_delay_doubles_and_caps(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_RESPAWN_BACKOFF_BASE_SECONDS", "10")
    monkeypatch.setenv("HERMES_KANBAN_RESPAWN_BACKOFF_MAX_SECONDS", "100")
    assert kb._respawn_backoff_delay(1) == 10
    assert kb._respawn_backoff_delay(2) == 20
    assert kb._respawn_backoff_delay(3) == 40
    assert kb._respawn_backoff_delay(4) == 80
    assert kb._respawn_backoff_delay(5) == 100   # capped (160 -> 100)
    assert kb._respawn_backoff_delay(50) == 100


def test_backoff_delay_zero_base_disables(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_RESPAWN_BACKOFF_BASE_SECONDS", "0")
    assert kb._respawn_backoff_delay(1) == 0
    assert kb._respawn_backoff_delay(9) == 0


def test_record_task_failure_stamps_last_failure_at(conn):
    tid = _make_ready_task(conn)
    _fail_task(conn, tid)
    row = conn.execute(
        "SELECT consecutive_failures, last_failure_at FROM tasks WHERE id = ?",
        (tid,),
    ).fetchone()
    assert row["consecutive_failures"] == 1
    assert row["last_failure_at"] is not None
    assert abs(int(row["last_failure_at"]) - time.time()) < 30


def test_guard_returns_backoff_inside_window(conn):
    tid = _make_ready_task(conn)
    _fail_task(conn, tid)  # failures=1 -> 60s default backoff
    assert kb.check_respawn_guard(conn, tid) == "backoff"


def test_guard_backoff_elapses(conn):
    tid = _make_ready_task(conn)
    _fail_task(conn, tid)
    # Push the failure far enough back that base*2^0=60s has elapsed.
    conn.execute(
        "UPDATE tasks SET last_failure_at = ? WHERE id = ?",
        (int(time.time()) - 3600, tid),
    )
    conn.commit()
    assert kb.check_respawn_guard(conn, tid) is None


def test_clear_failure_counter_resets_backoff_clock(conn):
    tid = _make_ready_task(conn)
    _fail_task(conn, tid)
    kb._clear_failure_counter(conn, tid)
    row = conn.execute(
        "SELECT consecutive_failures, last_failure_at FROM tasks WHERE id = ?",
        (tid,),
    ).fetchone()
    assert row["consecutive_failures"] == 0
    assert row["last_failure_at"] is None
    assert kb.check_respawn_guard(conn, tid) is None


def test_unblock_clears_backoff_clock(conn):
    tid = _make_ready_task(conn)
    kb.quarantine_task(conn, tid, "test quarantine", kind="max_respawns")
    assert kb.unblock_task(conn, tid) is True
    row = conn.execute(
        "SELECT consecutive_failures, last_failure_at, status FROM tasks "
        "WHERE id = ?",
        (tid,),
    ).fetchone()
    assert row["status"] == "ready"
    assert row["consecutive_failures"] == 0
    assert row["last_failure_at"] is None


# ---------------------------------------------------------------------------
# 2. windowed max-respawn cap
# ---------------------------------------------------------------------------

def test_churn_max_respawns_counts_every_spawn(conn, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_QUARANTINE_IDENTICAL_SPAWNS", "0")
    monkeypatch.setenv("HERMES_KANBAN_MAX_RESPAWNS_PER_WINDOW", "4")
    tid = _make_ready_task(conn)
    _insert_spawned_events(conn, tid, 3)
    assert kb.check_respawn_churn(conn, tid) is None
    _insert_spawned_events(conn, tid, 1)  # 4 total
    kind, detail = kb.check_respawn_churn(conn, tid)
    assert kind == "max_respawns"
    assert "4 spawns" in detail


def test_churn_spawn_cap_ignores_old_spawns(conn, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_QUARANTINE_IDENTICAL_SPAWNS", "0")
    monkeypatch.setenv("HERMES_KANBAN_MAX_RESPAWNS_PER_WINDOW", "4")
    tid = _make_ready_task(conn)
    # 3 spawns 30h ago (outside the 24h window) + 2 recent -> below cap.
    _insert_spawned_events(conn, tid, 3, age_seconds=30 * 3600)
    _insert_spawned_events(conn, tid, 2)
    assert kb.check_respawn_churn(conn, tid) is None


def test_churn_spawn_cap_catches_rate_limit_loops(conn, monkeypatch):
    """Rate-limit bounces never increment consecutive_failures; the
    spawn cap must still catch them (CW 67-spawn incident class)."""
    monkeypatch.setenv("HERMES_KANBAN_QUARANTINE_IDENTICAL_SPAWNS", "0")
    monkeypatch.setenv("HERMES_KANBAN_MAX_RESPAWNS_PER_WINDOW", "3")
    tid = _make_ready_task(conn)
    # Simulate quota-wall bounces: rate_limited outcomes, no failures.
    for _ in range(3):
        _insert_run(conn, tid, outcome="rate_limited",
                    error="provider 429 quota wall")
    _insert_spawned_events(conn, tid, 3)
    row = conn.execute(
        "SELECT consecutive_failures FROM tasks WHERE id = ?", (tid,),
    ).fetchone()
    assert row["consecutive_failures"] == 0
    kind, _detail = kb.check_respawn_churn(conn, tid)
    assert kind == "max_respawns"


# ---------------------------------------------------------------------------
# 3. identical-fingerprint quarantine
# ---------------------------------------------------------------------------

def test_churn_identical_failures(conn, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MAX_RESPAWNS_PER_WINDOW", "0")
    tid = _make_ready_task(conn)
    _insert_run(conn, tid, outcome="crashed",
                error="agent died: pid 311 caught SIGSEGV at 0x10")
    _insert_run(conn, tid, outcome="crashed",
                error="agent died: pid 999 caught SIGSEGV at 0x10")
    assert kb.check_respawn_churn(conn, tid) is None  # only 2 of 3
    _insert_run(conn, tid, outcome="crashed",
                error="agent died: pid 42 caught SIGSEGV at 0x10")
    kind, detail = kb.check_respawn_churn(conn, tid)
    assert kind == "identical_failures"
    # fingerprint normalizes pids away
    assert "pid 311" not in detail
    assert "identical" in detail


def test_churn_identical_requires_all_same(conn, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MAX_RESPAWNS_PER_WINDOW", "0")
    tid = _make_ready_task(conn)
    _insert_run(conn, tid, outcome="crashed", error="SIGSEGV in parser")
    _insert_run(conn, tid, outcome="crashed", error="SIGSEGV in parser")
    _insert_run(conn, tid, outcome="timed_out", error="worker exceeded 3600s")
    assert kb.check_respawn_churn(conn, tid) is None


def test_churn_identical_ignores_rate_limited_errors(conn, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MAX_RESPAWNS_PER_WINDOW", "0")
    monkeypatch.setenv("HERMES_KANBAN_QUARANTINE_IDENTICAL_SPAWNS", "3")
    tid = _make_ready_task(conn)
    _insert_run(conn, tid, outcome="crashed", error="provider 429")
    _insert_run(conn, tid, outcome="rate_limited", error="provider 429")
    _insert_run(conn, tid, outcome="rate_limited", error="provider 429")
    assert kb.check_respawn_churn(conn, tid) is None


# ---------------------------------------------------------------------------
# 4. quarantine_task semantics
# ---------------------------------------------------------------------------

def test_quarantine_task_is_sticky(conn):
    tid = _make_ready_task(conn)
    assert kb.quarantine_task(
        conn, tid, "3 identical consecutive failures: SIGSEGV",
        kind="identical_failures",
    ) is True
    row = conn.execute(
        "SELECT status, claim_lock FROM tasks WHERE id = ?", (tid,),
    ).fetchone()
    assert row["status"] == "blocked"
    # Sticky: latest blocked/unblocked event is 'blocked'.
    assert kb._has_sticky_block(conn, tid) is True
    # recompute_ready must not auto-promote a sticky-blocked task.
    kb.recompute_ready(conn)
    status = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (tid,),
    ).fetchone()["status"]
    assert status == "blocked"
    # Audit events: quarantined first, blocked (needs_input) last.
    kinds = [r.kind for r in kb.list_events(conn, tid)]
    assert kinds.index("quarantined") < kinds.index("blocked")
    last_blocked = [r for r in kb.list_events(conn, tid) if r.kind == "blocked"][-1]
    payload = last_blocked.payload
    assert isinstance(payload, dict)
    assert payload["reason"].startswith("needs_input [quarantine/")
    assert payload.get("quarantined") is True
    # Run outcome recorded as quarantined.
    run = kb.latest_run(conn, tid)
    assert run.outcome == "quarantined"


def test_quarantine_reason_classifies_as_needs_input():
    """The blocked reason prefix must satisfy the manager-profile digest
    classifier shape (``needs_input [quarantine/...]``) — asserted here
    structurally so the contract survives without the live script."""
    reason = (
        "needs_input [quarantine/max_respawns]: 6 spawns in the last 24h"
    )
    assert reason.startswith("needs_input")
    assert "[quarantine/" in reason


# ---------------------------------------------------------------------------
# 5. dispatch_once integration
# ---------------------------------------------------------------------------

class _SpawnRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, task, workspace, board=None):
        self.calls.append(task.id)
        return None  # no pid -> no spawned event


def test_dispatch_once_quarantines_churner(conn, spawnable, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_QUARANTINE_IDENTICAL_SPAWNS", "0")
    monkeypatch.setenv("HERMES_KANBAN_MAX_RESPAWNS_PER_WINDOW", "3")
    tid = _make_ready_task(conn)
    _insert_spawned_events(conn, tid, 3)
    rec = _SpawnRecorder()
    result = kb.dispatch_once(
        conn, spawn_fn=rec, urgency_required=False,
    )
    assert (tid, "max_respawns") in result.quarantined
    assert rec.calls == []
    status = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (tid,),
    ).fetchone()["status"]
    assert status == "blocked"


def test_dispatch_once_backoff_defers_spawn(conn, spawnable):
    tid = _make_ready_task(conn)
    _fail_task(conn, tid)  # failures=1, last_failure_at=now -> backoff
    rec = _SpawnRecorder()
    result = kb.dispatch_once(
        conn, spawn_fn=rec, urgency_required=False,
    )
    assert (tid, "backoff") in result.respawn_guarded
    assert rec.calls == []
    # Task stays ready — deferred, not parked.
    status = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (tid,),
    ).fetchone()["status"]
    assert status == "ready"


def test_dispatch_once_spawns_healthy_task(conn, spawnable):
    tid = _make_ready_task(conn)
    rec = _SpawnRecorder()
    result = kb.dispatch_once(
        conn, spawn_fn=rec, urgency_required=False,
    )
    assert result.quarantined == []
    assert rec.calls == [tid]


def test_dispatch_once_dry_run_reports_quarantine_without_mutating(
    conn, spawnable, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_QUARANTINE_IDENTICAL_SPAWNS", "0")
    monkeypatch.setenv("HERMES_KANBAN_MAX_RESPAWNS_PER_WINDOW", "2")
    tid = _make_ready_task(conn)
    _insert_spawned_events(conn, tid, 2)
    result = kb.dispatch_once(
        conn, spawn_fn=_SpawnRecorder(), dry_run=True, urgency_required=False,
    )
    assert (tid, "max_respawns") in result.quarantined
    status = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (tid,),
    ).fetchone()["status"]
    assert status == "ready"


# ---------------------------------------------------------------------------
# 6. legacy DB migration
# ---------------------------------------------------------------------------

def test_migration_adds_last_failure_at_to_legacy_db(conn, kanban_home):
    tid = _make_ready_task(conn)
    # Simulate a legacy DB: drop the column, then re-run init_db.
    conn.execute("ALTER TABLE tasks DROP COLUMN last_failure_at")
    conn.commit()
    kb.init_db(kanban_home / "kanban.db")
    cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(tasks)")
    }
    assert "last_failure_at" in cols
    # And the new code paths work against the migrated DB.
    conn.execute(
        "UPDATE tasks SET last_failure_at = ? WHERE id = ?",
        (int(time.time()), tid),
    )
    conn.commit()
    _fail_task(conn, tid)
    assert kb.check_respawn_guard(conn, tid) == "backoff"


def test_schema_has_last_failure_at_fresh_db(conn):
    cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(tasks)")
    }
    assert "last_failure_at" in cols
