"""Tests for the hoisted dead-pid strike pass (W3, board card t_8fc05744).

Context
-------
``gateway/kanban_watchers.py`` grew a reclaim-loop circuit breaker (commit
``10bbb0a8d8``): every ``running`` task whose worker pid is dead gets a strike
and is blocked for an exponential backoff, and hard-blocked after
``RECLAIM_BLOCK_AFTER`` strikes. The pass was placed *inside* the
``running >= target`` deadlock guard in the dispatcher tick, so with the fleet
below its capacity target it never ran at all — and the per-board
``dispatch_once`` reclaimed the dead worker through ``detect_crashed_workers``
(outcome ``crashed``), which deliberately clears ``consecutive_failures``. The
card was therefore re-spawned on the next tick forever without ever accruing a
strike (live: 4-6 crashes/hour per card, ``fleet_loop_guard`` reporting "no
looping cards").

These tests pin:

* the strike/backoff/hard-block behaviour of the pass itself (real SQLite rows,
  not mocks, so the ``tasks``/``task_runs`` join is exercised);
* the guards that keep a now-every-tick pass from striking *healthy* workers
  (host-local claims, launch-window grace) — both borrowed from
  ``kanban_db.detect_crashed_workers``;
* the wiring: the pass must be called unconditionally, not under the fleet-cap
  guard (asserted structurally via AST, because ``_tick_once`` is a closure
  inside a gateway watcher coroutine and cannot be driven directly).
"""
from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

import gateway.kanban_watchers as kw
from gateway.kanban_watchers import (
    RECLAIM_BACKOFF_BASE_S,
    RECLAIM_BLOCK_AFTER,
    _strike_dead_pid_workers,
)


# --- fixtures / fakes -------------------------------------------------------

TASKS_DDL = (
    "CREATE TABLE tasks ("
    " id TEXT PRIMARY KEY, status TEXT, worker_pid INTEGER,"
    " claim_lock TEXT, started_at INTEGER, current_run_id INTEGER)"
)
RUNS_DDL = (
    "CREATE TABLE task_runs ("
    " id INTEGER PRIMARY KEY, task_id TEXT, status TEXT, started_at INTEGER)"
)


