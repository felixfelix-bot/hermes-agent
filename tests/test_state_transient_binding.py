"""Regression: retry the transient sqlite3 binding SystemError in _execute_write.

2026-10-08 CobradorWave: under write contention a C-level sqlite3 call returned
NULL without setting an exception ("<TrackedConnection> returned NULL without
setting an exception"), a SystemError — not a sqlite3.Error — so it escaped the
retry net and aborted the operator's turn as session_persistence_failed. It is
transient; retry after forcing a reconnect.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hermes_state as hs  # noqa: E402

_MSG = ("<hermes_cli.sqlite_safe_read.TrackedConnection object at 0x1> "
        "returned NULL without setting an exception")


def test_classify_maps_transient_binding_error_to_locked():
    assert hs.classify_persistence_error(_MSG) == "locked"


def test_execute_write_retries_transient_binding_error():
    db = hs.SessionDB(Path(tempfile.mkdtemp()) / "state.db")
    db._sleep_before_write_retry = lambda *a, **k: True  # no jitter delay
    calls = {"n": 0}

    def fn(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SystemError(_MSG)
        conn.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        return "ok"

    try:
        assert db._execute_write(fn) == "ok"
        assert calls["n"] == 2
    finally:
        db.close()
