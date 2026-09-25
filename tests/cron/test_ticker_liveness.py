"""Unit tests for ``cron.jobs.get_ticker_liveness`` — store-scoped liveness.

Statuses (behavior contract, not snapshot):
  - no heartbeat file → ``never`` (nothing has ever ticked this store)
  - heartbeat older than the threshold → ``stale``
  - fresh heartbeat → ``live``
Threshold defaults to the shared ``TICKER_INTERVAL_SECONDS * 3 + 20`` used by
`hermes cron status` so the tool and the CLI can never disagree.
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants
    import cron.jobs

    importlib.reload(hermes_constants)
    importlib.reload(cron.jobs)
    return home


def _heartbeat(home, age_seconds):
    (home / "cron" / "ticker_heartbeat").write_text(
        str(time.time() - age_seconds), encoding="utf-8"
    )


def test_missing_heartbeat_is_never(hermes_env):
    from cron.jobs import get_ticker_liveness

    v = get_ticker_liveness()
    assert v["status"] == "never"
    assert v["heartbeat_age"] is None
    assert v["stale_after"] == 60 * 3 + 20


def test_old_heartbeat_is_stale(hermes_env):
    from cron.jobs import get_ticker_liveness, TICKER_INTERVAL_SECONDS

    _heartbeat(hermes_env, TICKER_INTERVAL_SECONDS * 3 + 20 + 1)
    assert get_ticker_liveness()["status"] == "stale"


def test_fresh_heartbeat_is_live(hermes_env):
    from cron.jobs import get_ticker_liveness

    _heartbeat(hermes_env, 5)
    v = get_ticker_liveness()
    assert v["status"] == "live"
    assert 4 <= v["heartbeat_age"] <= 10


def test_custom_threshold_respected(hermes_env):
    from cron.jobs import get_ticker_liveness

    _heartbeat(hermes_env, 120)
    assert get_ticker_liveness(stale_after=1000)["status"] == "live"
    assert get_ticker_liveness(stale_after=60)["status"] == "stale"


def test_scoped_to_active_profile_store(hermes_env, tmp_path):
    """The verdict must come from the ACTIVE store (`_current_cron_store`),
    so a profile home with no heartbeat reports `never` even while another
    home's store is freshly ticked — the exact dq05 false-negative shape."""
    from cron.jobs import get_ticker_liveness, use_cron_store

    other = tmp_path / "other-home"
    (other / "cron").mkdir(parents=True)
    _heartbeat(other, 5)

    assert get_ticker_liveness()["status"] == "never"
    with use_cron_store(other):
        assert get_ticker_liveness()["status"] == "live"
    assert get_ticker_liveness()["status"] == "never"


# ── unknown: present-but-unreadable heartbeat is NOT "never" ──────────────────
# The reader's contract (get_ticker_heartbeat_age) says None = "cannot
# determine" (missing, torn read, I/O error) — callers must not read it as
# "dead". Liveness keeps that distinction: only a PROVABLY ABSENT file is
# `never`; a file we failed to read is `unknown` and must stay quiet.


def test_torn_heartbeat_is_unknown_not_never(hermes_env):
    """A heartbeat file whose content can't be parsed (torn/partial write)
    reports `unknown`, never a loud `never` — a store that IS being ticked
    must not be told its jobs are dead letters."""
    from cron.jobs import get_ticker_liveness

    (hermes_env / "cron" / "ticker_heartbeat").write_text(
        "not-an-epoch-at-all", encoding="utf-8"
    )
    v = get_ticker_liveness()
    assert v["status"] == "unknown"
    assert v["heartbeat_age"] is None


def test_empty_heartbeat_is_unknown(hermes_env):
    from cron.jobs import get_ticker_liveness

    (hermes_env / "cron" / "ticker_heartbeat").write_text("", encoding="utf-8")
    assert get_ticker_liveness()["status"] == "unknown"


def test_unknown_is_distinct_from_missing(hermes_env):
    """Same probe, two stores: absent file → `never`; present-but-torn →
    `unknown`. The distinction is the whole point of the status."""
    from cron.jobs import get_ticker_liveness, use_cron_store

    torn = hermes_env / "torn-home"
    (torn / "cron").mkdir(parents=True)
    (torn / "cron" / "ticker_heartbeat").write_text("garbage", encoding="utf-8")

    assert get_ticker_liveness()["status"] == "never"
    with use_cron_store(torn):
        assert get_ticker_liveness()["status"] == "unknown"


def test_default_threshold_is_the_shared_constant(hermes_env):
    """The staleness threshold is ONE constant shared with `hermes cron
    status` — the tool and the CLI must never disagree about the same
    store (a duplicated literal could drift)."""
    import cron.jobs as jobs_mod

    assert jobs_mod.TICKER_STALE_AFTER_SECONDS == 60 * 3 + 20
    from cron.jobs import get_ticker_liveness

    assert get_ticker_liveness()["stale_after"] == jobs_mod.TICKER_STALE_AFTER_SECONDS

    import hermes_cli.cron as cli_cron

    assert cli_cron.TICKER_STALE_AFTER_SECONDS == jobs_mod.TICKER_STALE_AFTER_SECONDS
