"""psi.py — Linux Pressure Stall Information (PSI) reader.

Cheap, always-on host-pressure signal for the kanban dispatch gate. PSI
(``/proc/pressure/{cpu,io,memory}``) reports the fraction of wall-clock time
that tasks were *stalled* on a resource — unlike ``loadavg``, which conflates
runnable tasks with tasks blocked on I/O and is blind to proxy queueing.

Why this exists (2026-09-30): under load 15-30 on 4 cores, ``/proc/pressure
cpu some avg300`` read 60 % and an 8-token completion took 14-25 s instead of
~2 s. The live router's 90 s candidate budget then expired and returned the
*same* ``503 all providers exhausted`` string as genuine exhaustion, which made
the fleet conclude cross-family review was structurally impossible. The
dispatch gate needs a real stall signal, and this is the cheapest one that
reads nothing but a procfs file.

Pure stdlib, never raises: a missing/unreadable file yields ``{}`` and the
caller fails open.
"""
from __future__ import annotations

from pathlib import Path

#: procfs root holding the three PSI files (overridable for tests).
DEFAULT_ROOT = "/proc/pressure"

#: The resource files we read.
RESOURCES = ("cpu", "io", "memory")


def parse_psi(text: str) -> dict:
    """Parse one PSI file body into ``{"some": {...}, "full": {...}}``.

    Each line is ``<kind> avg10=0.00 avg60=1.23 avg300=4.56 total=12345``.
    Unknown/blank lines are skipped; malformed values are dropped rather than
    raised (a torn read must not break dispatch).
    """
    out: dict[str, dict[str, float]] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]
        vals: dict[str, float] = {}
        for tok in parts[1:]:
            if "=" not in tok:
                continue
            key, _, raw = tok.partition("=")
            try:
                vals[key] = float(raw)
            except (TypeError, ValueError):
                continue
        out[kind] = vals
    return out


def read_psi(root: str = DEFAULT_ROOT) -> dict:
    """Read all three PSI files. Anything unreadable becomes ``{}``."""
    res: dict[str, dict] = {}
    for name in RESOURCES:
        try:
            res[name] = parse_psi((Path(root) / name).read_text())
        except Exception:
            res[name] = {}
    return res


def psi_snapshot(root: str = DEFAULT_ROOT) -> dict:
    """Flatten PSI into the scalar gauges the dispatch gate consumes.

    Every gauge is ``None`` when unavailable, so a caller can distinguish
    "measured 0.0 %" (an idle box) from "sensor absent / not available" and
    fail open in the latter case.
    """
    raw = read_psi(root)

    def _gauge(resource: str, kind: str, field: str):
        try:
            return float(raw.get(resource, {}).get(kind, {}).get(field))
        except (TypeError, ValueError):
            return None

    return {
        # cpu "some": at least one task stalled on CPU. avg60 = the always-on
        # check; avg300 = the slow-moving trend (the metric that read 60 % in
        # the 2026-09-30 incident).
        "cpu_some_avg60": _gauge("cpu", "some", "avg60"),
        "cpu_some_avg300": _gauge("cpu", "some", "avg300"),
        # io "full": *every* task stalled on I/O — the real stall signal
        # (read 15.1 % in the incident).
        "io_full_avg60": _gauge("io", "full", "avg60"),
        # memory "some": at least one task stalled reclaiming memory.
        "mem_some_avg300": _gauge("memory", "some", "avg300"),
        "available": any(raw.get(r) for r in RESOURCES),
        "root": str(root),
    }
