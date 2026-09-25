"""Regression: externally-added jobs must survive tick() and mark_job_run().

Filed incident (t_7419f6af sibling hazard): comp-gov-1787437592 vanished from
the default store ~2026-09-05; a role-46 comment claims "the gateway owns the
in-memory job list and clobbers CLI-added crons". These tests pin the contract
that a store mutation performed by the ticker path (advance_next_runs /
mark_job_run inside tick(), or a direct mark_job_run call) must preserve a job
that a SEPARATE process (``hermes cron add`` / ensure_cron.py / hand edit)
wrote into jobs.json between this process's load and save — the shrink-merge
guard (#80624) extended to the tick path.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

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


def test_mark_job_run_preserves_externally_added_job(hermes_env):
    from cron.jobs import create_job, load_jobs, mark_job_run

    agent = create_job(prompt="hello", schedule="every 5m", name="agent", deliver="local")
    external = _external_add(hermes_env, "ext-11111")

    mark_job_run(agent["id"], success=True)

    stored = {j["id"]: j for j in load_jobs()}
    assert "ext-11111" in stored
    assert stored["ext-11111"]["prompt"] == "external"
    assert stored[agent["id"]]["last_status"] == "ok"
    assert stored[agent["id"]]["next_run_at"] != agent["next_run_at"]


def test_tick_preserves_externally_added_job(hermes_env, monkeypatch):
    from cron.jobs import create_job, load_jobs
    from cron.scheduler import tick

    agent = create_job(prompt="hello", schedule="every 5m", name="agent", deliver="local")

    # Make the agent job due NOW and add the external job in the same
    # out-of-band disk edit (a CLI sibling acting between our load and tick).
    backdated = (hermes_now() - timedelta(minutes=1)).isoformat()
    path = _jobs_file(hermes_env)
    data = json.loads(path.read_text(encoding="utf-8"))
    for job in data["jobs"]:
        if job["id"] == agent["id"]:
            job["next_run_at"] = backdated
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    external = _external_add(hermes_env, "ext-22222")

    # Execute nothing for real: the delivery/agent path is irrelevant to the
    # preservation contract under test.
    monkeypatch.setattr(
        "cron.scheduler.run_one_job", lambda job, **kw: True, raising=True
    )

    executed = tick(verbose=False, can_dispatch=lambda: True)

    assert executed >= 1
    stored = {j["id"]: j for j in load_jobs()}
    assert "ext-22222" in stored
    assert stored["ext-22222"]["prompt"] == "external"
    # The due job was advanced past its backdated slot (at-most-once semantics).
    advanced_to = datetime.fromisoformat(stored[agent["id"]]["next_run_at"])
    assert advanced_to > hermes_now() - timedelta(seconds=1)
