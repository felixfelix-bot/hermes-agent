"""RED-first tests for the host-pressure dispatch gate (Phase 2, 2026-09-30).

The gap these pin: the dispatch governor judged host load from PSI-less
``cpu_load_1m`` (a load AVERAGE that conflates I/O wait) with no end-to-end
ground truth. Under real load an 8-token completion took 14-25 s and the
router's candidate budget expired into the *same* ``503 all providers
exhausted`` string as genuine exhaustion, which made the fleet wrongly conclude
cross-family review was impossible.

The gate must therefore:
  * use PSI + a bounded probe as the signal (never raw loadavg alone),
  * report ``probe_ran=False`` and fail OPEN when the sensor cannot run,
  * still allow REVIEW work while refusing new WORKER spawns at the same
    signal (a review is part of the critical path; a fresh worker is not),
  * keep the reviewer-lane hold visible without a KeyError.
"""
from gateway.dispatch_probe import (
    DEFAULT_POLICY,
    evaluate_load_gate,
    load_gate,
)
from gateway.psi import parse_psi, psi_snapshot

# reviewer_headroom is a SIBLING lane's addition (2026-09-30 incident hotfix,
# uncommitted on main at the time of writing). Guard the import so this file
# still collects against a base without it; the reviewer tests skip cleanly.
try:  # pragma: no cover - presence depends on the base
    from gateway.dispatch_headroom import reviewer_headroom
    _HAS_REVIEWER = True
except Exception:  # pragma: no cover
    reviewer_headroom = None
    _HAS_REVIEWER = False

import pytest

_needs_reviewer = pytest.mark.skipif(
    not _HAS_REVIEWER, reason="reviewer_headroom not present on this base")


# ── PSI parsing ─────────────────────────────────────────────────────────────

_PSI_CPU = "some avg10=6.39 avg60=4.46 avg300=2.30 total=62731238087\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"


def test_parse_psi_reads_both_kinds():
    d = parse_psi(_PSI_CPU)
    assert d["some"]["avg60"] == 4.46
    assert d["some"]["avg300"] == 2.30
    assert d["full"]["avg10"] == 0.0


def test_parse_psi_tolerates_garbage():
    # A malformed value must be dropped, never raised — a torn read must not
    # break dispatch.
    d = parse_psi("some avg10=oops avg60=1.0\n\nnot-a-line\nfull total=5\n")
    assert d["some"]["avg60"] == 1.0
    assert "avg10" not in d["some"] or d["some"]["avg10"] is None
    assert d["full"]["total"] == 5.0


def test_psi_snapshot_missing_root_is_all_none(tmp_path):
    snap = psi_snapshot(str(tmp_path / "nope"))
    assert snap["available"] is False
    assert snap["cpu_some_avg60"] is None
    assert snap["io_full_avg60"] is None


def test_psi_snapshot_reads_files(tmp_path):
    (tmp_path / "cpu").write_text(_PSI_CPU)
    (tmp_path / "io").write_text("some avg60=1.0\nfull avg60=15.1\n")
    (tmp_path / "memory").write_text("some avg300=0.2\n")
    snap = psi_snapshot(str(tmp_path))
    assert snap["available"] is True
    assert snap["cpu_some_avg60"] == 4.46
    assert snap["io_full_avg60"] == 15.1
    assert snap["mem_some_avg300"] == 0.2


# ── pure gate decision ──────────────────────────────────────────────────────

def _ok_probe():
    return {"ran": True, "ok": True, "latency_ms": 800, "error": ""}


def _slow_probe(ms=9000):
    return {"ran": True, "ok": False, "latency_ms": ms, "error": ""}


def _no_probe():
    return {"ran": False, "ok": None, "latency_ms": 120, "error": "ConnectionRefusedError"}


