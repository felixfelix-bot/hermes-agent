"""RED-first tests: PSI dimensions in the resource headroom fold (Phase 2).

``resource_headroom`` judged CPU from ``cpu_load_1m`` only — a load AVERAGE
that conflates runnable tasks with I/O wait and is blind to the router queueing
that actually caused the 2026-09-30 false-exhaustion 503s. These tests pin the
new contract: PSI gauges and a loadavg emergency trip become their own
dimensions, and the existing signature keeps working.
"""
from gateway.dispatch_headroom import DEFAULT_POLICY, resource_headroom


def _raw(cpu=0.0, mem=0.0, swap=0.0, disk=0.0):
    return {"cpu_load": cpu, "memory_pct": mem,
            "swap_used_pct": swap, "disk_used_pct": disk}


def test_existing_signature_still_works():
    # Back-compat: the pre-existing 4th positional (kalman_warn set) must not
    # change meaning.
    per_dim = resource_headroom(_raw(cpu=1.0), dict(DEFAULT_POLICY), 4, set())
    assert per_dim["cpu_load"] == 1.0
    assert per_dim["memory_pct"] == 1.0


def test_psi_cpu_dimension_present_and_zero_when_high():
    psi = {"cpu_some_avg60": 45.0, "io_full_avg60": 1.0,
           "mem_some_avg300": 0.2, "cpu_some_avg300": 30.0}
    per_dim = resource_headroom(_raw(), dict(DEFAULT_POLICY), 4, set(), psi=psi)
    assert per_dim["cpu_load_psi"] == 0.0


def test_psi_io_full_dimension_zero_when_stalled():
    psi = {"cpu_some_avg60": 1.0, "io_full_avg60": 15.1,
           "mem_some_avg300": 0.0, "cpu_some_avg300": 1.0}
    per_dim = resource_headroom(_raw(), dict(DEFAULT_POLICY), 4, set(), psi=psi)
    assert per_dim["io_pressure"] == 0.0


def test_psi_memory_dimension_zero_when_stalled():
    psi = {"cpu_some_avg60": 1.0, "io_full_avg60": 0.0,
           "mem_some_avg300": 12.0, "cpu_some_avg300": 1.0}
    per_dim = resource_headroom(_raw(), dict(DEFAULT_POLICY), 4, set(), psi=psi)
    assert per_dim["memory_pressure"] == 0.0


def test_quiet_psi_leaves_dimensions_at_one():
    psi = {"cpu_some_avg60": 1.0, "io_full_avg60": 0.5,
           "mem_some_avg300": 0.1, "cpu_some_avg300": 2.0}
    per_dim = resource_headroom(_raw(), dict(DEFAULT_POLICY), 4, set(), psi=psi)
    assert per_dim["cpu_load_psi"] == 1.0
    assert per_dim["io_pressure"] == 1.0
    assert per_dim["memory_pressure"] == 1.0


def test_absent_psi_does_not_invent_dimensions():
    # No PSI (older kernel / sensor gone) => the dims are simply not present,
    # so the fold is unchanged and fails open.
    per_dim = resource_headroom(_raw(), dict(DEFAULT_POLICY), 4, set(), psi={})
    assert "cpu_load_psi" not in per_dim
    assert "io_pressure" not in per_dim


def test_loadavg_emergency_trip_dimension():
    per_dim = resource_headroom(_raw(cpu=1.0), dict(DEFAULT_POLICY), 4, set(),
                                loadavg=30.0)
    assert per_dim["loadavg_per_core"] == 0.0  # 7.5/core >= 6.0


def test_loadavg_below_trip_is_one():
    per_dim = resource_headroom(_raw(cpu=1.0), dict(DEFAULT_POLICY), 4, set(),
                                loadavg=4.0)
    assert per_dim["loadavg_per_core"] == 1.0
