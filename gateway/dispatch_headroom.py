"""dispatch_headroom.py — pure policy + fold for the kanban dispatch governor.

Extracted from ``gateway/kanban_watchers._compute_dispatch_headroom`` so the
decision is unit-testable without importing the gateway, and so the policy is
**config-as-code** (``state/fleet/dispatch_headroom.yaml``, deployed by role
59-dispatch-health) instead of hardcoded thresholds.

Why this exists (2026-09-28): the governor held the whole fleet at
``target_workers=0`` because ``cpu_load_1m`` (a load AVERAGE, which on a box
with heavy I/O wait counts uninterruptible tasks) exceeded an absolute
threshold of 8.0. CPU was treated as *critical*, so a busy-but-not-dangerous
box starved every board — and the same load gate blocked the disk reaper that
would have relieved the pressure. CPU is now a SOFT dimension (a breach still
yields ``soft_floor`` workers); only memory/disk/swap can hard-zero dispatch.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Code defaults; every key is overridable by the versioned YAML.
DEFAULT_POLICY: dict = {
    # CPU is load-per-core so a many-core box is not held by a load that is fine
    # for its size, and it is SOFT: a breach throttles but still allows
    # `soft_floor` workers (LLM workers are mostly network-bound; a busy box
    # should not starve the whole fleet).
    "cpu_load_crit_per_core": 3.0,
    "cpu_load_soft_per_core": 1.5,
    # Memory / disk / swap are HARD safety dimensions: breaching them can OOM
    # the box or fill the disk mid-write, so they zero the target.
    "memory_crit_pct": 92.0,
    "memory_soft_pct": 85.0,
    "swap_crit_pct": 90.0,
    "swap_soft_pct": 80.0,
    "disk_crit_pct": 92.0,
    "disk_soft_pct": 88.0,
    "critical_dimensions": ["memory_pct", "disk_used_pct", "swap_used_pct"],
    # Workers still allowed when only SOFT dimensions breach (e.g. CPU).
    "soft_floor": 1,
    # ── PSI + loadavg emergency (Phase 2, 2026-09-30) ──
    # Pressure Stall Information gauges. PSI measures the fraction of time
    # tasks were *stalled* on a resource; unlike loadavg it does not count
    # runnable-but-idle tasks and it is not blind to router queueing. A breach
    # is a SOFT throttle unless the dimension is listed in
    # ``critical_dimensions``. Values are PERCENT.
    "psi_cpu_some_avg60_crit": 40.0,
    "psi_cpu_some_avg60_soft": 20.0,
    "psi_io_full_avg60_crit": 15.0,
    "psi_io_full_avg60_soft": 8.0,
    "psi_mem_some_avg300_crit": 10.0,
    "psi_mem_some_avg300_soft": 5.0,
    # loadavg/core emergency trip (the plan's last-resort signal).
    "loadavg_per_core_crit": 6.0,
    "loadavg_per_core_soft": 3.0,
}


def policy_paths(home: str | None = None, env: dict | None = None) -> list[str]:
    """Ordered candidate paths for the policy file (env override first)."""
    env = os.environ if env is None else env
    base = Path(home) if home else Path.home()
    out = []
    if env.get("HERMES_DISPATCH_HEADROOM_POLICY"):
        out.append(env["HERMES_DISPATCH_HEADROOM_POLICY"])
    out.append(str(base / ".hermes" / "state" / "fleet" / "dispatch_headroom.yaml"))
    out.append(str(base / ".hermes" / "bot" / "dispatch_headroom.yaml"))
    return out


def load_policy(home: str | None = None, env: dict | None = None) -> dict:
    """Load + merge the headroom policy; never raises, falls back to defaults."""
    policy = dict(DEFAULT_POLICY)
    for path in policy_paths(home, env):
        if not path:
            continue
        try:
            if not os.path.exists(path):
                continue
            import yaml
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                policy.update({k: v for k, v in data.items() if v is not None})
                break
        except Exception:
            continue
    return policy


def resource_headroom(
    raw: dict,
    policy: dict,
    cores: int,
    kalman_warn: set | None = None,
    *,
    psi: dict | None = None,
    loadavg: float | None = None,
) -> dict:
    """Map raw resource usage to per-dimension headroom (1.0 / 0.5 / 0.0).

    ``cpu_load`` is compared as load-per-core. A dimension at/above its
    *critical* threshold gives headroom 0.0, at/above *soft* (or predicted by
    the Kalman early-warning) gives 0.5, else 1.0.

    ``psi`` (Phase 2, 2026-09-30): a PSI snapshot from
    :func:`gateway.psi.psi_snapshot`. When present, three extra dimensions are
    emitted — ``cpu_load_psi``, ``io_pressure``, ``memory_pressure`` — each a
    banded 1.0 / 0.5 / 0.0 from the PSI gauges. A *measured* gauge of ``0.0``
    maps to headroom 1.0 (an idle box), and an ABSENT gauge (``None``) emits no
    dimension at all so the fold degrades to its previous shape rather than
    inventing pressure. Without ``psi`` nothing extra is emitted (back-compat).

    ``loadavg``: the 1-minute load average. When given, an extra
    ``loadavg_per_core`` dimension bands on ``loadavg / cores`` against its own
    soft/crit thresholds — the plan's emergency trip, independent of PSI.
    """
    kalman_warn = kalman_warn or set()
    cores = max(1, int(cores or 1))
    bands = {
        "cpu_load": (
            float(policy.get("cpu_load_crit_per_core", 3.0)) * cores,
            float(policy.get("cpu_load_soft_per_core", 1.5)) * cores,
        ),
        "memory_pct": (
            float(policy.get("memory_crit_pct", 92.0)),
            float(policy.get("memory_soft_pct", 85.0)),
        ),
        "swap_used_pct": (
            float(policy.get("swap_crit_pct", 90.0)),
            float(policy.get("swap_soft_pct", 80.0)),
        ),
        "disk_used_pct": (
            float(policy.get("disk_crit_pct", 92.0)),
            float(policy.get("disk_soft_pct", 88.0)),
        ),
    }
    per_dim: dict = {}
    for res, (crit, soft) in bands.items():
        rv = float(raw.get(res, 0.0) or 0.0)
        if rv >= crit:
            per_dim[res] = 0.0
        elif rv >= soft:
            per_dim[res] = 0.5
        elif res in kalman_warn:
            per_dim[res] = 0.5
        else:
            per_dim[res] = 1.0

    def _band(value, crit, soft):
        v = _opt_float(value)
        if v is None:
            return None
        if v >= crit:
            return 0.0
        if v >= soft:
            return 0.5
        return 1.0

    if isinstance(psi, dict) and psi:
        psi_bands = (
            ("cpu_load_psi", psi.get("cpu_some_avg60"),
             policy.get("psi_cpu_some_avg60_crit", 40.0),
             policy.get("psi_cpu_some_avg60_soft", 20.0)),
            ("io_pressure", psi.get("io_full_avg60"),
             policy.get("psi_io_full_avg60_crit", 15.0),
             policy.get("psi_io_full_avg60_soft", 8.0)),
            ("memory_pressure", psi.get("mem_some_avg300"),
             policy.get("psi_mem_some_avg300_crit", 10.0),
             policy.get("psi_mem_some_avg300_soft", 5.0)),
        )
        for name, value, crit, soft in psi_bands:
            band = _band(value, float(crit), float(soft))
            if band is not None:
                per_dim[name] = band

    la = _opt_float(loadavg)
    if la is not None:
        band = _band(
            la / cores,
            _opt_float(policy.get("loadavg_per_core_crit")) or 6.0,
            _opt_float(policy.get("loadavg_per_core_soft")) or 3.0,
        )
        if band is not None:
            per_dim["loadavg_per_core"] = band
    return per_dim


def _opt_float(value):
    """float(value) or None on any failure (PSI gauges are ``None`` when unread)."""
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def board_sort_key(slug: str, now_count: int, priority: int) -> tuple:
    """Pure board-ordering key: urgent first, then higher priority, then slug.

    Before 2026-09-28 the dispatcher ordered boards ``(has-now, slug)`` with no
    priority, so under a small fleet cap the lowest-slug boards were served
    forever and the boards an operator cared about starved. ``priority`` is
    per-board config (``board.json`` ``dispatch_priority``).
    """
    return (0 if now_count else 1, -int(priority or 0), slug or "")


def fold_target(
    per_dim: dict,
    critical_dims,
    static_cap,
    soft_floor: int,
) -> int:
    """Fold per-dimension headroom (0..1) into a worker target count (pure).

    A *critical* dimension at headroom 0 hard-zeroes dispatch. Any **other**
    dimension at 0 — CPU load being the common one — is a SOFT throttle: the
    fleet still gets ``soft_floor`` workers. Otherwise the target scales with
    the minimum headroom, floored at 1.
    """
    cap = static_cap if static_cap and static_cap >= 1 else 3
    if not per_dim:
        return 1
    crit = set(critical_dims or [])
    for dim in crit:
        if per_dim.get(dim, 1.0) <= 0.0:
            return 0
    min_headroom = min(per_dim.values())
    if min_headroom <= 0.0:
        return max(0, int(soft_floor))
    return max(1, int(round(cap * min_headroom)))
