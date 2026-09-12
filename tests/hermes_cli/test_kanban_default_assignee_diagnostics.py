"""Fix D tests — config-source logging for default_assignee + the
"unassigned ready card will be auto-grabbed" diagnostics rule.

The plebeian-adr incident's root cause was a config PRECEDENCE trap: the
operator edited the root ``~/.hermes/config.yaml`` (``worker-tollgate``) while
the live dispatcher ran inside the ``manager`` profile and read
``~/.hermes/profiles/manager/config.yaml`` (``worker-base``). Nothing in the
logs said which file won.

Two guards:
  * ``kanban_config_source()`` — resolves WHICH config file supplied a
    ``kanban.*`` key, plus any other candidate file that declares the same key
    and is therefore shadowed.
  * ``unassigned_default_assignee`` diagnostic — an unassigned ready card on a
    host with ``kanban.default_assignee`` set is going to be grabbed by the
    dispatcher; warn with the remediation.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# kanban_config_source()
# --------------------------------------------------------------------------

def test_config_source_reports_profile_config_and_shadowed_root(monkeypatch, tmp_path):
    """Profile mode: the profile config is authoritative and the root file
    that also declares the key is reported as shadowed (the incident trap)."""
    home = tmp_path / "hermes"
    profile_dir = home / "profiles" / "manager"
    profile_cfg = _write(
        profile_dir / "config.yaml",
        "kanban:\n  default_assignee: worker-y\n",
    )
    root_cfg = _write(
        home / "config.yaml",
        "kanban:\n  default_assignee: worker-tollgate\n",
    )
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))

    from gateway.kanban_watchers import kanban_config_source

    src = kanban_config_source("default_assignee")
    assert Path(src["source"]) == profile_cfg
    assert [Path(p) for p in src["shadowed"]] == [root_cfg]
    assert src["value"] == "worker-y"


def test_config_source_reports_root_config_when_not_in_profile_mode(monkeypatch, tmp_path):
    """Non-profile mode: HERMES_HOME itself holds config.yaml."""
    home = tmp_path / "hermes"
    root_cfg = _write(
        home / "config.yaml",
        "kanban:\n  default_assignee: worker-x\n",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    from gateway.kanban_watchers import kanban_config_source

    src = kanban_config_source("default_assignee")
    assert Path(src["source"]) == root_cfg
    assert src["shadowed"] == []


def test_config_source_reports_none_when_key_absent(monkeypatch, tmp_path):
    """No key anywhere → source is None (nothing to log as a source)."""
    home = tmp_path / "hermes"
    _write(home / "config.yaml", "kanban:\n  failure_limit: 3\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    from gateway.kanban_watchers import kanban_config_source

    src = kanban_config_source("default_assignee")
    assert src["source"] is None
    assert src["value"] is None
    assert src["shadowed"] == []


def test_config_source_ignores_shadowed_file_with_same_value(monkeypatch, tmp_path):
    """A root file that agrees with the winner is not a shadowing hazard."""
    home = tmp_path / "hermes"
    profile_dir = home / "profiles" / "manager"
    profile_cfg = _write(
        profile_dir / "config.yaml",
        "kanban:\n  default_assignee: worker-y\n",
    )
    _write(home / "config.yaml", "kanban:\n  default_assignee: worker-y\n")
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))

    from gateway.kanban_watchers import kanban_config_source

    src = kanban_config_source("default_assignee")
    assert Path(src["source"]) == profile_cfg
    assert src["shadowed"] == []


# --------------------------------------------------------------------------
# diagnostics rule
# --------------------------------------------------------------------------

def _task(**over) -> dict:
    base = {
        "id": "t1",
        "title": "card",
        "status": "ready",
        "assignee": None,
        "claim_lock": None,
        "created_at": 1_700_000_000,
    }
    base.update(over)
    return base


def _cfg(**kanban_over) -> dict:
    from hermes_cli.kanban_diagnostics import config_from_runtime_config

    return config_from_runtime_config({"kanban": dict(kanban_over)})


def _kinds(diags) -> list:
    return [d.kind for d in diags]


def test_rule_warns_on_unassigned_ready_card_with_default_assignee():
    from hermes_cli.kanban_diagnostics import compute_task_diagnostics

    diags = compute_task_diagnostics(
        _task(), [], [], now=1_700_000_000, config=_cfg(default_assignee="worker-x"),
    )
    hits = [d for d in diags if d.kind == "unassigned_default_assignee"]
    assert len(hits) == 1, _kinds(diags)
    diag = hits[0]
    assert diag.severity == "warning"
    # Remediation must be in the operator-facing text.
    assert "--initial-status blocked" in diag.detail
    assert "--hold" in diag.detail
    assert "kanban.default_assignee" in diag.detail
    assert "worker-x" in diag.detail
    assert diag.data["default_assignee"] == "worker-x"
    assert diag.actions, "diagnostic must carry at least one suggested action"


def test_rule_silent_without_default_assignee():
    from hermes_cli.kanban_diagnostics import compute_task_diagnostics

    diags = compute_task_diagnostics(_task(), [], [], now=1_700_000_000, config=_cfg())
    assert "unassigned_default_assignee" not in _kinds(diags)


def test_rule_silent_for_assigned_ready_card():
    from hermes_cli.kanban_diagnostics import compute_task_diagnostics

    diags = compute_task_diagnostics(
        _task(assignee="worker-y"), [], [], now=1_700_000_000,
        config=_cfg(default_assignee="worker-x"),
    )
    assert "unassigned_default_assignee" not in _kinds(diags)


def test_rule_silent_for_claimed_unassigned_card():
    from hermes_cli.kanban_diagnostics import compute_task_diagnostics

    diags = compute_task_diagnostics(
        _task(claim_lock="host:123"), [], [], now=1_700_000_000,
        config=_cfg(default_assignee="worker-x"),
    )
    assert "unassigned_default_assignee" not in _kinds(diags)


@pytest.mark.parametrize("status", ["blocked", "todo", "running", "done", "review"])
def test_rule_silent_for_non_ready_status(status):
    from hermes_cli.kanban_diagnostics import compute_task_diagnostics

    diags = compute_task_diagnostics(
        _task(status=status), [], [], now=1_700_000_000,
        config=_cfg(default_assignee="worker-x"),
    )
    assert "unassigned_default_assignee" not in _kinds(diags)


def test_kind_is_registered():
    from hermes_cli.kanban_diagnostics import DIAGNOSTIC_KINDS

    assert "unassigned_default_assignee" in DIAGNOSTIC_KINDS
