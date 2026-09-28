"""Tests for the dispatch headroom policy + fold (2026-09-28).

The governor held the fleet at target_workers=0 because a *load average* above
an absolute threshold was treated as a critical CPU breach. These tests pin the
corrected contract: CPU is a SOFT dimension (a breach still yields the soft
floor), only memory/disk/swap hard-zero dispatch, and the policy is
config-as-code.
"""
from __future__ import annotations

import textwrap

from gateway.dispatch_headroom import (
    DEFAULT_POLICY,
    fold_target,
    load_policy,
    resource_headroom,
)


# ── fold ────────────────────────────────────────────────────────────────────

def test_cpu_breach_is_soft_and_keeps_the_floor():
    # The exact 2026-09-28 freeze shape: cpu_load headroom 0, nothing critical.
    per_dim = {
        "cpu_load": 0.0,
        "memory_pct": 0.5,
        "swap_used_pct": 1.0,
        "disk_used_pct": 0.5,
        "llm": 1.0,
    }
    target = fold_target(per_dim, DEFAULT_POLICY["critical_dimensions"], 3, 1)
    assert target == 1, "a CPU-only breach must still let one worker run"


def test_critical_dimension_zeroes_dispatch():
    per_dim = {"cpu_load": 1.0, "memory_pct": 1.0, "disk_used_pct": 0.0}
    assert fold_target(per_dim, ["memory_pct", "disk_used_pct"], 3, 1) == 0


def test_all_healthy_scales_with_min_headroom():
    per_dim = {"cpu_load": 1.0, "memory_pct": 0.5, "disk_used_pct": 1.0}
    # min headroom 0.5, cap 3 -> round(1.5) = 2
    assert fold_target(per_dim, ["memory_pct", "disk_used_pct"], 3, 1) == 2


def test_soft_floor_respects_config():
    per_dim = {"cpu_load": 0.0, "memory_pct": 1.0}
    assert fold_target(per_dim, ["memory_pct"], 3, 2) == 2
    assert fold_target(per_dim, ["memory_pct"], 3, 0) == 0


def test_no_sensors_fails_open():
    assert fold_target({}, ["memory_pct"], 3, 1) == 1


# ── resource_headroom ───────────────────────────────────────────────────────

def test_cpu_load_is_judged_per_core():
    raw = {"cpu_load": 21.0, "memory_pct": 50.0, "swap_used_pct": 10.0,
           "disk_used_pct": 50.0}
    # crit 3.0/core -> 12 on 4 cores (breach), 24 on 8 cores (soft).
    assert resource_headroom(raw, DEFAULT_POLICY, 4)["cpu_load"] == 0.0
    assert resource_headroom(raw, DEFAULT_POLICY, 8)["cpu_load"] == 0.5


def test_disk_and_memory_bands():
    raw = {"cpu_load": 1.0, "memory_pct": 80.0, "swap_used_pct": 10.0,
           "disk_used_pct": 92.0}
    hr = resource_headroom(raw, DEFAULT_POLICY, 4)
    assert hr["disk_used_pct"] == 0.0      # >= crit 92
    assert hr["memory_pct"] == 1.0         # < soft 85


def test_kalman_warning_softens_a_dimension():
    raw = {"cpu_load": 1.0, "memory_pct": 50.0, "swap_used_pct": 10.0,
           "disk_used_pct": 50.0}
    hr = resource_headroom(raw, DEFAULT_POLICY, 4, {"memory_pct"})
    assert hr["memory_pct"] == 0.5


# ── policy is config-as-code ────────────────────────────────────────────────

def test_policy_defaults_when_file_absent(tmp_path):
    pol = load_policy(home=str(tmp_path), env={})
    assert pol["critical_dimensions"] == DEFAULT_POLICY["critical_dimensions"]


def test_policy_file_overrides_defaults(tmp_path):
    d = tmp_path / ".hermes" / "state" / "fleet"
    d.mkdir(parents=True)
    (d / "dispatch_headroom.yaml").write_text(textwrap.dedent("""
        soft_floor: 2
        critical_dimensions: [disk_used_pct]
        cpu_load_crit_per_core: 9.0
    """))
    pol = load_policy(home=str(tmp_path), env={})
    assert pol["soft_floor"] == 2
    assert pol["critical_dimensions"] == ["disk_used_pct"]
    assert pol["cpu_load_crit_per_core"] == 9.0
    # unspecified keys keep their defaults
    assert pol["memory_crit_pct"] == DEFAULT_POLICY["memory_crit_pct"]


def test_env_path_override(tmp_path):
    p = tmp_path / "custom.yaml"
    p.write_text("soft_floor: 3\n")
    pol = load_policy(home=str(tmp_path), env={"HERMES_DISPATCH_HEADROOM_POLICY": str(p)})
    assert pol["soft_floor"] == 3
