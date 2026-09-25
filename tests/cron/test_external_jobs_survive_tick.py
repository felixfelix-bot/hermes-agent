"""Regression: externally-added jobs must survive tick() and mark_job_run().

Filed incident (t_7419f6af sibling hazard): comp-gov-1787437592 vanished from
the default store ~2026-09-05; a role-46 comment claims "the gateway owns the
in-memory job list and clobbers CLI-added crons". These tests pin the contract
that a store mutation performed by the ticker path (advance_next_runs /
mark_job_run inside tick(), or a direct stale-payload save) must preserve a job
that a SEPARATE process (``hermes cron add`` / ensure_cron.py / hand edit)
wrote into jobs.json between this process's load and save — the shrink-merge
guard (#80624) extended to the tick path.

NON-VACUITY: the external write must land AFTER the operation's own
load_jobs() and BEFORE its save_jobs(). Writing it before the call would make
survival trivial (the fresh load already sees it) — that shape passes even
with the guard deleted. Each test below injects the sibling write mid-flight
(inside the load→save window of the code under test) via a hook the real code
calls between the two: mark_job_run touches ``_hermes_now`` after its load;
advance_next_runs calls ``compute_next_run`` between its load and its save;
the direct test performs the load→write→stale-save sequence explicitly.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from hermes_time import now as hermes_now


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants
    import cron.jobs
    import cron.scheduler

    importlib.reload(hermes_constants)
    importlib.reload(cron.jobs)
    importlib.reload(cron.scheduler)
    return home


def _jobs_file(home):
    return home / "cron" / "jobs.json"


def _external_add(home, job_id, **fields):
    """Add a job to jobs.json the way a separate process would: raw read,
    append, write — no in-process state, no lock, no stamp refresh."""
    path = _jobs_file(home)
    data = json.loads(path.read_text(encoding="utf-8"))
    job = {
        "id": job_id,
        "name": fields.get("name", job_id),
        "prompt": fields.get("prompt", "external"),
        "schedule": {"kind": "interval", "minutes": 60, "display": "every 60m"},
        "schedule_display": "every 60m",
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "deliver": "local",
        "next_run_at": fields.get(
            "next_run_at", (hermes_now() + timedelta(hours=1)).isoformat()
        ),
    }
    data["jobs"].append(job)
    data["updated_at"] = hermes_now().isoformat()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return job


def _backdate(home, job_id):
    """Make a job due NOW with a raw disk edit (same shape a CLI sibling
    would use), so tick() dispatches it this pass."""
    path = _jobs_file(home)
    data = json.loads(path.read_text(encoding="utf-8"))
    for job in data["jobs"]:
        if job["id"] == job_id:
            job["next_run_at"] = (hermes_now() - timedelta(minutes=1)).isoformat()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_stale_payload_save_preserves_external_job(hermes_env):
    """THE guard pin, stated directly: save_jobs() called with a list loaded
    BEFORE a sibling's out-of-band write must not shrink the store. This is
    exactly the degraded-writer shape #80624 guards (loaded older snapshot,
    then saves and would otherwise clobber the concurrent create)."""
    from cron.jobs import _jobs_lock, create_job, load_jobs, save_jobs

    agent = create_job(prompt="hello", schedule="every 5m", name="agent", deliver="local")

    with _jobs_lock():
        stale = load_jobs()  # payload WITHOUT the sibling's job
        assert all(j["id"] != "ext-direct" for j in stale)
        _external_add(hermes_env, "ext-direct")  # sibling lands mid-section
        save_jobs(stale)  # stale-payload write: the clobber attempt

    stored = {j["id"] for j in load_jobs()}
    assert "ext-direct" in stored, "stale-payload save clobbered the sibling's job"
    assert agent["id"] in stored


def test_mark_job_run_preserves_external_job_landed_mid_section(hermes_env, monkeypatch):
    """mark_job_run() does load → mutate → save under one lock section. A
    sibling write that lands between its load and its save must survive —
    injected via ``_hermes_now``, which mark_job_run calls right after the
    load, before the save."""
    import cron.jobs as jobs_mod
    from cron.jobs import create_job, load_jobs

    agent = create_job(prompt="hello", schedule="every 5m", name="agent", deliver="local")

    real_now = jobs_mod._hermes_now
    fired: list[bool] = []

    def now_with_sibling():
        if not fired:
            fired.append(True)
            _external_add(hermes_env, "ext-midflight")
        return real_now()

    # Armed AFTER create_job so the hook's single shot fires inside
    # mark_job_run's critical section, not during create.
    monkeypatch.setattr(jobs_mod, "_hermes_now", now_with_sibling)
    jobs_mod.mark_job_run(agent["id"], success=True)
    assert fired, "hook never fired — test is vacuous"

    stored = {j["id"]: j for j in load_jobs()}
    assert "ext-midflight" in stored, "mark_job_run clobbered the sibling's job"
    assert stored[agent["id"]]["last_status"] == "ok"
    assert stored[agent["id"]]["next_run_at"] != agent["next_run_at"]


def test_tick_preserves_external_job_landed_during_advance(hermes_env, monkeypatch):
    """tick()'s at-most-once advance (advance_next_runs) is a load → compute →
    save batch. A sibling write landing inside that window (injected via
    ``compute_next_run``, called between the load and the save) must survive
    the tick's rewrite of jobs.json."""
    import cron.jobs as jobs_mod
    from cron.jobs import create_job, load_jobs
    from cron.scheduler import tick

    agent = create_job(prompt="hello", schedule="every 5m", name="agent", deliver="local")
    _backdate(hermes_env, agent["id"])

    real_cnr = jobs_mod.compute_next_run
    fired: list[bool] = []

    def cnr_with_sibling(schedule, now):
        if not fired:
            fired.append(True)
            _external_add(hermes_env, "ext-tick")
        return real_cnr(schedule, now)

    monkeypatch.setattr(jobs_mod, "compute_next_run", cnr_with_sibling)
    # Execute nothing for real: the delivery/agent path is irrelevant to the
    # preservation contract under test.
    monkeypatch.setattr(
        "cron.scheduler.run_one_job", lambda job, **kw: True, raising=True
    )

    executed = tick(verbose=False, can_dispatch=lambda: True)

    assert executed >= 1
    assert fired, "hook never fired — test is vacuous"
    stored = {j["id"]: j for j in load_jobs()}
    assert "ext-tick" in stored, "tick clobbered the sibling's job"
    assert stored["ext-tick"]["prompt"] == "external"
    # The due job was advanced past its backdated slot (at-most-once semantics).
    from datetime import datetime

    advanced_to = datetime.fromisoformat(stored[agent["id"]]["next_run_at"])
    assert advanced_to > hermes_now() - timedelta(seconds=1)
