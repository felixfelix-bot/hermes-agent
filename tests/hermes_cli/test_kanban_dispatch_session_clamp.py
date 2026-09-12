"""Regression tests for the dispatcher per-profile SESSION clamp (Fix C).

The dispatcher's per-profile cap (``kanban.max_in_progress_per_profile``,
#21582) counted only the dispatcher's own ceiling. A target profile's own
``max_concurrent_sessions`` — the gateway's active-session limit, which is
what makes a worker print ``max_concurrent_sessions reached`` and exit — was
invisible to it. In the plebeian-adr incident that amplifier turned one
misconfigured ``kanban.default_assignee`` into four crash-looping workers:
three cards were spawned at once into a 1-slot profile.

These tests pin the clamp:

    effective per-assignee ceiling =
        min(kanban.max_in_progress_per_profile,
            target profile's max_concurrent_sessions)

Semantics that must hold:

* ``max_concurrent_sessions: null`` (what the ``manager`` profile ships)
  means UNLIMITED — never 0.
* A profile whose ``config.yaml`` has no such key is uncapped.
* ``0`` disables the cap (same as the gateway's
  ``resolve_max_concurrent_sessions``).
* When the dispatcher cap is the lower of the two, tasks keep landing in the
  pre-existing ``skipped_per_profile_capped`` bucket (unchanged #21582
  behavior); only the target-profile-derived clamp uses the new
  ``skipped_per_profile_session_capped`` bucket.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest


def _write_profile(home: str, name: str, config_body: str = "") -> Path:
    pdir = Path(home) / "profiles" / name
    pdir.mkdir(parents=True, exist_ok=True)
    if config_body:
        (pdir / "config.yaml").write_text(config_body, encoding="utf-8")
    return pdir


@pytest.fixture()
def isolated_kanban_home_with_session_caps(monkeypatch):
    """Fresh HERMES_HOME with profiles that carry max_concurrent_sessions."""
    test_home = tempfile.mkdtemp(prefix="kanban_session_clamp_test_")
    for prof in ("default", "worker-x", "worker-y", "worker-uncapped",
                 "worker-null", "worker-zero"):
        _write_profile(test_home, prof)
    # The star of the incident: a one-slot profile.
    _write_profile(test_home, "worker-y", "max_concurrent_sessions: 1\n")
    # null == unlimited (this is what profiles/manager/config.yaml ships).
    _write_profile(test_home, "worker-null", "max_concurrent_sessions: null\n")
    # 0 == disabled == unlimited (gateway semantics).
    _write_profile(test_home, "worker-zero", "max_concurrent_sessions: 0\n")
    # worker-uncapped keeps an unrelated config with no such key.
    _write_profile(test_home, "worker-uncapped", "model:\n  default: stub\n")

    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db


def _fake_spawn(*args, **kwargs):
    """Stand-in for the real worker spawn — returns a fake PID."""
    return 12345


def _seed(kb, assignee: str, count: int, *, unassigned: bool = False):
    ids = []
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(count):
            ids.append(
                kb.create_task(
                    conn,
                    title=f"{assignee or 'unassigned'}-{i}",
                    assignee=None if unassigned else assignee,
                )
            )
    return ids


def test_profile_session_cap_limits_spawns_per_tick(isolated_kanban_home_with_session_caps):
    """G1: worker-y (max_concurrent_sessions: 1) + 3 ready tasks + no
    dispatcher-side per-profile cap and default_assignee unset → exactly one
    spawn on the tick, the other two in the new
    skipped_per_profile_session_capped bucket."""
    kb = isolated_kanban_home_with_session_caps
    _seed(kb, "worker-y", 3)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res.spawned) == 1, res.spawned
    assert res.spawned[0][1] == "worker-y"
    assert len(res.skipped_per_profile_session_capped) == 2
    assert [c[1] for c in res.skipped_per_profile_session_capped] == ["worker-y"] * 2
    # The dispatcher-side bucket stays empty: the binding constraint was the
    # target profile's own session cap, not kanban.max_in_progress_per_profile.
    assert res.skipped_per_profile_capped == []


def test_session_capped_task_dispatched_on_next_tick(isolated_kanban_home_with_session_caps):
    """The clamp defers, it does not drop: once the running worker finishes,
    the next tick spawns the next queued card."""
    kb = isolated_kanban_home_with_session_caps
    _seed(kb, "worker-y", 3)
    with kb.connect_closing() as conn:
        res1 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res1.spawned) == 1
    first_id = res1.spawned[0][0]

    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done', claim_lock = NULL WHERE id = ?",
                (first_id,),
            )

    with kb.connect_closing() as conn:
        res2 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res2.spawned) == 1
    assert res2.spawned[0][0] != first_id
    assert len(res2.skipped_per_profile_session_capped) == 1


def test_null_session_cap_is_unlimited(isolated_kanban_home_with_session_caps):
    """``max_concurrent_sessions: null`` must mean UNCAPPED, not 0."""
    kb = isolated_kanban_home_with_session_caps
    _seed(kb, "worker-null", 3)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res.spawned) == 3
    assert res.skipped_per_profile_session_capped == []
    assert res.skipped_per_profile_capped == []


def test_missing_session_cap_key_is_uncapped(isolated_kanban_home_with_session_caps):
    """A profile config without the key is uncapped."""
    kb = isolated_kanban_home_with_session_caps
    _seed(kb, "worker-uncapped", 3)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res.spawned) == 3
    assert res.skipped_per_profile_session_capped == []


def test_zero_session_cap_is_not_a_hard_block(isolated_kanban_home_with_session_caps):
    """``0`` disables the cap (gateway semantics) — it must never wedge the
    dispatcher by capping a profile at zero workers."""
    kb = isolated_kanban_home_with_session_caps
    _seed(kb, "worker-zero", 3)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res.spawned) == 3
    assert res.skipped_per_profile_session_capped == []


def test_dispatcher_cap_is_the_effective_ceiling_when_lower(isolated_kanban_home_with_session_caps):
    """min() semantics: dispatcher cap 2 + profile cap 5 → 2 spawns, and the
    deferred tasks keep using the ORIGINAL per-profile bucket (#21582)."""
    kb = isolated_kanban_home_with_session_caps
    _seed(kb, "worker-x", 4)
    # worker-x has no session cap; give it one above the dispatcher cap.
    cfg = Path(os.environ["HERMES_HOME"]) / "profiles" / "worker-x" / "config.yaml"
    cfg.write_text("max_concurrent_sessions: 5\n", encoding="utf-8")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            max_in_progress_per_profile=2,
        )
    assert len(res.spawned) == 2
    assert res.skipped_per_profile_session_capped == []
    assert len(res.skipped_per_profile_capped) == 2


