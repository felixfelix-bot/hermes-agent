"""Tests for board-level LLM-dispatch opt-out (board.json no_llm_dispatch).

2026-09-17: the PCB autoroute board looped 46x because deterministic EDA
tooling was being driven by LLM workers. A board opts out with
``"no_llm_dispatch": true``.
"""
from __future__ import annotations

import json

from gateway.kanban_watchers import _board_no_llm_dispatch


def _board(home, slug, **cfg):
    d = home / "kanban" / "boards" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "board.json").write_text(json.dumps({"slug": slug, **cfg}))


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