def test_quiet_box_is_ok():
    psi = {"cpu_some_avg60": 4.0, "io_full_avg60": 0.5, "mem_some_avg300": 0.1,
           "cpu_some_avg300": 2.0}
    r = evaluate_load_gate(psi, _ok_probe(), 1.5, 4)
    assert r["ok"] is True
    assert r["reason"] == "ok"


def test_high_psi_cpu_blocks():
    psi = {"cpu_some_avg60": 45.0, "io_full_avg60": 0.5, "mem_some_avg300": 0.0,
           "cpu_some_avg300": 2.0}
    r = evaluate_load_gate(psi, _ok_probe(), 1.5, 4)
    assert r["ok"] is False
    assert "cpu some avg60" in r["reason"]


def test_high_psi_io_full_blocks():
    psi = {"cpu_some_avg60": 1.0, "io_full_avg60": 15.1, "mem_some_avg300": 0.0,
           "cpu_some_avg300": 1.0}
    r = evaluate_load_gate(psi, _ok_probe(), 1.5, 4)
    assert r["ok"] is False
    assert "io full" in r["reason"]


def test_slow_probe_alone_does_not_block_on_a_clean_host():
    # REFINED 2026-10-01. The original spec said a slow probe blocks even when
    # PSI looks fine — but measured live: a QUIET box (PSI 2.4 %, loadavg 0.9)
    # served a 5-token completion in ~9 s because the router was stuck on a
    # hanging lane. Gating on that stalls dispatch forever on a healthy host,
    # which is the same class of false signal this task removes. A miss is only
    # host pressure when the HOST is corroborated.
    psi = {"cpu_some_avg60": 5.0, "io_full_avg60": 1.0, "mem_some_avg300": 0.0,
           "cpu_some_avg300": 5.0}
    r = evaluate_load_gate(psi, _slow_probe(), 1.0, 4)
    assert r["ok"] is True
    assert "probe_miss_ignored" in r["signals"]


def test_slow_probe_blocks_when_host_is_corroborated():
    # Same slow probe, but PSI cpu some avg60 is at/above half its gate
    # threshold (10 % of 20 %) → the host IS the plausible cause → defer.
    psi = {"cpu_some_avg60": 12.0, "io_full_avg60": 1.0, "mem_some_avg300": 0.0,
           "cpu_some_avg300": 5.0}
    r = evaluate_load_gate(psi, _slow_probe(), 1.0, 4)
    assert r["ok"] is False
    assert "SLO" in r["reason"] and "corroborated" in r["reason"]


def test_local_backpressure_verdict_blocks_without_psi():
    # The router itself attributed the failure to local backpressure (429 /
    # local_backpressure body): that is a host verdict and needs no PSI.
    probe = {"ran": True, "ok": False, "latency_ms": 120,
             "error": "HTTP 429", "local_backpressure": True}
    psi = {"cpu_some_avg60": 1.0, "io_full_avg60": 0.0, "mem_some_avg300": 0.0,
           "cpu_some_avg300": 1.0}
    r = evaluate_load_gate(psi, probe, 1.0, 4)
    assert r["ok"] is False
    assert "local-backpressure" in r["reason"]


def test_corroboration_can_be_disabled_by_policy():
    # Operators may pin the original strict behaviour (block on any miss).
    psi = {"cpu_some_avg60": 1.0, "io_full_avg60": 0.0, "mem_some_avg300": 0.0,
           "cpu_some_avg300": 1.0}
    r = evaluate_load_gate(psi, _slow_probe(), 1.0, 4,
                           policy={"probe_require_host_corroboration": False})
    assert r["ok"] is False


def test_emergency_loadavg_trip_blocks():
    psi = {"cpu_some_avg60": 0.0, "io_full_avg60": 0.0, "mem_some_avg300": 0.0,
           "cpu_some_avg300": 0.0}
    r = evaluate_load_gate(psi, _ok_probe(), 30.0, 4)  # 7.5/core > 6.0
    assert r["ok"] is False
    assert "loadavg/core" in r["reason"]


