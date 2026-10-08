"""t_ba78cd4e: the board must explain a silent worker death.

A worker whose profile lost its model routing (e.g. a missing
``~/.hermes/profiles/<p>/config.yaml`` → unresolved ``model.default``) dies on
its FIRST API call with ``HTTP 400: No models provided`` and exits rc=0 before
it can call ``kanban_complete``/``kanban_block``. The reaper classified that as
a clean-exit protocol violation, tripped the breaker, and left the card in
``blocked`` with ``block_kind = NULL`` and no reason at all — indistinguishable
from a card parked for no reason (6 manager decision cards sat like that on
2026-09-27).

These tests pin the fix:

* ``_silent_death_reason`` reads the per-task worker log and only returns a
  reason when the log PROVES the run never completed a successful API call
  (one message, zero tool calls, a fatal first-call error);
* the breaker trip for such a run stamps a typed ``block_kind`` and records a
  ``blocked`` event carrying that reason, so the board/diagnostics can show it;
* an ordinary protocol violation (the worker reached the model and merely
  skipped the terminal kanban call) keeps its existing behaviour.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    """Redirect the per-task worker log dir at a temp dir."""
    from hermes_cli import kanban_db as kb

    d = tmp_path / "worker-logs"
    d.mkdir()
    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: d)
    return d


# ---------------------------------------------------------------------------
# Log fixtures — verbatim shape of the real 2026-09-27 manager logs
# ---------------------------------------------------------------------------

FIRST_CALL_FAILURE_BLOCK = """\
Query: work kanban task {tid}
Initializing agent...
────────────────────────────────────────

⚠️  API call failed (attempt 1/3): BadRequestError [HTTP 400]
   🔌 Provider: openrouter  Model: 
   🌐 Endpoint: https://openrouter.ai/api/v1
   📝 Error: HTTP 400: No models provided
   📋 Details: {{'message': 'No models provided', 'code': 400}}
   ⏱️  Elapsed: 2.07s  Context: 2 msgs, ~18,400 tokens
❌ Non-retryable error (HTTP 400): HTTP 400: No models provided
❌ Non-retryable client error (HTTP 400). Aborting.
   🔌 Provider: openrouter  Model: 
   🌐 Endpoint: https://openrouter.ai/api/v1
   💡 This type of error won't be fixed by retrying.
 ─  ⚕ Hermes  ─────────────────────────────────────────────────────────────

     Error: HTTP 400: No models provided

 ────────────────────────────────────────────────────────────────────────

Resume this session with:
  hermes --resume 20260927_005531_4456b4 -p manager

