"""Zombie-run guard: a worker that outlives its own run must not close a successor's.

Defect (card t_ddcaad8b, observed live on t_87e5657d, 2026-09-13)
--------------------------------------------------------------
``agent/turn_finalizer.py`` records a budget-exhausted failure through
``kanban_db._record_task_failure(..., release_claim=True, end_run=True)``
keyed **only** on ``HERMES_KANBAN_TASK``. A worker process that keeps turning
after it has already handed its card off (``kanban_request_review`` /
``kanban_complete`` close the run, but the CLI keeps spinning until its
iteration budget runs out) therefore targets whatever run is current *then* --
the **next** worker's. In the live incident the zombie closed the reviewer's
42-second run as ``timed_out`` 42 s after spawn, released the reviewer's claim
(refusing its later ``kanban_complete`` as "unknown id or already terminal")
and inflated ``consecutive_failures`` 1 -> 2 on a card with zero real defects.

Invariant pinned here
---------------------
A failure may only be recorded by the process that owns the run it is
attributed to. The dispatcher exports ``HERMES_KANBAN_RUN_ID`` into every
worker env (``kanban_db._default_spawn``), and the finalizer must pass it as
``expected_run_id``. ``_record_task_failure`` then refuses -- recording
nothing, touching no claim, closing no run -- when that id is no longer the
task's ``current_run_id``. Run ownership is explicit; it is never inferred
from the task id alone.

``test_stale_run_id_budget_exhaustion_*`` is the defect-level RED (fails on
the pre-fix code end-to-end, through the real finalizer path);
``test_record_task_failure_*`` pins the shared guard itself so every future
caller inherits it, and ``*_own_run_id_*`` proves the legitimate path is
unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.turn_finalizer import finalize_turn
from hermes_cli import kanban_db as kb
from tests.agent.test_turn_finalizer_iteration_limit_exit import _LimitAgent

_BUDGET_ERROR = (
    "Iteration budget exhausted (90/90) — task could not complete within "
    "the allowed iterations"
)


@pytest.fixture
def conn(tmp_path: Path):
    db = kb.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def _start_review_run(conn):
    """Reproduce the live two-run sequence and return its three key ids.

    ``claim_task`` opens run A; the implementer hands off with
    ``request_review`` (run A closes, ``current_run_id`` clears); the
    dispatcher then claims the card for review and opens run B.
    """
    task_id = kb.create_task(conn, title="Implement the export", assignee="builder")
    implementation = kb.claim_task(conn, task_id, claimer="builder:zombie")
    assert implementation is not None and implementation.current_run_id is not None
    stale_run_id = int(implementation.current_run_id)

    assert kb.request_review(
        conn,
        task_id,
        summary="ready for review",
        reviewer="reviewer",
        expected_run_id=stale_run_id,
    )
    assert kb.get_task(conn, task_id).current_run_id is None

    successor = kb.claim_review_task(conn, task_id)
    assert successor is not None and successor.current_run_id is not None
    successor_run_id = int(successor.current_run_id)
    assert successor_run_id != stale_run_id
    return task_id, stale_run_id, successor_run_id


def _task_row(conn, task_id: str) -> dict:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return {k: row[k] for k in row.keys()}


def _run_row(conn, run_id: int) -> dict:
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    return {k: row[k] for k in row.keys()}


def _event_count(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0])


def _finished(monkeypatch, conn, task_id: str, run_id) -> dict:
    """Run the real finalizer's budget-exhausted path as this worker.

    ``connect`` is redirected to a fresh connection on the same temp DB file so
    the finalizer's own ``close()`` cannot shut the fixture connection.
    """
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    if run_id is None:
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    _real_connect = kb.connect
    monkeypatch.setattr(
        "hermes_cli.kanban_db.connect", lambda *a, **kw: _real_connect(db_path)
    )
    agent = _LimitAgent(max_iterations=90, budget_remaining=0)
    return finalize_turn(
        agent,
        final_response=None,
        api_call_count=90,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": f"work kanban task {task_id}"}],
        conversation_history=[],
        effective_task_id=task_id,
        turn_id="turn",
        user_message=f"work kanban task {task_id}",
        original_user_message=f"work kanban task {task_id}",
        _should_review_memory=False,
        _turn_exit_reason="unknown",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Defect-level regression: the zombie's late failure must not hit the successor
# ─────────────────────────────────────────────────────────────────────────────


def test_stale_run_id_budget_exhaustion_leaves_successor_run_untouched(conn, monkeypatch):
    task_id, stale_run_id, successor_run_id = _start_review_run(conn)
    task_before = _task_row(conn, task_id)
    run_before = _run_row(conn, successor_run_id)
    events_before = _event_count(conn)

    result = _finished(monkeypatch, conn, task_id, stale_run_id)

    assert result["turn_exit_reason"] == "max_iterations_reached(90/90)"

    # The successor's run, its claim and the failure counter are all untouched.
    assert _task_row(conn, task_id) == task_before
    assert _run_row(conn, successor_run_id) == run_before
    assert _event_count(conn) == events_before

    task = kb.get_task(conn, task_id)
    assert task.status == "running"
    assert task.current_run_id == successor_run_id
    assert int(task.consecutive_failures) == 0
    assert task.last_failure_error in (None, "")
    assert not [e for e in kb.list_events(conn, task_id) if e.kind == "timed_out"]


def test_run_id_absent_keeps_legacy_unpinned_behaviour(conn, monkeypatch):
    """No ``HERMES_KANBAN_RUN_ID`` (pre-fix env, direct CLI use) ⇒ legacy path.

    The guard is only armed by an explicit id, so dispatcher-internal callers
    that never had one keep working exactly as before.
    """
    task_id = kb.create_task(conn, title="Implement the export", assignee="builder")
    claimed = kb.claim_task(conn, task_id, claimer="builder:1")
    assert claimed is not None
    run_id = int(claimed.current_run_id)

    _finished(monkeypatch, conn, task_id, None)

    task = kb.get_task(conn, task_id)
    assert task.current_run_id is None
    assert task.status == "ready"
    assert int(task.consecutive_failures) == 1
    run = _run_row(conn, run_id)
    assert run["outcome"] == "timed_out" and run["ended_at"] is not None


# ─────────────────────────────────────────────────────────────────────────────
# The legitimate path still records timed_out and releases the claim
# ─────────────────────────────────────────────────────────────────────────────


def test_own_run_id_budget_exhaustion_records_timeout_and_releases_claim(conn, monkeypatch):
    task_id = kb.create_task(conn, title="Implement the export", assignee="builder")
    claimed = kb.claim_task(conn, task_id, claimer="builder:1")
    assert claimed is not None
    run_id = int(claimed.current_run_id)

    _finished(monkeypatch, conn, task_id, run_id)

    task = kb.get_task(conn, task_id)
    assert task.status == "ready"
    assert task.current_run_id is None
    assert task.claim_lock is None
    assert int(task.consecutive_failures) == 1
    assert "Iteration budget exhausted" in (task.last_failure_error or "")

    run = _run_row(conn, run_id)
    assert run["outcome"] == "timed_out"
    assert run["status"] == "timed_out"
    assert run["ended_at"] is not None
    assert "Iteration budget exhausted" in (run["error"] or "")

    timeouts = [e for e in kb.list_events(conn, task_id) if e.kind == "timed_out"]
    assert len(timeouts) == 1
    assert timeouts[0].run_id == run_id


# ─────────────────────────────────────────────────────────────────────────────
# Shared guard: every caller (not just the finalizer) gets the ownership check
# ─────────────────────────────────────────────────────────────────────────────


def test_record_task_failure_refuses_stale_run_id(conn):
    task_id, stale_run_id, successor_run_id = _start_review_run(conn)
    task_before = _task_row(conn, task_id)
    run_before = _run_row(conn, successor_run_id)
    events_before = _event_count(conn)

    blocked = kb._record_task_failure(
        conn,
        task_id,
        _BUDGET_ERROR,
        outcome="timed_out",
        release_claim=True,
        end_run=True,
        expected_run_id=stale_run_id,
    )

    assert blocked is False
    assert _task_row(conn, task_id) == task_before
    assert _run_row(conn, successor_run_id) == run_before
    assert _event_count(conn) == events_before


def test_record_task_failure_accepts_own_run_id(conn):
    task_id, _stale_run_id, successor_run_id = _start_review_run(conn)

    blocked = kb._record_task_failure(
        conn,
        task_id,
        _BUDGET_ERROR,
        outcome="timed_out",
        release_claim=True,
        end_run=True,
        expected_run_id=successor_run_id,
    )

    assert blocked is False  # below the default failure limit
    task = kb.get_task(conn, task_id)
    # The run was claimed from the review lane, so it retries there.
    assert task.status == "review"
    assert task.current_run_id is None
    assert task.claim_lock is None
    assert int(task.consecutive_failures) == 1

    run = _run_row(conn, successor_run_id)
    assert run["outcome"] == "timed_out" and run["ended_at"] is not None
