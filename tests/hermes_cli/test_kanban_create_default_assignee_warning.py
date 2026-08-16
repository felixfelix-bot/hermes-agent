"""Tests for create-time dispatch transparency (Fix A).

Incident (plebeian-adr, 2026-08-16): four manager-gate cards were created
without ``--assignee`` intending "unassigned = won't dispatch". The
dispatcher's ``kanban.default_assignee`` fallback (#27145) silently
re-assigned them ~53s later and spawned 4 workers into a 1-slot profile.
See ~/plans/hermes-fix-handover/HANDOVER-kanban-assignee-default.md and
docs/adr/proposals/adr-kanban-dispatch-transparency.md.

These tests pin the create-time contracts:

1. **NULL stays NULL + idle note.** With ``kanban.default_assignee``
   unset, ``hermes kanban create`` without ``--assignee`` leaves the DB
   row NULL and prints a short stderr note that the card will NOT be
   dispatched. A dispatch tick puts it in ``skipped_unassigned``.

2. **Warning when default_assignee is set.** With the config set, the
   same create prints a stderr warning naming the fallback profile and
   recommending ``--hold`` for gate-style cards. ``--json`` output
   stays silent on stderr.

3. **``--hold`` alias.** ``--hold`` parks the card sticky-blocked at
   creation with blocked-event reason 'held' (vs 'initial-status' for
   the explicit long form); a plain ``--initial-status running`` must
   still create a normal ready card; contradicting flags are a usage
   error (exit 2).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re

import pytest
import yaml

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so config + board DB reads are deterministic."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(__import__("pathlib").Path, "home", lambda: tmp_path)
    return home


def _write_profile_config(home, kanban_cfg: dict) -> None:
    """Write the profile-scoped config.yaml the gateway dispatcher reads."""
    (home / "config.yaml").write_text(yaml.safe_dump({"kanban": kanban_cfg}))


def _run_create(argv_extra: list[str], title: str = "gate card"):
    """Run ``hermes kanban create <title> <extra>`` in-process.

    Uses the same parser + ``kanban_command`` dispatch the CLI and
    ``/kanban`` slash path share, but captures stdout and stderr
    SEPARATELY so the stderr-only contract is assertable. argparse
    usage errors surface as SystemExit(2) — capture them like the
    shell would.
    """
    wrap = argparse.ArgumentParser(prog="test-wrap", add_help=False)
    wrap.exit_on_error = False  # type: ignore[attr-defined]
    top = wrap.add_subparsers(dest="_top")
    parser = kc.build_parser(top)
    parser.exit_on_error = False  # type: ignore[attr-defined]
    out, err = io.StringIO(), io.StringIO()
    rc = 2
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            args = parser.parse_args(["create", title] + list(argv_extra))
            rc = kc.kanban_command(args)
        except SystemExit as exc:
            rc = int(exc.code or 0)
    return rc, out.getvalue(), err.getvalue()


def _fake_spawn(*args, **kwargs):
    """Stand-in for the real worker spawn — returns a fake PID."""
    return 12345


def _task_id_from_stdout(stdout: str) -> str:
    m = re.search(r"Created (t_[0-9a-f]+)", stdout)
    assert m, f"could not parse task id from stdout: {stdout!r}"
    return m.group(1)


# ---------------------------------------------------------------------------
# Test 1 — NULL stays NULL + idle note (default_assignee unset)
# ---------------------------------------------------------------------------


def test_unassigned_create_null_stays_null_with_idle_note(kanban_home):
    rc, out, err = _run_create([])
    assert rc == 0, f"create failed: {err}"
    assert "will NOT be dispatched" in err, (
        "unassigned create with default_assignee unset must print the "
        f"idle note on stderr, got stderr={err!r}"
    )
    tid = _task_id_from_stdout(out)

    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
    assert row["assignee"] is None, "CLI must leave assignee NULL"

    # A dispatch tick with no default_assignee skips the card entirely.
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert res.skipped_unassigned == [tid]
    assert not res.auto_assigned_default
    assert not res.spawned

    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assigned = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? AND kind = 'assigned'",
            (tid,),
        ).fetchone()
    assert row["assignee"] is None
    assert assigned["n"] == 0


# ---------------------------------------------------------------------------
# Test 2 — warning names the profile when default_assignee is set
# ---------------------------------------------------------------------------


def test_unassigned_create_warns_when_default_assignee_set(
    kanban_home, all_assignees_spawnable
):
    _write_profile_config(kanban_home, {"default_assignee": "worker-x"})
    rc, out, err = _run_create([])
    assert rc == 0, f"create failed: {err}"
    assert "worker-x" in err, (
        f"warning must name the fallback profile, got stderr={err!r}"
    )
    assert "--hold" in err, (
        f"warning must recommend --hold, got stderr={err!r}"
    )
    tid = _task_id_from_stdout(out)

    # And the warned-about behavior really happens on the next tick:
    # auto-assigned from kanban.default_assignee + spawned exactly once.
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_assignee="worker-x",
        )
    assert res.auto_assigned_default == [tid]
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == tid
    assert res.spawned[0][1] == "worker-x"

    with kb.connect_closing() as conn:
        evs = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'assigned'",
            (tid,),
        ).fetchall()
    assert len(evs) == 1
    payload = json.loads(evs[0]["payload"])
    assert payload["assignee"] == "worker-x"
    assert payload["source"] == "kanban.default_assignee"


def test_json_create_is_silent_on_stderr(kanban_home):
    """--json keeps stdout strictly machine-parseable: no warning at all."""
    _write_profile_config(kanban_home, {"default_assignee": "worker-x"})
    rc, out, err = _run_create(["--json"])
    assert rc == 0, f"create failed: {err}"
    assert err == "", f"--json must not emit stderr warnings, got {err!r}"
    data = json.loads(out)
    assert data["assignee"] is None
    assert data["status"] == "ready"


def test_assigned_create_gets_no_default_assignee_warning(kanban_home):
    """The warning targets UNASSIGNED cards only — an explicit assignee
    is never re-routed by default_assignee, so no warning."""
    _write_profile_config(kanban_home, {"default_assignee": "worker-x"})
    rc, out, err = _run_create(["--assignee", "worker-x"])
    assert rc == 0, f"create failed: {err}"
    assert "default_assignee" not in err, f"unexpected warning: {err!r}"


def test_triage_create_gets_no_default_assignee_warning(kanban_home):
    """Triage cards sit outside the dispatch loop until promoted — the
    warning is only for status='ready' cards."""
    _write_profile_config(kanban_home, {"default_assignee": "worker-x"})
    rc, out, err = _run_create(["--triage"])
    assert rc == 0, f"create failed: {err}"
    assert "default_assignee" not in err, f"unexpected warning: {err!r}"


def test_blocked_create_gets_no_default_assignee_warning(kanban_home):
    """Blocked cards are outside the dispatch loop (sticky block) — the
    warning is only for status='ready' cards."""
    _write_profile_config(kanban_home, {"default_assignee": "worker-x"})
    rc, out, err = _run_create(["--initial-status", "blocked"])
    assert rc == 0, f"create failed: {err}"
    assert "default_assignee" not in err, f"unexpected warning: {err!r}"


# ---------------------------------------------------------------------------
# Test 3 — --hold alias (Fix B)
# ---------------------------------------------------------------------------


def _blocked_events(tid: str) -> list[dict]:
    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked'",
            (tid,),
        ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def test_hold_creates_sticky_blocked_with_held_reason(kanban_home):
    _write_profile_config(kanban_home, {"default_assignee": "worker-x"})
    rc, out, err = _run_create(["--hold"])
    assert rc == 0, f"create failed: {err}"
    tid = _task_id_from_stdout(out)

    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status, assignee FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
    assert row["status"] == "blocked"
    assert row["assignee"] is None
    evs = _blocked_events(tid)
    assert len(evs) == 1
    assert evs[0]["reason"] == "held"

    # A held card is invisible to the dispatcher even with default_assignee
    # set — the whole point of the alias.
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_assignee="worker-x",
        )
    assert res.auto_assigned_default == []
    assert not res.spawned
    assert res.skipped_unassigned == []


def test_explicit_initial_status_blocked_reason_is_initial_status(kanban_home):
    """The long form keeps its generic 'initial-status' reason — only
    --hold records 'held'."""
    rc, out, err = _run_create(["--initial-status", "blocked"])
    assert rc == 0, f"create failed: {err}"
    tid = _task_id_from_stdout(out)
    evs = _blocked_events(tid)
    assert len(evs) == 1
    assert evs[0]["reason"] == "initial-status"


def test_hold_conflicts_with_explicit_initial_status(kanban_home):
    rc, out, err = _run_create(["--hold", "--initial-status", "running"])
    assert rc == 2, f"contradictory flags must exit 2, got {rc}; err={err!r}"
    assert "--hold" in err


def test_hold_plus_initial_status_blocked_is_accepted(kanban_home):
    """--hold --initial-status blocked is redundant but not contradictory."""
    rc, out, err = _run_create(["--hold", "--initial-status", "blocked"])
    assert rc == 0, f"create failed: {err}"
    tid = _task_id_from_stdout(out)
    evs = _blocked_events(tid)
    assert len(evs) == 1
    assert evs[0]["reason"] == "held"


def test_explicit_initial_status_running_still_makes_ready(kanban_home):
    """Regression: argparse default is now None so 'not given' and an
    explicit 'running' can be told apart; the explicit form must still
    create a normal ready card."""
    rc, out, err = _run_create(["--initial-status", "running"])
    assert rc == 0, f"create failed: {err}"
    tid = _task_id_from_stdout(out)
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
    assert row["status"] == "ready"
