"""Unit tests for the D-128 §8.2 gateway dispatch_gate fail-open-on-timeout.

2026-10-03 made ``llm`` (the /v1/dispatch_gate verdict) a HARD, critical
dimension: an undeliverable pool yields ``target_workers=0``. That is correct
for a *negative* verdict, but it makes the router's availability part of the
dispatch loop's critical path: when the router hangs (not answers "no" — just
never answers within the 5 s urlopen timeout), ``per_dim["llm"]`` was silently
left at 1.0 with only a log line, and when the router answers
``{"can_dispatch": false}`` to a request it never really evaluated, every
board on the node starves — the exact "CW dispatcher stall" D-128 §8.2
was opened for.

Policy (D-128 §8.2 / D-123): the gate must FAIL OPEN on timeout — dispatch
continues under a **conservative cap** (``llm_gate_timeout_cap`` workers) and
an **alert** is surfaced (reason + state file for the fleet watchdogs), never
a total spawn block. The alert channel itself must never wedge dispatch.
"""
from __future__ import annotations

import json

import gateway.kanban_watchers as kw
from gateway.dispatch_headroom import (
    DEFAULT_POLICY,
    fold_target,
    llm_gate_degraded_headroom,
)

GATE_URL = "http://localhost:9099/v1/dispatch_gate"


def _probe(requests, responses):
    """Fake urlopen factory: pops canned answers, default = timeout."""
    import urllib.error

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        requests.append((str(getattr(req, "full_url", req)), timeout))
        if responses:
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return _Resp(r)
        import socket
        raise socket.timeout("timed out")  # the timeout shape: no answer at all

    return _fake_urlopen


