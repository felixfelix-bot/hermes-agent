"""Cross-session HERMES_SESSION_ID leak via the shared bash snapshot.

Regression coverage for the bug where a single long-lived backend serves many
sessions through ONE ``_active_environments["default"]`` LocalEnvironment (the
messaging gateway, TUI, and desktop/web dashboard all collapse the terminal to
"default"). That environment persists a bash *session snapshot* file and
``source``s it before every command. ``export -p`` dumped the FIRST session's
``HERMES_SESSION_ID`` into the snapshot, so every LATER session ``source``d that
stale value and its ``echo $HERMES_SESSION_ID`` reported a FOREIGN session's id
— overriding the correct per-command Popen env injected by
``_inject_session_context_env``.

The fix strips the per-session bridged vars (HERMES_SESSION_* / UI /
CRON_AUTO_DELIVER_) from the snapshot at both dump sites in
``tools/environments/base.py``; they are re-injected fresh on every command.

The same shared-snapshot leak applies to the per-execution *identity* markers:
``HERMES_DELEGATED_CHILD_CONTEXT`` (set into a delegate child's command env by
``agent.delegation_context.scrub_kanban_env()``) and ``HERMES_KANBAN_*``. A
delegate child that runs one terminal command in the shared "default" backend
dumped the marker into the snapshot, after which every later manager/operator
session that sourced it failed the kanban mutation guard
("delegate_task child contexts cannot mutate Kanban tasks or boards"). Those
names are now stripped from the dump as well.
"""

import os
import re
import shlex
import sys

import pytest

from tools.environments.base import (
    _SNAPSHOT_EXCLUDED_ENV_REGEX,
    _SNAPSHOT_EXCLUDED_IDENTITY_NAMES,
    _SNAPSHOT_EXCLUDED_IDENTITY_PREFIXES,
    _export_dump_excluding_session_vars,
)


# ---------------------------------------------------------------------------
# Unit: the exclusion regex matches exactly the bridged vars, nothing else.
# ---------------------------------------------------------------------------

def test_regex_matches_bridged_session_vars():
    rx = re.compile(_SNAPSHOT_EXCLUDED_ENV_REGEX)
    # Every var the gateway bridges must be excluded.
    from gateway.session_context import _VAR_MAP

    for name in _VAR_MAP:
        line = f'declare -x {name}="whatever"'
        assert rx.search(line), f"{name} should be excluded from the snapshot"


def test_export_snippet_shape():
    snippet = _export_dump_excluding_session_vars('"$__hermes_snap_tmp"')
    assert "export -p" in snippet
    # Unset-by-name (not line-grep): multi-line declare values must not leave
    # continuation lines in the snapshot (issue #71296).
    assert "unset" in snippet
    assert "${!HERMES_SESSION_*}" in snippet
    assert "${!HERMES_CRON_AUTO_DELIVER_*}" in snippet
    assert "HERMES_UI_SESSION_ID" in snippet
    # Per-execution identity markers are stripped too (delegation + kanban).
    for name in _SNAPSHOT_EXCLUDED_IDENTITY_NAMES:
        assert name in snippet, f"{name} should be excluded from the snapshot"
    for prefix in _SNAPSHOT_EXCLUDED_IDENTITY_PREFIXES:
        assert f"${{!{prefix}*}}" in snippet, (
            f"{prefix}* should be excluded from the snapshot"
        )
    assert "grep -vE" not in snippet
    assert '"$__hermes_snap_tmp"' in snippet
    # The redirection must be attached to a brace group wrapping the dump,
    # NOT to a pipeline segment: a redirect on a pipeline segment expands the
    # temp-path variable inside that segment's subshell (potentially
    # inconsistently with the parent that expands the follow-up ``mv``
    # operand), silently orphaning the dump and breaking snapshot env
    # persistence entirely.
    assert snippet.lstrip().startswith("{ ")
    assert "|| true; }" in snippet
    assert snippet.rstrip().endswith('> "$__hermes_snap_tmp"')


# ---------------------------------------------------------------------------
# Integration: real LocalEnvironment, two sessions, no cross-contamination.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_shared_snapshot_no_cross_session_leak(tmp_path):
    import threading

    from gateway.session_context import _VAR_MAP, _UNSET, set_session_vars
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    env.init_session()
    try:
        def run_as(sid):
            out = {}

            def worker():
                for v in _VAR_MAP.values():
                    v.set(_UNSET)
                set_session_vars(session_key="k" + sid, session_id=sid, source="desktop")
                out["r"] = env.execute('echo "[$HERMES_SESSION_ID]"')

            t = threading.Thread(target=worker)
            t.start()
            t.join()
            return out["r"].get("output", "")

        out_a = run_as("SIDAAA")
        out_b = run_as("SIDBBB")

        assert "SIDAAA" in out_a, f"session A saw {out_a!r}"
        # The core assertion: B must see its OWN id, not A's leaked via snapshot.
        assert "SIDBBB" in out_b, f"session B saw {out_b!r}"
        assert "SIDAAA" not in out_b, f"session B leaked A's id: {out_b!r}"

        # And the snapshot file must not carry the session id at all.
        snap = env._snapshot_path
        if os.path.exists(snap):
            with open(snap) as f:
                assert "HERMES_SESSION_ID" not in f.read()
    finally:
        env.cleanup()


# ---------------------------------------------------------------------------
# Unit: the identity-marker leak (delegation + kanban) is stripped from the dump.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_delegation_and_kanban_markers_not_persisted(tmp_path):
    """A delegate child's env must not survive into the shared snapshot.

    Reproduces the 2026-10-06 incident: a delegate child that runs a terminal
    command in the shared "default" backend dumps
    ``HERMES_DELEGATED_CHILD_CONTEXT=1`` (and any ``HERMES_KANBAN_*``) into the
    snapshot; every later non-child session that sources it then fails the
    kanban mutation guard.
    """
    import subprocess

    snap = tmp_path / "hermes-snap.sh"
    snippet = _export_dump_excluding_session_vars(shlex.quote(str(snap)))
    script = "\n".join([
        "export HERMES_DELEGATED_CHILD_CONTEXT=1",
        "export HERMES_KANBAN_TASK=t_deadbeef",
        "export HERMES_KANBAN_BOARD=auditable-voting",
        "export HERMES_KANBAN_RUN_ID=run-1",
        "export USER_SHELL_STATE=keepme",
        snippet,
    ])
    subprocess.run(["bash", "-c", script], check=True)

    body = snap.read_text()
    # Identity markers must not persist...
    assert "HERMES_DELEGATED_CHILD_CONTEXT" not in body
    assert "HERMES_KANBAN_TASK" not in body
    assert "HERMES_KANBAN_BOARD" not in body
    assert "HERMES_KANBAN_RUN_ID" not in body
    # ...but genuine user shell state must.
    assert "USER_SHELL_STATE" in body