Session:        20260927_005531_4456b4
Duration:       17s
Messages:       1 (1 user, 0 tool calls)
"""


def answered_without_terminal_call_block(tid: str, messages: int = 2,
                                         tool_calls: int = 0) -> str:
    """A run that DID reach the model (the ordinary protocol violation)."""
    return (
        f"Query: work kanban task {tid}\n"
        "Initializing agent...\n"
        "────────────────────────────────────────\n"
        "\n"
        "I have finished the work and everything looks good.\n"
        "\n"
        " ─  ⚕ Hermes  ─────────────────────────────────────────────────\n"
        "\n"
        "Resume this session with:\n"
        "  hermes --resume 20260927_011500_aaaaaa -p worker\n"
        "\n"
        "Session:        20260927_011500_aaaaaa\n"
        f"Duration:       41s\n"
        f"Messages:       {messages} (1 user, {tool_calls} tool calls)\n"
    )


def write_log(log_dir: Path, tid: str, text: str) -> Path:
    path = log_dir / f"{tid}.log"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _silent_death_reason — the classifier
# ---------------------------------------------------------------------------


def test_first_call_failure_is_reported_as_a_silent_death(kanban_home, log_dir):
    from hermes_cli import kanban_db as kb

    tid = "t_silent01"
    write_log(log_dir, tid, FIRST_CALL_FAILURE_BLOCK.format(tid=tid))

    reason = kb._silent_death_reason(tid)

    assert reason, "a run that never got an API reply must be reported"
    assert "HTTP 400: No models provided" in reason
    assert "openrouter" in reason
    assert "0 tool calls" in reason


def test_run_that_reached_the_model_is_not_a_silent_death(kanban_home, log_dir):
    """The ordinary protocol violation must stay untouched."""
    from hermes_cli import kanban_db as kb

    for tid, messages in (("t_answered1", 2), ("t_answered2", 6)):
        write_log(log_dir, tid, answered_without_terminal_call_block(tid, messages))
        assert kb._silent_death_reason(tid) is None


def test_failure_after_a_real_reply_is_not_a_silent_death(kanban_home, log_dir):
    """A fatal error is not enough on its own — the run must never have
    produced an assistant message (otherwise it did complete API calls)."""
    from hermes_cli import kanban_db as kb

    tid = "t_latefail"
    block = FIRST_CALL_FAILURE_BLOCK.format(tid=tid).replace(
        "Messages:       1 (1 user, 0 tool calls)",
        "Messages:       7 (1 user, 3 tool calls)",
    )
    write_log(log_dir, tid, block)

    assert kb._silent_death_reason(tid) is None


def test_missing_worker_log_is_not_a_silent_death(kanban_home, log_dir):
    from hermes_cli import kanban_db as kb

    assert kb._silent_death_reason("t_neverran") is None


@pytest.mark.parametrize("label,text", [
    # Not a worker CLI log at all — no run header to scope the scan to.
    ("no_run_header", "random garbage\nMessages: 1 (1 user, 0 tool calls)\n"),
    # Run header, but the run never printed its closing summary (killed mid-run,
    # or the log was rotated away) — nothing to conclude.
    ("no_summary", "Query: work kanban task t_trunc\nStarting up…\n"),
    # Summary says the model never replied, but no fatal API error is recorded:
    # do not speculate about a cause we cannot see in the log.
    ("no_fatal_error", "Query: work kanban task t_quiet\n"
                       "Session:        20260927_010000_bbbbbb\n"
                       "Messages:       1 (1 user, 0 tool calls)\n"),
])
def test_no_verdict_without_the_required_evidence(kanban_home, log_dir, label, text):
    from hermes_cli import kanban_db as kb

    tid = f"t_{label}"
    write_log(log_dir, tid, text)

    assert kb._silent_death_reason(tid) is None


def test_classifier_scopes_to_the_last_run_in_an_appended_log(kanban_home, log_dir):
    """Worker logs are append-only across attempts — evidence from an EARLIER
    run must neither create nor mask a silent death."""
    from hermes_cli import kanban_db as kb

    # Earlier attempt used the model, latest attempt died on its first call.
    tid_recovered = "t_append1"
    write_log(
        log_dir, tid_recovered,
        answered_without_terminal_call_block(tid_recovered)
        + FIRST_CALL_FAILURE_BLOCK.format(tid=tid_recovered),
    )
    assert kb._silent_death_reason(tid_recovered) is not None

    # Earlier attempt died on its first call, latest attempt reached the model.
    tid_recovered2 = "t_append2"
    write_log(
        log_dir, tid_recovered2,
        FIRST_CALL_FAILURE_BLOCK.format(tid=tid_recovered2)
        + answered_without_terminal_call_block(tid_recovered2, messages=4, tool_calls=2),
    )
    assert kb._silent_death_reason(tid_recovered2) is None


# ---------------------------------------------------------------------------
# End to end: the reaper's breaker trip must explain itself
# ---------------------------------------------------------------------------


def _drive_worker_exit(conn, tid, fake_pid, raw_status):
    """Claim ``tid``, record ``raw_status`` for its dead worker pid, reap once."""
    import hermes_cli.kanban_db as _kb

    host_prefix = _kb._claimer_id().split(":", 1)[0]
    claimed = _kb.claim_task(conn, tid, claimer=f"{host_prefix}:mock")
    assert claimed is not None, "task was not claimable for the next attempt"
    _kb._set_worker_pid(conn, tid, fake_pid)
    _kb._record_worker_exit(fake_pid, raw_status)
    original_alive = _kb._pid_alive
    _kb._pid_alive = lambda p: False
    try:
        return _kb.detect_crashed_workers(conn)
    finally:
        _kb._pid_alive = original_alive


def _drive_to_the_breaker(conn, tid, log_dir, log_text, first_pid=700100):
    """Run enough clean-exit protocol violations to trip the breaker."""
    import hermes_cli.kanban_db as kb

    write_log(log_dir, tid, log_text)
    for i in range(kb._PROTOCOL_VIOLATION_FAILURE_LIMIT):
        _drive_worker_exit(conn, tid, first_pid + i, 0)


def test_breaker_trip_names_the_block_and_records_the_reason(kanban_home, log_dir):
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="manager card", assignee="manager")
        _drive_to_the_breaker(
            conn, tid, log_dir, FIRST_CALL_FAILURE_BLOCK.format(tid=tid),
        )

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == kb._SILENT_DEATH_BLOCK_KIND, (
            "an auto-blocked silent death must carry a typed block kind"
        )
        assert "HTTP 400: No models provided" in (task.last_failure_error or "")

        events = kb.list_events(conn, tid)
        blocked = [e for e in events if e.kind == "blocked"]
        assert len(blocked) == 1, "the board needs a blocked event to explain it"
        assert "HTTP 400: No models provided" in blocked[0].payload["reason"]
        assert blocked[0].payload.get("auto") is True
        # The violation itself is still recorded, with the diagnosis attached.
        violations = [e for e in events if e.kind == "protocol_violation"]
        assert violations and violations[-1].payload.get("silent_death") is True
    finally:
        conn.close()


def test_ordinary_protocol_violation_keeps_its_old_shape(kanban_home, log_dir):
    """No regression: a worker that used the model and only skipped the
    terminal call still blocks exactly as before (no typed kind, no reason)."""
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="chatty worker", assignee="worker")
        _drive_to_the_breaker(
            conn, tid, log_dir, answered_without_terminal_call_block(tid),
        )

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind is None
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "blocked"]
        assert "protocol violation" in (task.last_failure_error or "")
    finally:
        conn.close()
