"""Unit tests for the kanban dispatch headroom governor (soft-CPU policy).

These lock the 2026-09-28 fix: CPU load is a SOFT, per-core dimension, so a
busy-but-not-dangerous box still gets `soft_floor` workers; only memory, disk
and swap may hard-zero dispatch. They also lock the config-as-code merge.
"""
from gateway.dispatch_headroom import DEFAULT_POLICY, fold_target, load_policy, resource_headroom


def _raw(cpu=0.0, mem=0.0, swap=0.0, disk=0.0):
    return {
        "cpu_load": cpu,
        "memory_pct": mem,
        "swap_used_pct": swap,
        "disk_used_pct": disk,
    }


def test_cpu_is_soft_so_a_busy_box_still_dispatches():
    # 4 cores, load 30 -> cpu headroom 0, but CPU is not a critical dimension.
    per_dim = resource_headroom(_raw(cpu=30.0), dict(DEFAULT_POLICY), 4, set())
    assert per_dim["cpu_load"] == 0.0
    target = fold_target(
        per_dim, DEFAULT_POLICY["critical_dimensions"], static_cap=3,
        soft_floor=DEFAULT_POLICY["soft_floor"],
    )
    assert target == DEFAULT_POLICY["soft_floor"], "a CPU-only breach must not starve the fleet"
    assert target > 0


def test_cpu_thresholds_are_per_core():
    policy = dict(DEFAULT_POLICY)
    # 2 cores: soft at load 3.0 (1.5/core), critical at 6.0 (3.0/core).
    assert resource_headroom(_raw(cpu=2.9), policy, 2)["cpu_load"] == 1.0
    assert resource_headroom(_raw(cpu=3.0), policy, 2)["cpu_load"] == 0.5
    assert resource_headroom(_raw(cpu=6.0), policy, 2)["cpu_load"] == 0.0
    # The same absolute load on a bigger box is fine.
    assert resource_headroom(_raw(cpu=6.0), policy, 8)["cpu_load"] == 1.0


def test_memory_is_critical_and_hard_zeroes():
    per_dim = resource_headroom(_raw(mem=95.0), dict(DEFAULT_POLICY), 4)
    assert per_dim["memory_pct"] == 0.0
    assert fold_target(per_dim, DEFAULT_POLICY["critical_dimensions"], 3, 3) == 0


def test_memory_soft_band_throttles_but_does_not_zero():
    per_dim = resource_headroom(_raw(mem=88.0), dict(DEFAULT_POLICY), 4)  # soft 85..92
    assert per_dim["memory_pct"] == 0.5
    assert fold_target(per_dim, DEFAULT_POLICY["critical_dimensions"], 4, 1) == 2


def test_healthy_box_scales_to_static_cap():
    per_dim = resource_headroom(_raw(cpu=1.0, mem=40.0, swap=10.0, disk=50.0),
                                dict(DEFAULT_POLICY), 4)
    assert set(per_dim.values()) == {1.0}
    assert fold_target(per_dim, DEFAULT_POLICY["critical_dimensions"], 8, 1) == 8


def test_no_sensors_fails_open():
    assert fold_target({}, ["memory_pct"], 3, 1) == 1


def test_load_policy_merges_yaml_over_defaults(tmp_path, monkeypatch):
    override = tmp_path / "dispatch_headroom.yaml"
    override.write_text("soft_floor: 5\nmemory_crit_pct: 99.0\n")
    monkeypatch.setenv("HERMES_DISPATCH_HEADROOM_POLICY", str(override))
    policy = load_policy()
    assert policy["soft_floor"] == 5
    assert policy["memory_crit_pct"] == 99.0
    # Unspecified keys keep their code defaults.
    assert policy["cpu_load_crit_per_core"] == DEFAULT_POLICY["cpu_load_crit_per_core"]


def test_load_policy_falls_back_to_defaults_without_a_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_DISPATCH_HEADROOM_POLICY", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("HOME", str(tmp_path))
    policy = load_policy(home=str(tmp_path))
    assert policy == dict(DEFAULT_POLICY)
