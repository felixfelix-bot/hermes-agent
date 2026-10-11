"""Unit tests for the kanban dispatch headroom governor (soft-CPU policy).

These lock the 2026-09-28 fix: CPU load is a SOFT, per-core dimension, so a
busy-but-not-dangerous box still gets `soft_floor` workers; only memory, disk
and swap may hard-zero dispatch. They also lock the config-as-code merge.
"""
from gateway.dispatch_headroom import (
    DEFAULT_POLICY,
    board_sort_key,
    fold_target,
    load_policy,
    resource_headroom,
)


# ── board scheduling priority (2026-09-28) ──────────────────────────────────

def test_priority_beats_slug_within_an_urgency():
    # The starvation shape: tollgate-* (later slug) waited behind plebeian
    # forever under a cap of 2. A higher priority must sort first.
    tollgate = board_sort_key("tollgate-module-basic-go", 1, 100)
    plebeian = board_sort_key("plebeian-pr-reviews", 1, 0)
    assert sorted([plebeian, tollgate])[0] == tollgate


def test_urgent_board_still_beats_priority():
    urgent_low = board_sort_key("z-board", 1, 0)
    soon_high = board_sort_key("a-board", 0, 100)
    assert sorted([soon_high, urgent_low])[0] == urgent_low


def test_zero_priority_falls_back_to_slug():
    assert board_sort_key("a", 1, 0) < board_sort_key("b", 1, 0)


def test_none_priority_is_zero():
    assert board_sort_key("x", 1, None) == board_sort_key("x", 1, 0)


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


# ── price deferral vs capacity hold (2026-10-11) ────────────────────────────
# The router's dispatch gate answers can_dispatch=False in TWO different
# situations: the pool is UNDELIVERABLE (capacity -> hold the fleet), or its
# cheapest DELIVERABLE lane is merely above this urgency's willingness-to-pay
# ceiling (spend -> alert, keep soft_floor workers). Conflating them parked the
# whole fleet on a $0.61/M market against the $0.50/M `soon` ceiling, with no
# hysteresis, so it flapped with every market sample (2026-10-11 incident).

def _gate(**over):
    base = {
        "can_dispatch": True,
        "deliverable": True,
        "price_ceiling_per_m": 0.5,
        "effective_price_per_m": 0.4,
        "reason": "ok",
    }
    base.update(over)
    return base


def test_deliverable_lane_above_the_ceiling_is_a_spend_deferral():
    from gateway.dispatch_headroom import is_price_deferral
    assert is_price_deferral(
        _gate(can_dispatch=False, effective_price_per_m=0.61)
    )


def test_undeliverable_pool_is_a_capacity_hold_not_a_deferral():
    from gateway.dispatch_headroom import is_price_deferral
    assert not is_price_deferral(_gate(can_dispatch=False, deliverable=False))


def test_answered_no_without_price_fields_is_not_a_deferral():
    from gateway.dispatch_headroom import is_price_deferral
    assert not is_price_deferral({"can_dispatch": False, "deliverable": True})


def test_a_dispatchable_verdict_is_never_a_deferral():
    from gateway.dispatch_headroom import is_price_deferral
    assert not is_price_deferral(_gate())


def test_a_spend_deferral_must_not_hard_zero_the_fleet():
    from gateway.dispatch_headroom import (
        DEFAULT_POLICY, fold_target, price_defer_headroom,
    )
    policy = dict(DEFAULT_POLICY)
    policy["critical_dimensions"] = [
        "memory_pct", "disk_used_pct", "swap_used_pct", "llm",
    ]
    healthy = {"cpu_load": 1.0, "memory_pct": 1.0, "swap_used_pct": 1.0,
               "disk_used_pct": 1.0, "llm": 0.0}
    # as the policy stands today: a critical llm at 0.0 zeroes the whole fleet
    assert fold_target(healthy, policy["critical_dimensions"], 3, 1) == 0
    # after the deferral view: soft_floor workers keep every board moving
    per_dim = price_defer_headroom(healthy, policy, 3)
    assert per_dim["llm"] == 1.0, "spend is not capacity: never a hard hold"
    assert fold_target(per_dim, policy["critical_dimensions"], 3, 1) == 1


def test_a_spend_deferral_lands_on_soft_floor_for_any_cap():
    from gateway.dispatch_headroom import (
        DEFAULT_POLICY, fold_target, price_defer_headroom,
    )
    policy = dict(DEFAULT_POLICY)
    policy["critical_dimensions"] = [
        "memory_pct", "disk_used_pct", "swap_used_pct", "llm",
    ]
    for cap in (1, 2, 3, 4, 6, 8, 12):
        per_dim = price_defer_headroom({"llm": 0.0, "cpu_load": 1.0}, policy, cap)
        target = fold_target(per_dim, policy["critical_dimensions"], cap, 1)
        assert target == int(policy["soft_floor"]), (
            f"cap={cap}: a spend deferral must land exactly on soft_floor, "
            f"got {target} (per_dim={per_dim})"
        )
