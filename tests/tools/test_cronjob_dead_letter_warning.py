"""Dead-letter warning: creating a job in a store no ticker services must say so.

Dispatched workers and profile-scoped agents run with HERMES_HOME pointed at a
profile home whose cron store is only ticked when a gateway serves that profile
(per-profile design, #4707). On a single-gateway host the profile store has no
ticker at all, and the ``cronjob`` tool used to answer a plain
``Cron job 'x' created.`` for a job that would never fire (filed: comp-gov
5f7cd0a9da1f dead from creation; rp5-shadow-health-check 989451edaa98 dead for
11 days without anyone noticing — next_run_at frozen, last_run_at null).

The create result must carry a loud, structured warning derived from the
STORE-SCOPED ticker heartbeat (never gateway pids: a gateway running for a
different home is exactly the false-negative case).
"""
from __future__ import annotations

import json
import time

import pytest

from tools.cronjob_tools import cronjob


@pytest.fixture(autouse=True)
def _setup_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")


def _write_heartbeat(tmp_path, age_seconds):
    hb = tmp_path / "cron" / "ticker_heartbeat"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text(str(time.time() - age_seconds), encoding="utf-8")


def test_no_heartbeat_ever_warns_loudly():
    created = json.loads(
        cronjob(action="create", prompt="Check", schedule="every 1h", name="dead")
    )
    # The job IS created — the warning must not turn create into a failure
    # (a profile gateway may start later; CLI parity with #51038).
    assert created["success"] is True
    assert created["job_id"]
    assert created["ticker_liveness"] == "never"
    assert created["warning"]
    # Loud and actionable, not buried in a nested field.
    assert "⚠" in created["message"]
    assert "will not fire" in created["warning"]
    assert "hermes cron status" in created["warning"]


def test_stale_heartbeat_warns(tmp_path):
    # Well past the `hermes cron status` staleness threshold (3 missed
    # iterations + slack): the ticker that once served this store is gone.
    _write_heartbeat(tmp_path, (60 * 3 + 20) * 2)
    created = json.loads(
        cronjob(action="create", prompt="Check", schedule="every 1h")
    )
    assert created["success"] is True
    assert created["ticker_liveness"] == "stale"
    assert created["warning"]


def test_fresh_heartbeat_no_warning(tmp_path):
    _write_heartbeat(tmp_path, 5)
    created = json.loads(
        cronjob(action="create", prompt="Check", schedule="every 1h")
    )
    assert created["success"] is True
    assert created["ticker_liveness"] == "live"
    assert not created.get("warning")
    assert "⚠" not in created["message"]


def test_external_provider_never_warns(monkeypatch):
    """External providers fire via webhook, not the in-process ticker —
    a missing heartbeat is expected there, same carve-out as the CLI's
    `_warn_if_gateway_not_running`."""
    import cron.scheduler_provider as sp

    class _Chronos:
        # Minimal external-provider stub: name + the registration hooks the
        # create path calls (CronScheduler's own defaults are no-ops).
        name = "chronos"

        def register_job(self, job):
            return None

        def on_jobs_changed(self):
            return None

    monkeypatch.setattr(sp, "resolve_cron_scheduler", lambda: _Chronos())
    created = json.loads(
        cronjob(action="create", prompt="Check", schedule="every 1h")
    )
    assert created["success"] is True
    assert not created.get("warning")
    assert "ticker_liveness" not in created


def test_torn_heartbeat_is_quiet_unknown(tmp_path):
    """A present-but-unreadable heartbeat (torn write) is `unknown`, and
    unknown is QUIET: the store may well be ticked, so no DEAD LETTER
    warning — only the structured status field, and create still succeeds."""
    hb = tmp_path / "cron" / "ticker_heartbeat"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text("garbage-torn-write", encoding="utf-8")
    created = json.loads(
        cronjob(action="create", prompt="Check", schedule="every 1h")
    )
    assert created["success"] is True
    assert created["ticker_liveness"] == "unknown"
    assert not created.get("warning")
    assert "⚠" not in created["message"]


def test_indeterminate_provider_is_quiet(monkeypatch):
    """If the provider probe is indeterminate (active_provider_name → None),
    the firing mechanism is unknown — heartbeat heuristics don't apply and
    the create result carries no liveness fields at all (advisory probe,
    never a false alarm and never a create failure). The probe is patched
    directly: a resolve_cron_scheduler that RAISES also fails create's
    provider registration, which is separate, pre-existing behavior."""
    import cron.scheduler_provider as sp

    monkeypatch.setattr(sp, "active_provider_name", lambda: None)
    created = json.loads(
        cronjob(action="create", prompt="Check", schedule="every 1h")
    )
    assert created["success"] is True
    assert not created.get("warning")
    assert "ticker_liveness" not in created


def test_liveness_probe_failure_still_creates(monkeypatch):
    """The probe is advisory: a broken heartbeat read must never break create."""
    import cron.jobs as jobs_mod

    def _boom(*a, **kw):
        raise OSError("heartbeat unreadable")

    monkeypatch.setattr(jobs_mod, "get_ticker_liveness", _boom)
    created = json.loads(
        cronjob(action="create", prompt="Check", schedule="every 1h")
    )
    assert created["success"] is True
    assert not created.get("warning")


def test_list_has_no_warning_fields():
    cronjob(action="create", prompt="Check", schedule="every 1h")
    listing = json.loads(cronjob(action="list"))
    assert listing["success"] is True
    assert "warning" not in listing