class _NoCloseConn:
    """Connection proxy that ignores ``close()``.

    The real ``kanban_db.connect(board=...)`` opens a fresh connection per
    board and the pass closes it; the fake shares one in-memory connection so
    the assertions can keep reading it.
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        return None


class _FakeKb:
    """Minimal stand-in for the ``hermes_cli.kanban_db`` module surface."""

    DEFAULT_BOARD = "default"

    def __init__(self, conn, alive=(), grace=30, host="hostA"):
        self.conn = conn
        self.alive = set(alive)
        self.grace = grace
        self.host = host
        self.blocked: list[tuple[str, str]] = []
        # pid_alive calls, so a test can prove the pass skipped a task
        # *before* probing liveness (grace / host-local cases).
        self.pid_probes: list[int] = []

    def connect(self, board=None):
        return _NoCloseConn(self.conn)

    def _pid_alive(self, pid):
        self.pid_probes.append(int(pid))
        return int(pid) in self.alive

    def _resolve_crash_grace_seconds(self):
        return self.grace

    def _claimer_id(self):
        return f"{self.host}:1234"

    def block_task(self, conn, task_id, *, reason=None, **kwargs):
        self.blocked.append((task_id, reason or ""))
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?",
                     (task_id,))
        return True


def _rows_kb(now, *, pid=4242, lock="hostA:999", task_started=None,
             run_started=None, alive=(), task_id="t_dead", status="running",
             grace=30, host="hostA"):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(TASKS_DDL)
    conn.execute(RUNS_DDL)
    conn.execute(
        "INSERT INTO task_runs (id, task_id, status, started_at) "
        "VALUES (1, ?, 'running', ?)",
        (task_id, now - 60 if run_started is None else run_started),
    )
    conn.execute(
        "INSERT INTO tasks (id, status, worker_pid, claim_lock, started_at,"
        " current_run_id) VALUES (?, ?, ?, ?, ?, 1)",
        (task_id, status, pid, lock,
         now - 3600 if task_started is None else task_started),
    )
    conn.commit()
    return _FakeKb(conn, alive=alive, grace=grace, host=host), conn


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    """Isolate the strike ledger (resolved from HERMES_HOME per call)."""
    path = tmp_path / "reclaim_backoff.json"

    def _path():
        return path

    monkeypatch.setattr(kw, "_reclaim_backoff_path", _path)
    return path


BOARD = [{"slug": "default"}]


def _status(conn, task_id):
    return conn.execute("SELECT status FROM tasks WHERE id = ?",
                        (task_id,)).fetchone()["status"]


# --- behaviour --------------------------------------------------------------

def test_dead_pid_worker_is_struck_and_blocked_for_a_backoff(ledger):
    kb, conn = _rows_kb(1000000)
    acted = _strike_dead_pid_workers(kb, BOARD, now_fn=lambda: 1000000)

    assert acted == ["t_dead"]
    assert [t for t, _ in kb.blocked] == ["t_dead"]
    assert kb.blocked[0][1].startswith("reclaim backoff until ")
    assert "dead worker pid, strike 1" in kb.blocked[0][1]
    assert _status(conn, "t_dead") == "blocked"
    entry = kw._load_reclaim_backoff()["t_dead"]
    assert entry["count"] == 1
    assert entry["until"] == 1000000 + RECLAIM_BACKOFF_BASE_S
    assert entry["board"] == "default"


def test_live_worker_is_never_struck(ledger):
    kb, conn = _rows_kb(1000000, alive=(4242,))
    assert _strike_dead_pid_workers(kb, BOARD, now_fn=lambda: 1000000) == []
    assert kb.blocked == []
    assert kw._load_reclaim_backoff() == {}
    assert _status(conn, "t_dead") == "running"


def test_hard_block_after_the_strike_threshold(ledger):
    kb, conn = _rows_kb(1000000)
    kw._save_reclaim_backoff(
        {"t_dead": {"count": RECLAIM_BLOCK_AFTER - 1, "until": 0,
                    "board": "default"}}
    )
    acted = _strike_dead_pid_workers(kb, BOARD, now_fn=lambda: 1000000)

    assert acted == ["t_dead"]
    reason = kb.blocked[0][1]
    assert reason.startswith("reclaim loop: worker died "
                             f"{RECLAIM_BLOCK_AFTER}x")
    assert "needs human" in reason
    # The ledger entry is dropped on a hard block (a human owns the card now).
    assert "t_dead" not in kw._load_reclaim_backoff()


def test_freshly_started_task_inside_the_launch_grace_is_skipped(ledger):
    # Mirror of kanban_db.detect_crashed_workers: tasks.started_at (the first
    # time the task ever started) drives the grace, NOT the current run's
    # start, so a re-dispatched crashing card still accrues strikes.
    kb, conn = _rows_kb(1000000, task_started=1000000 - 5)
    assert _strike_dead_pid_workers(kb, BOARD, now_fn=lambda: 1000000) == []
    assert kb.blocked == []
    assert kb.pid_probes == []          # skipped before any liveness probe


def test_claim_from_another_host_is_skipped(ledger):
    # PIDs are host-local; a pid written by another dispatcher means nothing
    # here (the pass now runs on every tick, so this guard matters).
    kb, conn = _rows_kb(1000000, lock="hostB:424242")
    assert _strike_dead_pid_workers(kb, BOARD, now_fn=lambda: 1000000) == []
    assert kb.blocked == []
    assert kb.pid_probes == []


def test_task_without_a_worker_pid_is_skipped(ledger):
    kb, conn = _rows_kb(1000000, pid=None)
    assert _strike_dead_pid_workers(kb, BOARD, now_fn=lambda: 1000000) == []
    assert kb.blocked == []


def test_non_running_tasks_are_not_scanned(ledger):
    kb, conn = _rows_kb(1000000, status="ready")
    assert _strike_dead_pid_workers(kb, BOARD, now_fn=lambda: 1000000) == []
    assert kb.blocked == []


def test_recovered_task_keeps_its_ledger_entry_across_a_second_strike(ledger):
    kb, conn = _rows_kb(1000000)
    _strike_dead_pid_workers(kb, BOARD, now_fn=lambda: 1000000)
    # Second dead worker on the same card: strike 2 → 2x backoff.
    conn.execute("UPDATE tasks SET status = 'running' WHERE id = 't_dead'")
    _strike_dead_pid_workers(kb, BOARD, now_fn=lambda: 1000000)
    entry = kw._load_reclaim_backoff()["t_dead"]
    assert entry["count"] == 2
    assert entry["until"] == 1000000 + 2 * RECLAIM_BACKOFF_BASE_S


# --- wiring -----------------------------------------------------------------

def _cap_gated_call_lines(src: str, name: str) -> list[int]:
    """Line numbers of ``name(...)`` calls sitting inside a guard whose test
    mentions the fleet-cap count (``_count_running_across_boards``)."""
    tree = ast.parse(src)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    hits: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == name):
            continue
        anc = parents.get(node)
        while anc is not None:
            if isinstance(anc, (ast.If, ast.While)):
                seg = ast.get_source_segment(src, anc.test) or ""
                if "_count_running_across_boards" in seg:
                    hits.append(node.lineno)
                    break
            anc = parents.get(anc)
    return hits


def test_dead_pid_strike_pass_runs_every_tick_not_only_at_capacity():
    src = Path(kw.__file__).read_text()
    assert "_strike_dead_pid_workers(_kb, boards)" in src, (
        "dispatcher tick no longer calls the dead-pid strike pass"
    )
    assert _cap_gated_call_lines(src, "_strike_dead_pid_workers") == [], (
        "dead-pid strike pass is back inside the `running >= target` gate: "
        "below capacity it never runs, so the reclaim loop is unbounded"
    )


def test_cap_gate_detector_actually_detects_the_gate():
    # Guard against a vacuous wiring test: the detector must flag a call that
    # *is* nested in the fleet-cap guard.
    sample = (
        "def tick():\n"
        "    if _count_running_across_boards(boards) >= target:\n"
        "        _strike_dead_pid_workers(_kb, boards)\n"
    )
    assert _cap_gated_call_lines(sample, "_strike_dead_pid_workers") == [3]
