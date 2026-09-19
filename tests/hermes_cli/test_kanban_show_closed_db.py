"""Regression: ``hermes kanban show <id>`` aborted on a closed DB connection.

``_cmd_show`` opened the board inside ``kb.connect_closing()`` and then, in the
text (non-JSON) branch, called ``kb.task_graph_context(conn, task.id)`` for the
diagnostics section *after* that context manager had exited and closed the
connection. Result: the CLI printed the task header and then died with
``sqlite3.ProgrammingError: Cannot operate on a closed database.`` for every
task on every board, so agents lost the comments/events/runs evidence they
inspect cards for.

These tests drive the real CLI entrypoint (``kanban_command``), not the raw
kernel, so any future leaking of ``conn`` past the ``with`` block fails here.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _show_args(task_id: str, *, json_out: bool = False) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    argv = ["kanban", "show", task_id]
    if json_out:
        argv.append("--json")
    return parser.parse_args(argv)


def _seed_task() -> tuple[str, str]:
    """Create a task with a comment, an event and a finished run."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent task", assignee="alice")
        task = kb.create_task(conn, title="child task", assignee="bob")
        kb.link_tasks(conn, parent_id=parent, child_id=task)
        kb.add_comment(conn, task_id=task, author="alice", body="needs evidence")
        kb._append_event(conn, task, "commented", {"a": 1})
        # Parent must be done before the child is claimable (structural
        # invariant in claim_task); completing it also promotes the child.
        assert kb.complete_task(conn, parent, summary="parent done")
        assert kb.claim_task(conn, task, claimer="bob") is not None
    return task, parent


def test_cmd_show_text_returns_comments_events_and_runs(kanban_home, capsys):
    """Text ``show`` must exit 0 with the card's evidence rendered."""
    task_id, parent_id = _seed_task()

    rc = kc.kanban_command(_show_args(task_id))

    out = capsys.readouterr().out
    assert rc == 0
    assert f"Task {task_id}: child task" in out
    # Evidence the closed-connection crash used to swallow.
    assert "Comments (1):" in out
    assert "needs evidence" in out
    assert "Events" in out
    assert "Runs" in out
    assert f"parents:   {parent_id}" in out


def test_cmd_show_json_still_returns_payload(kanban_home, capsys):
    """The JSON branch must keep working (it never touched ``conn`` late)."""
    task_id, _ = _seed_task()

    rc = kc.kanban_command(_show_args(task_id, json_out=True))

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["task"]["id"] == task_id
    assert payload["comments"][0]["body"] == "needs evidence"
    kinds = [e["kind"] for e in payload["events"]]
    assert kinds[0] == "created"
    assert "commented" in kinds
    assert "claimed" in kinds
    assert payload["runs"][0]["profile"] == "bob"


def test_run_slash_show_reports_no_error(kanban_home):
    """Gateway/CLI ``/kanban show`` must not surface an error string."""
    task_id, _ = _seed_task()

    out = kc.run_slash(f"show {task_id}")

    assert "error:" not in out
    assert "closed database" not in out
    assert "needs evidence" in out