def test_probe_that_never_ran_fails_open():
    # Router unreachable is NOT evidence of load — the gate must not wedge.
    psi = {"cpu_some_avg60": 1.0, "io_full_avg60": 1.0, "mem_some_avg300": 0.0,
           "cpu_some_avg300": 1.0}
    r = evaluate_load_gate(psi, _no_probe(), 1.0, 4)
    assert r["ok"] is True
    assert r["probe_ran"] is False


def test_absent_psi_gauges_fail_open():
    r = evaluate_load_gate({}, _ok_probe(), None, 4)
    assert r["ok"] is True


def test_custom_policy_threshold_respected():
    psi = {"cpu_some_avg60": 12.0}
    assert evaluate_load_gate(psi, _ok_probe(), 1.0, 4)["ok"] is True
    r = evaluate_load_gate(psi, _ok_probe(), 1.0, 4,
                           policy={"psi_cpu_some_avg60": 10.0})
    assert r["ok"] is False


# ── full gate (injected readers) ────────────────────────────────────────────

def test_load_gate_uses_injected_sensors():
    r = load_gate(
        psi_reader=lambda: {"cpu_some_avg60": 50.0},
        probe_runner=_ok_probe,
    )
    assert r["ok"] is False and r["enabled"] is True


def test_load_gate_kill_switch(monkeypatch):
    monkeypatch.setenv("HERMES_LOAD_GATE", "0")
    r = load_gate(psi_reader=lambda: {"cpu_some_avg60": 99.0},
                  probe_runner=_slow_probe)
    assert r["ok"] is True
    assert r["enabled"] is False


def test_load_gate_probe_kill_switch_skips_only_the_probe(monkeypatch):
    monkeypatch.setenv("HERMES_LOAD_PROBE", "0")
    called = {"n": 0}

    def _runner():
        called["n"] += 1
        return _slow_probe()

    r = load_gate(psi_reader=lambda: {"cpu_some_avg60": 1.0},
                  probe_runner=_runner)
    assert called["n"] == 0            # probe skipped
    assert r["ok"] is True             # PSI still clean
    assert r["probe_ran"] is False


def test_load_gate_reader_exception_fails_open():
    def _boom():
        raise RuntimeError("procfs gone")

    r = load_gate(psi_reader=_boom, probe_runner=_ok_probe)
    assert r["ok"] is True


def test_default_policy_has_the_plan_thresholds():
    assert DEFAULT_POLICY["probe_deadline_ms"] == 3000
    assert DEFAULT_POLICY["probe_max_tokens"] == 5
    assert DEFAULT_POLICY["loadavg_per_core_max"] == 6.0


# ── probe verdict semantics (2026-10-01: the false-block defect) ─────────────
#
# Found live on 2026-10-01: the probe's default model ("glm-4.5-flash") was no
# longer a declared lane, so the router answered an INSTANT 503 ("no lane
# declares this model", 26 ms) and the gate scored that fast error as "probe
# missed SLO" — which would block ALL new dispatch on a perfectly quiet box.
# Worse, the read timeout EQUALLED the deadline, so a response that genuinely
# took 14-25 s could never be measured as slow; it was always an
# indistinguishable read timeout. These pin the corrected contract.

import urllib.error  # noqa: E402


