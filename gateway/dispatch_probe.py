"""dispatch_probe.py — end-to-end ground-truth probe + PSI gate for dispatch.

The PSI signal (``gateway/psi.py``) is cheap and always-on, but it is a *proxy*:
the SLO that actually matters is "a tiny completion finishes quickly". A box can
show a modest PSI number while the live router is queueing, or show a scary
``loadavg`` while completions are fine.

This module runs a bounded 5-token completion against the live router and judges
it against a deadline (default 3000 ms). It also folds the PSI gauges into a
single gate decision. Placement is the DISPATCHER, never the router — a
router-side refusal fabricates the provider-exhausted 503 that already poisoned
lane selection and price learning.

Two entry points, both pure where it matters:

* :func:`evaluate_load_gate` — pure decision from (psi, probe, loadavg, cores,
  policy). Unit-testable with no I/O.
* :func:`load_gate` — the one that runs the probe (and optional PSI read) with a
  hard wall-clock cap, and returns the decision dict.

Fail-open by contract: if the probe cannot run (router down, malformed reply,
no ``urllib``), the gate reports ``ok=True`` with ``probe_ran=False``. A broken
sensor must never wedge dispatch; the operator gets the visible reason instead.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable

#: Code defaults; every key is overridable by the versioned policy.
DEFAULT_POLICY: dict[str, Any] = {
    # PSI cpu "some" avg60 (%) at/above which the box counts as loaded.
    "psi_cpu_some_avg60": 20.0,
    # PSI io "full" avg60 (%) at/above which I/O stall counts as loaded.
    "psi_io_full_avg60": 10.0,
    # PSI memory "some" avg300 (%) at/above which memory stall counts as loaded.
    "psi_mem_some_avg300": 5.0,
    # PSI cpu "some" avg300 (%) — the slow trend that read 60 % in the incident.
    "psi_cpu_some_avg300": 40.0,
    # The probe's SLO: a 5-token completion must finish within this many ms.
    "probe_deadline_ms": 3000,
    "probe_max_tokens": 5,
    # Grace added to the read timeout ON TOP of the deadline. The read timeout
    # must EXCEED the deadline, otherwise a response that takes longer than the
    # deadline is cut off at the deadline and the probe can never *measure* the
    # real latency of the exact failure it exists to detect (2026-09-30: an
    # 8-token completion took 14-25 s; with timeout == deadline every such
    # response was an indistinguishable read timeout). Worst case the dispatcher
    # blocks for deadline + grace.
    "probe_grace_ms": 3000,
    # Models the probe may dial, tried in order until one gives a verdict. A
    # model no lane declares yields an INSTANT "no lane declares this model" 503;
    # catalogue drift (glm-4.5-flash ceased to be declared, 2026-10-01) must not
    # cost the probe its ground truth, so a small ordered fallback list is
    # cheaper than a single hardcoded id.
    "probe_models": ("deepseek/deepseek-flash", "kimi-k3",
                     "deepseek/deepseek-v4-pro"),
    # Emergency trip on loadavg/core, as the plan directs, independent of PSI.
    "loadavg_per_core_max": 6.0,
    # ── Host ATTRIBUTION for a probe miss (2026-10-01) ──────────────────────
    # A slow probe is NOT proof of host pressure. Measured live 2026-10-01: the
    # box was quiet (PSI cpu some avg60=2.4 %, loadavg 0.9) yet a 5-token
    # completion took ~9 s because the router was walking a HANGING lane
    # (`candidate budget 90s exhausted at ollama_cloud_2`). Gating on that would
    # stall dispatch forever on a healthy host — a NEW false signal, the very
    # class of bug this task exists to remove. So a probe miss only defers when
    # the host is corroborated as the cause: either a host gauge is at/above
    # `probe_corroboration_factor` × its gate threshold, or the router itself
    # attributed the failure to local backpressure (a 429 / ``local_backpressure``
    # body — see ``router_error_body.local_backpressure_body``). A 2xx that
    # simply took too long also needs the same corroboration for the same reason.
    "probe_require_host_corroboration": True,
    #: Fraction of each gate threshold that counts as a corroborating host
    #: signal (0.5 = half the gate threshold: mild pressure still corroborates).
    "probe_corroboration_factor": 0.5,
    # Gate master switch + probe master switch.
    "load_gate_enabled": True,
    "probe_enabled": True,
}

#: Kill switches (read once per call so a live flip takes effect next tick).
KILL_ENV = "HERMES_LOAD_GATE"
PROBE_ENV = "HERMES_LOAD_PROBE"
DEFAULT_ROUTER = "http://localhost:9099"


def load_policy(policy: dict | None = None) -> dict:
    """Merge caller/policy-file values over :data:`DEFAULT_POLICY`."""
    merged = dict(DEFAULT_POLICY)
    if isinstance(policy, dict):
        for k, v in policy.items():
            if v is not None:
                merged[k] = v
    return merged


def _num(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def probe_completion(
    router_url: str | None = None,
    deadline_ms: int = 3000,
    max_tokens: int = 5,
    model: str = "glm-4.5-flash",
    _opener: Callable | None = None,
    grace_ms: int | None = None,
) -> dict:
    """Run a bounded completion; report whether it met the deadline.

    Returns ``{"ran": bool, "ok": bool|None, "latency_ms": int|None,
    "error": str, "model": str}``.

    Verdict semantics (this is the subtle part the gate depends on):

    * a 2xx completion → ``ok = latency_ms <= deadline_ms`` — the SLO verdict;
    * an HTTP error (503/429/…) → ``ok = None``, ``ran = True``: the router
      ANSWERED but could not serve, which is NOT the probe's question and must
      NOT block dispatch. A model no lane declares answers an INSTANT 503
      (26 ms observed 2026-10-01); scoring that as "missed the SLO" would wedge
      dispatch on a perfectly quiet box, and a 503 whose latency is genuinely
      over the deadline needs no special case because a healthy lane would have
      answered the tiny request long before then;
    * a read timeout → ``ok = False``: the one HTTP failure that *is* evidence
      of the condition we gate on;
    * a transport error (connection refused) → ``ok = None``, ``ran = False``:
      router down is not host load.

    The HTTP read timeout is ``deadline + grace`` (NOT the deadline itself), so
    the probe can actually *measure* a response slower than the deadline instead
    of cutting it off at it. Worst case the dispatcher blocks for that sum.
    """
    base = (router_url or os.environ.get("HERMES_ROUTER") or DEFAULT_ROUTER).rstrip("/")
    url = f"{base}/v1/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": int(max_tokens),
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    _grace = 3000 if grace_ms is None else int(grace_ms)
    timeout = max(0.1, (float(deadline_ms) + max(0, _grace)) / 1000.0)
    started = time.monotonic()
    opener = _opener or urllib.request.urlopen
    try:
        with opener(req, timeout=timeout) as resp:
            raw = resp.read()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        # Any well-formed 2xx completion counts as "the box can serve".
        try:
            body = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return {"ran": True, "ok": None, "latency_ms": elapsed_ms,
                    "error": "probe reply was not JSON", "model": model,
                    "local_backpressure": False}
        if not isinstance(body, dict) or "error" in body:
            return {"ran": True, "ok": None, "latency_ms": elapsed_ms,
                    "error": "probe reply carried an error", "model": model,
                    "local_backpressure": False}
        return {"ran": True, "ok": elapsed_ms <= int(deadline_ms),
                "latency_ms": elapsed_ms, "error": "", "model": model,
                "local_backpressure": False}
    except urllib.error.HTTPError as exc:
        # The router answered with an error status. Read the body: a 429, or a
        # body carrying ``local_backpressure: True``, is the router itself
        # attributing the failure to LOCAL host pressure (see
        # ``router_error_body.local_backpressure_body``) — that IS a verdict and
        # the gate may act on it. Any other error status (e.g. the instant "no
        # lane declares this model" 503) is NOT host evidence, so fail OPEN.
        elapsed_ms = int((time.monotonic() - started) * 1000)
        _local = exc.code == 429
        try:
            _b = json.loads((exc.read() or b"").decode("utf-8", "replace"))
            if isinstance(_b, dict) and _b.get("local_backpressure"):
                _local = True
        except Exception:
            pass
        return {"ran": True, "ok": (False if _local else None),
                "latency_ms": elapsed_ms, "error": f"HTTP {exc.code}",
                "model": model, "local_backpressure": _local}
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        is_timeout = "timed out" in str(exc).lower() or isinstance(exc, TimeoutError)
        return {
            # A read timeout means the router ACCEPTED the request but did not
            # answer within deadline+grace: an SLO miss, but NOT yet attributed
            # to the host (a hanging lane produces the same symptom). The gate
            # requires corroboration. Any other transport failure (refused, DNS)
            # is "could not probe" → fail open.
            "ran": is_timeout,
            "ok": False if is_timeout else None,
            "latency_ms": elapsed_ms,
            "error": f"{type(exc).__name__}: {exc}",
            "model": model,
            "local_backpressure": False,
        }


def evaluate_load_gate(
    psi: dict | None,
    probe: dict | None,
    loadavg: float | None,
    cores: int,
    policy: dict | None = None,
) -> dict:
    """Pure gate decision. Never raises; fail-open on absent sensors.

    ``ok`` is True unless a *trustworthy* signal says the host is loaded:

    * PSI cpu/io/memory above threshold (only when the gauge was measured),
    * the probe MISSED its deadline **AND the host is corroborated as the cause**
      (see below),
    * ``loadavg/cores`` over the emergency trip.

    An error that yields ``probe.ran is False`` / ``probe.ok is None`` does NOT
    block: the gate is a throttle, not a hard wall, and a broken sensor is not
    evidence of load.

    Host attribution (2026-10-01): a probe miss is CONFOUNDED — a quiet box with
    a hanging provider lane (``candidate budget 90s exhausted at ollama_cloud_2``)
    produces the same slow completion as genuine host pressure, and gating on it
    would stall dispatch forever on a healthy host. So when
    ``probe_require_host_corroboration`` is set, a miss only defers if EITHER

      * the probe itself carried the router's local-backpressure attribution
        (a 429 / ``local_backpressure`` body), OR
      * a host gauge is at/above ``probe_corroboration_factor`` × its gate
        threshold (or loadavg/core at half its emergency trip).

    A high PSI/loadavg breach alone still defers regardless of the probe (the
    always-on PSI path is unchanged).
    """
    pol = load_policy(policy)
    reasons: list[str] = []
    signals: dict[str, Any] = {}
    cores = max(1, int(cores or 1))

    psi = psi or {}
    gauges = [
        ("cpu_some_avg60", "psi_cpu_some_avg60", "PSI cpu some avg60"),
        ("io_full_avg60", "psi_io_full_avg60", "PSI io full avg60"),
        ("mem_some_avg300", "psi_mem_some_avg300", "PSI memory some avg300"),
        ("cpu_some_avg300", "psi_cpu_some_avg300", "PSI cpu some avg300"),
    ]
    factor = _num(pol.get("probe_corroboration_factor"), 0.5)
    if factor is None:
        factor = 0.5
    host_corroborated = False
    for gauge_key, policy_key, label in gauges:
        val = _num(psi.get(gauge_key))
        signals[gauge_key] = val
        thresh = _num(pol.get(policy_key), None)
        if val is None or thresh is None:
            continue
        if val >= thresh:
            reasons.append(f"{label} {val:.1f}% >= {thresh:.1f}%")
        elif val >= thresh * factor:
            host_corroborated = True

    la = _num(loadavg)
    per_core = (la / cores) if la is not None else None
    signals["loadavg_per_core"] = per_core
    la_max = _num(pol.get("loadavg_per_core_max"), None)
    if per_core is not None and la_max is not None:
        if per_core >= la_max:
            reasons.append(f"loadavg/core {per_core:.1f} >= {la_max:.1f}")
        elif per_core >= la_max * factor:
            host_corroborated = True

    probe = probe or {}
    probe_ran = bool(probe.get("ran"))
    probe_ok = probe.get("ok")
    probe_local = bool(probe.get("local_backpressure"))
    signals["probe_ran"] = probe_ran
    signals["probe_ok"] = probe_ok
    signals["probe_latency_ms"] = probe.get("latency_ms")
    signals["probe_local_backpressure"] = probe_local
    signals["host_corroborated"] = host_corroborated
    if probe_ran and probe_ok is False:
        require = pol.get("probe_require_host_corroboration", True)
        attributed = probe_local or host_corroborated
        if attributed or not require:
            dl = _num(pol.get("probe_deadline_ms"), 3000)
            lat = probe.get("latency_ms")
            why = "router local-backpressure" if probe_local else "host corroborated"
            reasons.append(f"probe missed SLO ({lat}ms > {dl:.0f}ms"
                           + (f", {probe.get('error')}" if probe.get("error") else "")
                           + f"; {why})")
        else:
            # Unattributed miss: likely a provider/lane stall, NOT this host.
            # Throttling on it would be the false signal this task exists to
            # remove, so fail OPEN — the always-on PSI path still guards.
            signals["probe_miss_ignored"] = (
                "probe slow but host not corroborated (likely provider stall)")

    ok = not reasons
    return {
        "ok": ok,
        "reason": "; ".join(reasons) if reasons else "ok",
        "signals": signals,
        "probe_ran": probe_ran,
        "thresholds": {
            "psi_cpu_some_avg60": pol.get("psi_cpu_some_avg60"),
            "psi_io_full_avg60": pol.get("psi_io_full_avg60"),
            "psi_mem_some_avg300": pol.get("psi_mem_some_avg300"),
            "psi_cpu_some_avg300": pol.get("psi_cpu_some_avg300"),
            "loadavg_per_core_max": pol.get("loadavg_per_core_max"),
            "probe_deadline_ms": pol.get("probe_deadline_ms"),
        },
    }


def _default_probe_runner(pol: dict, router_url: str | None) -> Callable:
    """Build the real probe runner: try each ``probe_models`` id until verdict.

    A model no lane declares answers an instant 503 (``ok is None``) — that is
    catalogue drift, not the host SLO, so fall through to the next model. The
    LAST attempt is returned when none yields a verdict, so a genuine read
    timeout on a real lane survives the loop and is still scored as a miss.
    """
    deadline = int(_num(pol.get("probe_deadline_ms"), 3000))
    grace = int(_num(pol.get("probe_grace_ms"), 3000))
    maxtok = int(_num(pol.get("probe_max_tokens"), 5))
    models = pol.get("probe_models") or ("deepseek/deepseek-flash",)
    if isinstance(models, str):
        models = (models,)
    models = tuple(models) or ("deepseek/deepseek-flash",)

    def _run() -> dict:
        last: dict | None = None
        for _m in models:
            last = probe_completion(
                router_url=router_url, deadline_ms=deadline,
                max_tokens=maxtok, model=_m, grace_ms=grace,
            )
            if last.get("ok") is not None:
                return last
        return last or {"ran": False, "ok": None, "latency_ms": None,
                        "error": "no probe model"}

    return _run


def load_gate(
    policy: dict | None = None,
    router_url: str | None = None,
    *,
    psi_reader: Callable | None = None,
    probe_runner: Callable | None = None,
) -> dict:
    """Full gate: read PSI + run the probe + fold. Never raises.

    ``psi_reader`` / ``probe_runner`` are injectable for tests and for the CLI.
    Result dict is the :func:`evaluate_load_gate` dict plus ``enabled`` and
    ``skipped`` bookkeeping so a caller can always answer "why did this not
    gate?".
    """
    pol = load_policy(policy)
    if os.environ.get(KILL_ENV, "1") == "0" or not pol.get("load_gate_enabled", True):
        return {"ok": True, "reason": "load gate disabled", "signals": {},
                "probe_ran": False, "enabled": False, "skipped": "disabled",
                "thresholds": {}}

    psi: dict = {}
    try:
        reader = psi_reader
        if reader is None:
            from gateway.psi import psi_snapshot as reader  # type: ignore
        psi = reader() or {}
    except Exception:
        psi = {}

    probe: dict = {"ran": False, "ok": None, "latency_ms": None, "error": "probe skipped"}
    if os.environ.get(PROBE_ENV, "1") != "0" and pol.get("probe_enabled", True):
        try:
            runner: Callable = probe_runner or _default_probe_runner(pol, router_url)
            probe = runner() or probe
        except Exception as exc:
            probe = {"ran": False, "ok": None, "latency_ms": None,
                     "error": f"{type(exc).__name__}: {exc}"}

    try:
        loadavg = float(open("/proc/loadavg").read().split()[0])
    except Exception:
        loadavg = None

    result = evaluate_load_gate(psi, probe, loadavg, os.cpu_count() or 1, pol)
    result["enabled"] = True
    result["skipped"] = ""
    return result
