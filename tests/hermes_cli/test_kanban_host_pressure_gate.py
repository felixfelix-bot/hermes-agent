"""RED-first tests: host-pressure gate wired into the dispatcher (Phase 2).

The gate itself is unit-tested in ``tests/test_dispatch_load_gate.py`` /
``tests/test_dispatch_headroom_psi.py``. THIS file pins the *wiring* contract
the 2026-09-30 acceptance criteria demand:

* (b) a worker spawn is REFUSED with a VISIBLE reason while the host is
  loaded, and the card is left resumable (still ``ready``, no failure counted)
  so it picks up unattended once the pressure clears;
* (a) a REVIEW spawn still happens at the same signal — the gate must never
  block the critical path (a review is critical path, a fresh worker is not).

The signal is passed in as ``host_pressure_gate`` (the dispatcher computes it
once per tick from ``gateway.dispatch_probe.load_gate``), so these tests need
no live router and no real PSI read.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Hermetic: an ambient cross-node claim command (set in a dispatched
    # worker's env) would fire on spawn and needs a board slug we don't pass.
    monkeypatch.delenv("KANBAN_SPAWN_CLAIM_CMD", raising=False)
    kb.init_db()
    return home


def _spawn_ok(*args, **kwargs):
    return 4242


def _loaded(reason: str = "PSI cpu some avg60 45.0% >= 20.0%") -> dict:
    return {"ok": False, "reason": reason, "signals": {}, "probe_ran": True}


def _host_events(conn, tid):
    return [
        r[0] for r in conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'host_pressure_deferred' "
            "ORDER BY id", (tid,),
        )
    ]


def test_loaded_host_defers_ready_spawn_with_visible_reason(kanban_home, monkeypatch):
    from hermes_cli import profiles as profmod
    monkeypatch.setattr(profmod, "profile_exists", lambda _n: True)
    monkeypatch.setenv("HERMES_HOST_PRESSURE_EVENT_THROTTLE_S", "0")

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="do work", assignee="builder")
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn_ok,
            host_pressure_gate=_loaded(),
        )

    # Refused, with the reason carried on the structured bucket...
    assert res.spawned == []
    assert [t for t, _ in res.skipped_host_pressure] == [tid]
    assert "PSI cpu some avg60" in res.skipped_host_pressure[0][1]

    with kb.connect() as conn:
        # ...and the card is left RESUMABLE (ready), not failed/blocked, so the
        # tick after the pressure clears picks it up unattended.
        row = conn.execute(
            "SELECT status, consecutive_failures FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row["status"] == "ready"
        assert int(row["consecutive_failures"] or 0) == 0
        # Visible in the timeline.
        evs = _host_events(conn, tid)
        assert len(evs) == 1
        assert "PSI cpu some avg60" in evs[0]


def test_loaded_host_still_spawns_review(kanban_home, monkeypatch):
    """(a) the gate must NOT block the critical path: a review still runs."""
    from hermes_cli import profiles as profmod
    from hermes_cli import config as cfgmod
    monkeypatch.setattr(profmod, "profile_exists", lambda _n: True)
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    with kb.connect() as conn:
        ready_id = kb.create_task(conn, title="new worker", assignee="builder")
        rev_id = kb.create_task(conn, title="review me", assignee="reviewer")
        claim = kb.claim_task(conn, rev_id)
        assert claim is not None
        assert kb.request_review(
            conn, rev_id, summary="ready",
            expected_run_id=claim.current_run_id,
        )
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn_ok,
            host_pressure_gate=_loaded(),
        )

    spawned_ids = [s[0] for s in res.spawned]
    assert rev_id in spawned_ids, "review lane must still spawn under load"
    assert ready_id not in spawned_ids, "ready lane must be deferred under load"
    assert [t for t, _ in res.skipped_host_pressure] == [ready_id]


def test_gate_ok_does_not_defer(kanban_home, monkeypatch):
    from hermes_cli import profiles as profmod
    monkeypatch.setattr(profmod, "profile_exists", lambda _n: True)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="do work", assignee="builder")
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn_ok,
            host_pressure_gate={"ok": True, "reason": "ok"},
        )
    assert [s[0] for s in res.spawned] == [tid]
    assert res.skipped_host_pressure == []


def test_absent_gate_does_not_defer(kanban_home, monkeypatch):
    """Default (None) keeps historical behaviour — no gate, no refusal."""
    from hermes_cli import profiles as profmod
    monkeypatch.setattr(profmod, "profile_exists", lambda _n: True)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="do work", assignee="builder")
        res = kb.dispatch_once(conn, spawn_fn=_spawn_ok)
    assert [s[0] for s in res.spawned] == [tid]
    assert res.skipped_host_pressure == []


def test_dry_run_ignores_gate(kanban_home, monkeypatch):
    """A dry-run preview must never be suppressed by the gate."""
    from hermes_cli import profiles as profmod
    monkeypatch.setattr(profmod, "profile_exists", lambda _n: True)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="do work", assignee="builder")
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn_ok, dry_run=True,
            host_pressure_gate=_loaded(),
        )
    assert tid in [s[0] for s in res.spawned]
    assert res.skipped_host_pressure == []


def test_deferral_event_is_throttled(kanban_home, monkeypatch):
    """A deep queue must not flood task_events: one event per window."""
    from hermes_cli import profiles as profmod
    monkeypatch.setattr(profmod, "profile_exists", lambda _n: True)
    monkeypatch.setenv("HERMES_HOST_PRESSURE_EVENT_THROTTLE_S", "600")

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="do work", assignee="builder")
        kb.dispatch_once(conn, spawn_fn=_spawn_ok, host_pressure_gate=_loaded())
        kb.dispatch_once(conn, spawn_fn=_spawn_ok, host_pressure_gate=_loaded())
        n = len(_host_events(conn, tid))
    assert n == 1, f"expected one throttled event, got {n}"


def test_pressure_clear_resumes_ready_spawn(kanban_home, monkeypatch):
    """(b) ...and the task resumes UNATTENDED once pressure clears."""
    from hermes_cli import profiles as profmod
    monkeypatch.setattr(profmod, "profile_exists", lambda _n: True)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="do work", assignee="builder")
        blocked = kb.dispatch_once(
            conn, spawn_fn=_spawn_ok, host_pressure_gate=_loaded())
        assert blocked.spawned == []
        cleared = kb.dispatch_once(
            conn, spawn_fn=_spawn_ok, host_pressure_gate={"ok": True})
    assert [s[0] for s in cleared.spawned] == [tid]