def _healthy_sensors(monkeypatch, tmp_path):
    """Force every resource dimension to 1.0 so `llm` is the only input.

    Writes the DEPLOYED policy shape (llm critical, 2026-10-03) into the
    sandboxed home so the fold matches production rather than the code
    defaults (which keep llm soft).
    """
    db = tmp_path / "zai_usage.db"
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE resource_metrics (ts REAL, cpu_load_1m REAL, "
        "memory_used_percent REAL, swap_used_percent REAL, disk_used_percent REAL)"
    )
    conn.execute(
        "INSERT INTO resource_metrics VALUES (1, 0.1, 10.0, 1.0, 10.0)")
    conn.commit()
    conn.close()
    pol = tmp_path / ".hermes" / "state" / "fleet" / "dispatch_headroom.yaml"
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.write_text(
        "critical_dimensions:\n"
        "  - cpu_load\n  - memory_pct\n  - disk_used_pct\n  - swap_used_pct\n"
        "  - llm\nsoft_floor: 1\n",
        encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    # Kalman + reviewer probes are irrelevant here; force them off the path.
    monkeypatch.setenv("HERMES_REVIEWER_AWARE_DISPATCH", "0")
    return db


# ── fail-open policy (pure) ─────────────────────────────────────────────────

def test_default_policy_has_a_conservative_timeout_cap():
    # Fail-open must be *conservative*: 1 worker, not the full static cap.
    assert DEFAULT_POLICY.get("llm_gate_timeout_cap") == 1


def test_llm_gate_degraded_headroom_allows_conservative_dispatch():
    # The core 8.2 contract: a timed-out gate yields the conservative cap,
    # never 0 (block) and never the un-throttled cap.
    per_dim = {"cpu_load": 1.0, "memory_pct": 1.0, "swap_used_pct": 1.0,
               "disk_used_pct": 1.0, "llm": 1.0}
    target = fold_target(
        llm_gate_degraded_headroom(per_dim, DEFAULT_POLICY, static_cap=8),
        ["memory_pct", "disk_used_pct", "swap_used_pct", "llm"],
        static_cap=8, soft_floor=1,
    )
    assert target == DEFAULT_POLICY["llm_gate_timeout_cap"]
    assert target > 0, "a timed-out gate must never hard-block all spawns"


def test_llm_gate_degraded_headroom_respects_a_resource_hold():
    # Fail-open applies to the *gate* dimension only: a real memory hold
    # still zeroes dispatch (we do not dispatch into a dying box).
    per_dim = {"cpu_load": 1.0, "memory_pct": 0.0, "swap_used_pct": 1.0,
               "disk_used_pct": 1.0, "llm": 1.0}
    degraded = llm_gate_degraded_headroom(per_dim, DEFAULT_POLICY)
    assert fold_target(degraded, ["memory_pct", "disk_used_pct", "swap_used_pct", "llm"], 8, 1) == 0


def test_llm_gate_degraded_headroom_configurable():
    policy = dict(DEFAULT_POLICY)
    policy["llm_gate_timeout_cap"] = 2
    per_dim = {"llm": 1.0, "memory_pct": 1.0, "swap_used_pct": 1.0,
               "disk_used_pct": 1.0, "cpu_load": 1.0}
    assert fold_target(
        llm_gate_degraded_headroom(per_dim, policy, static_cap=8), ["llm"], 8, 1,
    ) == 2


# ── _compute_dispatch_headroom wiring (gateway) ──────────────────────────────

def test_gate_timeout_fails_open_with_alert(monkeypatch, tmp_path):
    # The router never answers: dispatch must continue at the conservative
    # cap, and the degradation must be visible (reason + state file).
    _healthy_sensors(monkeypatch, tmp_path)
    requests: list = []
    monkeypatch.setattr(
        "urllib.request.urlopen", _probe(requests, []))
    out = kw._compute_dispatch_headroom(static_cap=4)
    assert out["can_dispatch"] is True
    assert out["target_workers"] == DEFAULT_POLICY["llm_gate_timeout_cap"]
    assert "dispatch_gate" in out["reason"] and "timeout" in out["reason"]
    # The alert surface for fleet watchdogs (operator_alert consumers).
    state = json.loads((tmp_path / ".hermes" / "bot" /
                        "dispatch_gate_degraded.json").read_text())
    assert state.get("mode") == "timeout-fail-open"
    assert state.get("last_reason", "").startswith("dispatch_gate timeout")
    assert requests and requests[0][1] == 5  # still a 5 s bounded probe


def test_negative_verdict_still_holds(monkeypatch, tmp_path):
    # An ANSWERED "no" is a real verdict, not a timeout: hold (target 0).
    _healthy_sensors(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _probe([], [{"can_dispatch": False, "reason": "all lanes exhausted"}]))
    out = kw._compute_dispatch_headroom(static_cap=4)
    assert out["can_dispatch"] is False
    assert out["target_workers"] == 0
    # No degradation marker: the gate worked; it just said no.
    assert not (tmp_path / ".hermes" / "bot" /
                "dispatch_gate_degraded.json").exists()


def test_positive_verdict_unchanged(monkeypatch, tmp_path):
    # Healthy path: an answered "yes" dispatches at the full static cap.
    _healthy_sensors(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _probe([], [{"can_dispatch": True, "reason": "ok"}]))
    out = kw._compute_dispatch_headroom(static_cap=4)
    assert out["target_workers"] == 4
    assert not (tmp_path / ".hermes" / "bot" /
                "dispatch_gate_degraded.json").exists()


def test_connection_error_also_fails_open(monkeypatch, tmp_path):
    # A dead router port (ConnectionRefused) is the same class as a timeout:
    # unavailable sensor -> conservative cap, never a total block.
    import urllib.error
    _healthy_sensors(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _probe([], [urllib.error.URLError("connection refused")]))
    out = kw._compute_dispatch_headroom(static_cap=4)
    assert out["can_dispatch"] is True
    assert out["target_workers"] == DEFAULT_POLICY["llm_gate_timeout_cap"]


def test_alert_write_failure_never_breaks_dispatch(monkeypatch, tmp_path):
    # The alert is best-effort: an unwritable state file must not raise or
    # change the decision. Make the marker path a DIRECTORY so the real
    # writer's write_text raises IsADirectoryError past any mock.
    _healthy_sensors(monkeypatch, tmp_path)
    marker = tmp_path / ".hermes" / "bot" / "dispatch_gate_degraded.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.mkdir()  # write_text on a dir -> IsADirectoryError (OSError)
    monkeypatch.setattr("urllib.request.urlopen", _probe([], []))
    out = kw._compute_dispatch_headroom(static_cap=4)  # must not raise
    assert out["can_dispatch"] is True
    assert out["target_workers"] == DEFAULT_POLICY["llm_gate_timeout_cap"]
