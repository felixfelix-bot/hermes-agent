"""Tests for board-level LLM-dispatch opt-out (board.json no_llm_dispatch).

2026-09-17: the PCB autoroute board looped 46x because deterministic EDA
tooling was being driven by LLM workers. A board opts out with
``"no_llm_dispatch": true``.
"""
from __future__ import annotations

import json

from hermes_cli import kanban_db as kb

from gateway.kanban_watchers import _board_has_spawnable_work, _board_no_llm_dispatch


def _board(home, slug, **cfg):
    d = home / "kanban" / "boards" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "board.json").write_text(json.dumps({"slug": slug, **cfg}))


class _FakeConn:
    def __init__(self, *, ready: bool, review: bool = False):
        self._ready = ready
        self._review = review
        self.closed = False

    def close(self):
        self.closed = True


def test_spawnable_work_skips_opted_out_board(tmp_path, monkeypatch):
    """An opted-out board with a full queue is 'correctly idle', not 'stuck'.

    The stuck-probe must short-circuit BEFORE opening its DB, otherwise the
    ready pile on a deterministic-tooling board keeps the false warning
    firing while zero cards are actually dispatchable (2026-10-15).
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _board(tmp_path, "pcb", no_llm_dispatch=True)

    def _boom(*_a, **_k):
        raise AssertionError("opted-out board must not open its DB")

    monkeypatch.setattr(kb, "connect", _boom)
    assert _board_has_spawnable_work("pcb", review_probe=False) is False


def test_spawnable_work_consults_db_when_not_opted_out(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _board(tmp_path, "market")
    conn = _FakeConn(ready=True)
    monkeypatch.setattr(kb, "connect", lambda **_k: conn)
    monkeypatch.setattr(kb, "has_spawnable_ready", lambda _c: True)
    monkeypatch.setattr(kb, "has_spawnable_review", lambda _c: False)

    assert _board_has_spawnable_work("market", review_probe=False) is True
    assert conn.closed is True


def test_spawnable_work_probes_review_only_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _board(tmp_path, "market")
    conn = _FakeConn(ready=False, review=True)
    monkeypatch.setattr(kb, "connect", lambda **_k: conn)
    monkeypatch.setattr(kb, "has_spawnable_ready", lambda _c: False)
    monkeypatch.setattr(kb, "has_spawnable_review", lambda _c: True)

    assert _board_has_spawnable_work("market", review_probe=False) is False
    assert _board_has_spawnable_work("market", review_probe=True) is True


def test_spawnable_work_fail_closed_on_db_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _board(tmp_path, "market")

    def _boom(**_k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(kb, "connect", _boom)
    assert _board_has_spawnable_work("market", review_probe=False) is False


def test_true_when_flag_set(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _board(tmp_path, "balloon", no_llm_dispatch=True)
    assert _board_no_llm_dispatch("balloon") is True


def test_false_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _board(tmp_path, "market")
    assert _board_no_llm_dispatch("market") is False


def test_fail_open_on_missing_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert _board_no_llm_dispatch("ghost") is False