class _Resp:
    """Minimal context-manager stand-in for the urlopen response."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fast_http_error_is_not_a_verdict_and_does_not_block():
    # A 503 in 26 ms is a router/lane answer, NOT host pressure. It must yield
    # NO verdict (ok=None) so the gate fails open, never a false SLO miss.
    def _opener(req, timeout=None):
        raise urllib.error.HTTPError("http://x", 503, "Service Unavailable", None, None)

    from gateway.dispatch_probe import probe_completion
    r = probe_completion(_opener=_opener)
    assert r["ok"] is None
    assert r["ran"] is True
    assert "503" in r["error"]


def test_read_timeout_is_the_one_real_slo_miss():
    # The router accepted the request but did not answer within deadline+grace:
    # that IS the condition the gate exists to detect.
    import gateway.dispatch_probe as dp

    def _opener(req, timeout=None):
        raise TimeoutError("timed out")

    r = dp.probe_completion(_opener=_opener, deadline_ms=3000, grace_ms=1000)
    assert r["ok"] is False
    assert r["ran"] is True


def test_transport_refusal_fails_open():
    def _opener(req, timeout=None):
        raise ConnectionRefusedError("connection refused")

    from gateway.dispatch_probe import probe_completion
    r = probe_completion(_opener=_opener)
    assert r["ran"] is False
    assert r["ok"] is None


def test_read_timeout_exceeds_the_deadline():
    # The read timeout must be deadline + grace, otherwise a slow-but-arriving
    # response is cut off at the deadline and its real latency is unmeasurable.
    import gateway.dispatch_probe as dp

    seen = {}

    def _opener(req, timeout=None):
        seen["timeout"] = timeout
        return _Resp(b'{"choices":[]}')

    dp.probe_completion(_opener=_opener, deadline_ms=3000, grace_ms=2000)
    assert seen["timeout"] == 5.0


def test_good_completion_is_a_verdict():
    from gateway.dispatch_probe import probe_completion
    r = probe_completion(_opener=lambda req, timeout=None: _Resp(b'{"choices":[]}'))
    assert r["ok"] is True


def test_default_probe_runner_falls_through_declared_lane_drift(monkeypatch):
    # First model: no lane declares it → instant 503 (ok=None). Second model:
    # a real verdict. The runner must use the second, not give up.
    import gateway.dispatch_probe as dp

    calls = []

    def _fake(router_url=None, deadline_ms=3000, max_tokens=5, model="x",
              _opener=None, grace_ms=None):
        calls.append(model)
        if model == "gone":
            return {"ran": True, "ok": None, "latency_ms": 20,
                    "error": "HTTP 503", "model": model}
        return {"ran": True, "ok": True, "latency_ms": 900, "error": "",
                "model": model}

    monkeypatch.setattr(dp, "probe_completion", _fake)
    run = dp._default_probe_runner({"probe_models": ("gone", "real")}, None)
    r = run()
    assert r["ok"] is True
    assert calls == ["gone", "real"]


def test_default_probe_runner_keeps_a_real_timeout_miss(monkeypatch):
    # If NO model yields a verdict but one TIMED OUT, the miss must survive the
    # fallback loop (a timeout is real SLO evidence, not catalogue drift).
    import gateway.dispatch_probe as dp

    def _fake(router_url=None, deadline_ms=3000, max_tokens=5, model="x",
              _opener=None, grace_ms=None):
        return {"ran": True, "ok": False, "latency_ms": 9000,
                "error": "TimeoutError: timed out", "model": model}

    monkeypatch.setattr(dp, "probe_completion", _fake)
    run = dp._default_probe_runner({"probe_models": ("a", "b")}, None)
    assert run()["ok"] is False


# ── reviewer-lane hold must not KeyError in the reason builder ──────────────

@_needs_reviewer
def test_reviewer_headroom_fails_open_without_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert reviewer_headroom({}) == 1.0


@_needs_reviewer
def test_reviewer_headroom_zero_when_probe_says_not_ready(tmp_path, monkeypatch):
    import json
    import time
    bot = tmp_path / ".hermes" / "bot"
    bot.mkdir(parents=True)
    (bot / "reviewer_readiness.json").write_text(json.dumps(
        {"ready": False, "ts": time.time()}))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert reviewer_headroom({}) == 0.0


@_needs_reviewer
def test_reviewer_headroom_ignores_stale_probe(tmp_path, monkeypatch):
    import json
    import time
    bot = tmp_path / ".hermes" / "bot"
    bot.mkdir(parents=True)
    (bot / "reviewer_readiness.json").write_text(json.dumps(
        {"ready": False, "ts": time.time() - 99999}))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert reviewer_headroom({}) == 1.0