def test_profile_cap_wins_over_higher_dispatcher_cap(isolated_kanban_home_with_session_caps):
    """min() semantics the other way: dispatcher cap 5 + profile cap 1 → 1
    spawn and the deferred tasks land in the session-capped bucket."""
    kb = isolated_kanban_home_with_session_caps
    _seed(kb, "worker-y", 3)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            max_in_progress_per_profile=5,
        )
    assert len(res.spawned) == 1
    assert len(res.skipped_per_profile_session_capped) == 2
    assert res.skipped_per_profile_capped == []


def test_default_assignee_still_applies_and_clamp_holds(isolated_kanban_home_with_session_caps):
    """Incident repro: kanban.default_assignee points at the 1-slot profile
    and three unassigned ready cards exist. #27145 semantics are untouched
    (the cards still get assigned), but the clamp keeps the spawn count at
    one per tick instead of firing four workers into a one-slot profile."""
    kb = isolated_kanban_home_with_session_caps
    ids = _seed(kb, "", 3, unassigned=True)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_assignee="worker-y",
        )
    assert len(res.spawned) == 1
    assert res.auto_assigned_default, "default_assignee auto-assign must survive Fix C"
    assert len(res.skipped_per_profile_session_capped) == 2
    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (ids[0],)
        ).fetchone()
    assert rows["assignee"] == "worker-y"


def test_default_profile_cap_comes_from_the_root_config(isolated_kanban_home_with_session_caps):
    """``get_profile_dir("default")`` IS the hermes root, so the ``default``
    profile's cap lives in <HERMES_HOME>/config.yaml — it must bind too."""
    kb = isolated_kanban_home_with_session_caps
    home = Path(os.environ["HERMES_HOME"])
    (home / "config.yaml").write_text(
        "max_concurrent_sessions: 1\n", encoding="utf-8"
    )
    _seed(kb, "default", 3)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res.spawned) == 1, res.spawned
    assert len(res.skipped_per_profile_session_capped) == 2
    assert res.skipped_per_profile_capped == []
