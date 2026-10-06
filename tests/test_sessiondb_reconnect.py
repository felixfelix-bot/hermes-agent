"""Regression: a lost/closed SessionDB connection must never surface as
``'NoneType' object has no attribute 'execute'`` (2026-10-06).

A transient failure left ``_conn is None``, or ``close()`` nulled it during
shutdown while a turn was still appending. The next ``append_message`` then
raised AttributeError and aborted the operator's turn. ``_execute_write`` now
reopens a lost connection once and raises a clear OperationalError when reopen
is impossible.
"""
from __future__ import annotations

import sqlite3

import pytest

from hermes_state import SessionDB


def test_append_reopens_lost_connection(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="s1", source="cli", model="m")
        db.append_message("s1", role="user", content="before")
        # Simulate a lost connection (transient init failure / teardown).
        db._conn = None
        # Must reopen transparently instead of raising NoneType.
        db.append_message("s1", role="user", content="after")
        assert db._conn is not None
    finally:
        db.close()


def test_append_after_close_raises_clear_error(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="s1", source="cli", model="m")
    db.close()
    assert db._closed is True
    with pytest.raises(sqlite3.OperationalError) as ei:
        db.append_message("s1", role="user", content="x")
    msg = str(ei.value)
    assert "closed" in msg or "unavailable" in msg, msg
    assert "NoneType" not in msg
