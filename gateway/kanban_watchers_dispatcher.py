"""Embedded kanban dispatcher: settings resolution and per-tick board work.

``GatewayKanbanWatchersMixin._kanban_dispatcher_watcher`` owns the loop,
the singleton lock and the health telemetry; everything that only needs the
``kanban_db`` module and the resolved settings lives here.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from gateway.kanban_watchers_common import _board_slugs, _positive_int_setting, logger


def _kbc():
    from hermes_cli import kanban_db_connect
    return kanban_db_connect


def _kbd():
    from hermes_cli import kanban_db_dispatch
    return kanban_db_dispatch

_CORRUPT_DB_MARKERS = ("file is not a database", "database disk image is malformed")


@dataclass
class _DispatcherSettings:
    """``kanban.*`` dispatch settings, read once at boot (restart to apply)."""

    interval: float
    max_spawn: Any
    max_in_progress: Optional[int]
    failure_limit: int
    stale_timeout_seconds: int
    reconcile_orphans: bool
    default_assignee: Optional[str]
    max_in_progress_per_profile: Optional[int]


def _resolve_dispatcher_settings(kanban_cfg: dict, kb: Any) -> _DispatcherSettings:
    """Parse and log the dispatcher settings in their established order."""
    try:
        interval = float(kanban_cfg.get("dispatch_interval_seconds", 60) or 60)
    except (ValueError, TypeError):
        logger.warning("kanban dispatcher: invalid dispatch_interval_seconds=%r, using default 60",
                       kanban_cfg.get("dispatch_interval_seconds"))
        interval = 60.0
    interval = max(interval, 1.0)  # sanity floor — tighter than this is a footgun

    max_spawn = kanban_cfg.get("max_spawn")
    if max_spawn is not None:
        logger.info("kanban dispatcher: max_spawn=%s", max_spawn)

    # Cap simultaneously running tasks so slow workers don't pile up and time
    # out. Explicit config wins; otherwise a memory-derived default (unbounded
    # fan-out swap-thrashes small hosts), or None where total memory can't be read.
    max_in_progress = _positive_int_setting(kanban_cfg, "max_in_progress")
    effective_max_in_progress = _kbd().resolve_max_in_progress(max_in_progress)
    if max_in_progress is None and effective_max_in_progress is not None:
        logger.info(
            "kanban dispatcher: kanban.max_in_progress unset; using "
            "memory-derived default max_in_progress=%d "
            "(set kanban.max_in_progress in config.yaml to override)",
            effective_max_in_progress,
        )

    raw_failure_limit = kanban_cfg.get("failure_limit", kb.DEFAULT_FAILURE_LIMIT)
    try:
        failure_limit = int(raw_failure_limit)
    except (TypeError, ValueError):
        logger.warning("kanban dispatcher: invalid kanban.failure_limit=%r; using default %d",
                       raw_failure_limit, kb.DEFAULT_FAILURE_LIMIT)
        failure_limit = kb.DEFAULT_FAILURE_LIMIT
    if failure_limit < 1:
        logger.warning("kanban dispatcher: kanban.failure_limit=%r is below 1; using default %d",
                       raw_failure_limit, kb.DEFAULT_FAILURE_LIMIT)
        failure_limit = kb.DEFAULT_FAILURE_LIMIT

    # 0 disables stale detection.
    raw_stale = kanban_cfg.get("dispatch_stale_timeout_seconds", 0)
    try:
        stale_timeout_seconds = int(raw_stale or 0)
    except (TypeError, ValueError):
        logger.warning("kanban dispatcher: invalid kanban.dispatch_stale_timeout_seconds=%r; "
                       "disabling stale detection", raw_stale)
        stale_timeout_seconds = 0

    # Fallback profile for tasks created without an assignee (e.g. via the
    # dashboard). Empty (the schema default) keeps skipping them.
    # When set, the dispatcher applies it to unassigned ready tasks instead of skipping them indefinitely
    # (#27145). Empty string (the schema default) means "no fallback, keep skipping" — backward-compatible
    # with existing installs.
    default_assignee = (kanban_cfg.get("default_assignee") or "").strip() or None
    if default_assignee:
        logger.info("kanban dispatcher: default_assignee=%r (unassigned ready tasks "
                    "will route to this profile)", default_assignee)

    return _DispatcherSettings(
        interval=interval,
        max_spawn=max_spawn,
        max_in_progress=effective_max_in_progress,
        failure_limit=failure_limit,
        stale_timeout_seconds=stale_timeout_seconds,
        # Requeue 'running' cards with broken claim bookkeeping (zombie-card
        # reconciliation); false keeps orphans frozen for manual forensics.
        reconcile_orphans=bool(kanban_cfg.get("reconcile_orphans", True)),
        default_assignee=default_assignee,
        # Per-profile concurrency cap: no single profile's local model / API
        # quota / browser pool gets overwhelmed by a fan-out.
        max_in_progress_per_profile=_positive_int_setting(kanban_cfg, "max_in_progress_per_profile"),
    )


class _KanbanDispatcher:
    """Per-tick board work for the embedded dispatcher (runs in worker threads).

    Boards are enumerated every tick so a board created mid-run is picked up
    without a restart. Corrupt-looking board DBs are quarantined per
    fingerprint and retried after ``CORRUPT_BOARD_RETRY_AFTER_SECONDS``:
    transient WAL/open races can look like "malformed" for one tick.
    """

    CORRUPT_BOARD_RETRY_AFTER_SECONDS = 300

    def __init__(self, kb: Any, settings: _DispatcherSettings) -> None:
        self.kb = kb
        self.settings = settings
        self.disabled_corrupt_boards: dict[str, tuple[tuple[str, int | None, int | None], float]] = {}

    def _board_slugs(self) -> list:
        from gateway.dispatch_headroom import board_sort_key
        return sorted(
            _board_slugs(self.kb),
            key=lambda b: board_sort_key(b, _board_now_count(b), _board_dispatch_priority(b)),
        )

    def board_db_fingerprint(self, slug: str) -> tuple[str, int | None, int | None]:
        path = self.kb.kanban_db_path(slug)
        try:
            resolved = str(path.expanduser().resolve())
        except Exception:
            resolved = str(path)
        try:
            stat = path.stat()
        except OSError:
            return (resolved, None, None)
        return (resolved, stat.st_mtime_ns, stat.st_size)

    def is_corrupt_board_db_error(self, exc: Exception) -> bool:
        if isinstance(exc, _kbc().KanbanDbCorruptError):
            return True
        return isinstance(exc, sqlite3.DatabaseError) and any(m in str(exc).lower() for m in _CORRUPT_DB_MARKERS)

    def _quarantine_lifted(self, slug: str, fingerprint: tuple) -> bool:
        """Return False while *slug* stays quarantined; lift (and log) otherwise."""
        disabled_entry = self.disabled_corrupt_boards.get(slug)
        if disabled_entry is None:
            return True
        disabled_fingerprint, disabled_at = disabled_entry
        age = time.monotonic() - disabled_at
        if disabled_fingerprint == fingerprint and age < self.CORRUPT_BOARD_RETRY_AFTER_SECONDS:
            return False
        if disabled_fingerprint == fingerprint:
            logger.info("kanban dispatcher: board %s database fingerprint unchanged "
                        "after %.0fs quarantine; retrying dispatch", slug, age)
        else:
            logger.info("kanban dispatcher: board %s database changed; retrying dispatch", slug)
        self.disabled_corrupt_boards.pop(slug, None)
        return True

    def tick_once_for_board(self, slug: str) -> Optional[object]:
        """Run one dispatch_once for a specific board.

        The per-board DB is opened explicitly so boards never share a
        connection or claim across each other.
        """
        if _board_no_llm_dispatch(slug):
            return None
        conn = None
        fingerprint = self.board_db_fingerprint(slug)
        if not self._quarantine_lifted(slug, fingerprint):
            return None
        kwargs = {k: v for k, v in asdict(self.settings).items() if k != "interval"}
        # Multi-dimensional dispatch headroom (config-as-code): CPU is SOFT,
        # memory/disk/swap HARD; the LLM and reviewer gates fold in. The target
        # worker count overrides the static cap for this tick (fail-open to the cap).
        try:
            _hr = _compute_dispatch_headroom(self.settings.max_in_progress)
            kwargs["max_in_progress"] = _hr.get("target_workers")
        except Exception:
            logger.debug("dispatch headroom unavailable", exc_info=True)
        try:
            # No explicit init_db(): connect() runs the migration once per
            # process (see the matching note in the notifier collector).
            conn = _kbc().connect(board=slug)
            return _kbd().dispatch_once(conn, board=slug, **kwargs)
        except Exception as exc:
            if self.is_corrupt_board_db_error(exc):
                self.disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                logger.error(
                    "kanban dispatcher: board %s database %s is not a valid "
                    "SQLite database; pausing dispatch for this board until "
                    "the file changes, the gateway restarts, or the "
                    "quarantine timer expires. Move or restore the file, "
                    "then run `hermes kanban init` if you need a fresh board.",
                    slug, fingerprint[0],
                )
                return None
            logger.exception("kanban dispatcher: tick failed on board %s", slug)
            return None
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()

    def tick_once(self) -> list[tuple[str, Optional[object]]]:
        """Run one dispatch_once per board. Returns (slug, result) pairs."""
        # Reclaim-loop breaker: strike dead-pid workers EVERY tick, deliberately
        # outside the capacity guard, so an immediately-dying worker is blocked
        # (exponential backoff, hard-block after N) instead of respawning forever.
        try:
            _strike_dead_pid_workers(self.kb, [{"slug": b} for b in self._board_slugs()])
        except Exception:
            logger.debug("dead-pid strike pass failed", exc_info=True)
        return [(slug, self.tick_once_for_board(slug)) for slug in self._board_slugs()]

    def ready_nonempty(self) -> bool:
        """Is there a ready+assigned+unclaimed task on ANY board the dispatcher would spawn for?

        Control-plane lanes (e.g. ``orion-cc``) are pulled by terminals via
        ``claim_task`` and never spawnable — a queue full of those is
        "correctly idle", not "stuck". The review column is probed only when
        review dispatch is on (same gate as the dispatcher): a task waiting
        for a human reviewer is idle, not stuck.
        """
        kbd = _kbd()
        _review_probe = kbd.review_dispatch_enabled()
        for slug in self._board_slugs():
            conn = None
            try:
                conn = _kbc().connect(board=slug)
                if kbd.has_spawnable_ready(conn) or (_review_probe and kbd.has_spawnable_review(conn)):
                    return True
            except Exception:
                continue
            finally:
                if conn is not None:
                    with contextlib.suppress(Exception):
                        conn.close()
        return False

    def auto_decompose_tick(self, auto_decompose_per_tick: int) -> int:
        """Auto-decompose up to N triage tasks across all boards into ready workgraphs.

        Runs before dispatch fans out; the per-tick cap keeps a bulk triage
        load from burst-spending the aux LLM. Returns the number decomposed.
        """
        try:
            from hermes_cli import kanban_decompose as _decomp
        except Exception as exc:  # pragma: no cover
            logger.warning("kanban auto-decompose: import failed (%s); skipping", exc)
            return 0
        attempted = 0
        successes = 0
        with _default_profile_secret_scope():
            for slug in self._board_slugs():
                if attempted >= auto_decompose_per_tick:
                    break
                # Pin the board via env for the call: the decomposer connects
                # with no board kwarg (same pattern as the dashboard specify endpoint).
                prev_env = os.environ.get("HERMES_KANBAN_BOARD")
                try:
                    os.environ["HERMES_KANBAN_BOARD"] = slug
                    try:
                        triage_ids = _decomp.list_triage_ids()
                    except Exception as exc:
                        logger.debug("kanban auto-decompose: list_triage_ids failed on board %s (%s)", slug, exc)
                        triage_ids = []
                    for tid in triage_ids:
                        if attempted >= auto_decompose_per_tick:
                            break
                        attempted += 1
                        successes += self._decompose_one(_decomp, slug, tid)
                finally:
                    if prev_env is None:
                        os.environ.pop("HERMES_KANBAN_BOARD", None)
                    else:
                        os.environ["HERMES_KANBAN_BOARD"] = prev_env
        return successes

    @staticmethod
    def _decompose_one(_decomp: Any, slug: str, tid: str) -> int:
        """Decompose one triage task; returns 1 on success, 0 otherwise."""
        try:
            outcome = _decomp.decompose_task(tid, author="auto-decomposer")
        except Exception:
            logger.exception("kanban auto-decompose: decompose_task crashed on %s", tid)
            return 0
        if not outcome.ok:
            # Common no-op reasons (no aux client) must not spam logs every tick.
            logger.debug("kanban auto-decompose [%s]: %s skipped: %s", slug, tid, outcome.reason)
            return 0
        if outcome.fanout and outcome.child_ids:
            logger.info("kanban auto-decompose [%s]: %s → %d children", slug, tid, len(outcome.child_ids))
        else:
            logger.info("kanban auto-decompose [%s]: %s → single task (no fanout)", slug, tid)
        return 1


@contextlib.contextmanager
def _default_profile_secret_scope():
    """Install the gateway launch profile's secret scope while multiplexing is on.

    The tick runs via ``_to_thread_process_service`` in a fresh context, so no
    per-turn scope exists and ``get_secret`` fails closed. The decomposer's aux
    LLM reads ``auxiliary.*`` from ``get_hermes_home()``, so its credentials come
    from that same home. No-op for single-profile gateways.
    """
    from agent.secret_scope import (
        build_profile_secret_scope, is_multiplex_active, reset_secret_scope, set_secret_scope)
    from hermes_constants import get_hermes_home

    if not is_multiplex_active():
        yield
        return
    token = set_secret_scope(
        build_profile_secret_scope(Path(get_hermes_home())), profile_home=str(get_hermes_home()))
    try:
        yield
    finally:
        reset_secret_scope(token)


def _log_spawn_results(results: Optional[list]) -> bool:
    """Log per-board spawn summaries; returns whether any board spawned."""
    any_spawned = False
    for slug, res in (results or []):
        if res is not None and getattr(res, "spawned", None):
            any_spawned = True
            # Quiet by default: an idle gateway stays silent.
            logger.info(
                "kanban dispatcher [%s]: spawned=%d reclaimed=%d "
                "crashed=%d timed_out=%d promoted=%d auto_blocked=%d",
                slug, len(res.spawned), res.reclaimed,
                len(res.crashed) if hasattr(res.crashed, "__len__") else 0,
                len(res.timed_out) if hasattr(res.timed_out, "__len__") else 0,
                res.promoted,
                len(res.auto_blocked) if hasattr(res.auto_blocked, "__len__") else 0,
            )
    return any_spawned


def _compute_dispatch_headroom(
    static_cap: "Optional[int]" = None,
) -> dict:
    """Compute multi-dimensional dispatch headroom in-process.

    Combines the resource sample (cpu/memory/swap/disk) with the LLM
    price/quota gate (``/v1/dispatch_gate`` on the live router) into a single
    ``{target_workers, per_dimension, reason, can_dispatch}`` decision.

    Policy — thresholds, which dimensions are HARD-critical, and the soft floor
    — is config-as-code (``gateway.dispatch_headroom`` /
    ``state/fleet/dispatch_headroom.yaml``, deployed by role
    59-dispatch-health). CPU is SOFT: a CPU breach throttles but still yields
    ``soft_floor`` workers, so a busy-but-not-dangerous box cannot starve every
    board; only memory/disk/swap breach can hard-zero dispatch. ``reason`` names
    the binding dimension so a resource hold is never misreported as a quota
    hold (the pre-2026-09-28 bug that made a CPU hold read as "primary keys
    tight").

    Fail-open: any sensor error degrades to a safe floor (1 worker) so a broken
    sensor can never wedge dispatch (same contract as the estop gate).
    """
    import json as _json
    import urllib.request as _urllib

    from gateway.dispatch_headroom import (
        fold_target,
        load_policy,
        resource_headroom,
    )

    policy = load_policy()
    cores = os.cpu_count() or 1

    # ── 1. Resource sample (latest raw row drives the gate) ──
    raw = {
        "cpu_load": 0.0,
        "memory_pct": 0.0,
        "swap_used_pct": 0.0,
        "disk_used_pct": 0.0,
    }
    try:
        import sqlite3 as _sqlite3
        _c = _sqlite3.connect(
            f"file:{Path.home()/'.hermes'/'bot'/'zai_usage.db'}?mode=ro",
            uri=True, timeout=5,
        )
        _row = _c.execute(
            "SELECT cpu_load_1m, memory_used_percent, swap_used_percent, "
            "disk_used_percent FROM resource_metrics ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        _c.close()
        if _row:
            raw = {
                "cpu_load": float(_row[0] or 0.0),
                "memory_pct": float(_row[1] or 0.0),
                "swap_used_pct": float(_row[2] or 0.0),
                "disk_used_pct": float(_row[3] or 0.0),
            }
    except Exception:
        pass

    # Kalman early-warning: which dimensions are trending toward a breach.
    kalman_warn = set()
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path.home() / ".hermes" / "bot"))
        from multi_resource_kalman import (
            MultiResourceKalmanPredictor,
            get_resource_history,
        )
        history = get_resource_history(hours=2)
        if len(history) >= 3:
            pred = MultiResourceKalmanPredictor()
            for h in history:
                pred.update({k: h.get(k, 0.0) for k in pred.RESOURCES})
            for w in pred.get_resource_warnings(minutes_ahead=30):
                kalman_warn.add(w.get("resource", "").lower())
    except Exception as exc:
        logger.warning("kanban dispatcher: resource Kalman unavailable (%s)", exc)

    per_dim = resource_headroom(raw, policy, cores, kalman_warn)

    # ── 2. LLM price/quota gate (market-based live router) ──
    llm_headroom = 1.0
    llm_reason = ""
    try:
        req = _urllib.Request(
            "http://localhost:9099/v1/dispatch_gate?estimated_tokens=200000&task_type=coding",
            headers={"Accept": "application/json"},
        )
        with _urllib.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        llm_headroom = 1.0 if data.get("can_dispatch", True) else 0.0
        llm_reason = data.get("reason", "") or ""
    except Exception as exc:
        logger.warning("kanban dispatcher: LLM dispatch_gate unavailable (%s)", exc)
    per_dim["llm"] = llm_headroom

    # ── 2b. Reviewer-aware gate (2026-09-30) ──
    # Hold dispatch when NO cross-family reviewer lane is live, so we do not
    # spawn workers whose DoD requires a cold review that cannot be obtained
    # (the qwen3.5:397b "no lane" class). Fail-open on any sensor error.
    try:
        from gateway.dispatch_headroom import reviewer_headroom
        per_dim["reviewer"] = reviewer_headroom(policy)
    except Exception:
        per_dim["reviewer"] = 1.0

    # ── 3. Fold to a target worker count (throttle, not binary) ──
    critical_dims = list(policy.get("critical_dimensions") or (
        "memory_pct", "disk_used_pct", "swap_used_pct",
    ))
    # reviewer is HARD-critical only when we are sure no lane exists; a healthy
    # probe leaves it as an ordinary (1.0) dimension with no effect.
    if per_dim.get("reviewer", 1.0) <= 0.0:
        critical_dims.append("reviewer")
    soft_floor = int(policy.get("soft_floor", 1))
    target = fold_target(per_dim, critical_dims, static_cap, soft_floor)

    # ── 4. Reason: name the binding dimension; never mask a resource hold with
    #       the LLM gate's text (the 2026-09-28 misreport). ──
    if per_dim:
        binding = min(per_dim, key=per_dim.get)
        if binding == "reviewer" and per_dim.get("reviewer", 1.0) <= 0.0:
            reason = "reviewer lane unavailable: no cross-family reviewer served"
            if llm_reason:
                reason += f"; llm: {llm_reason}"
        elif per_dim[binding] < 1.0 and binding != "llm":
            reason = (
                f"resource throttle: {binding} headroom {per_dim[binding]:.1f} "
                f"(cpu_load={raw.get('cpu_load', 0.0):.1f} on {cores}c)"
            )
            if llm_reason:
                reason += f"; llm: {llm_reason}"
        elif binding == "llm" and per_dim["llm"] <= 0.0:
            reason = llm_reason or "llm lane exhausted"
        else:
            reason = llm_reason or "ok"
    else:
        reason = "no sensors"

    result = {
        "target_workers": target,
        "can_dispatch": target > 0,
        "per_dimension": per_dim,
        "reason": reason,
    }
    # Persist so the task-lifecycle governor (Phase 7) reads the SAME headroom
    # signal and never revives into a starved box. Fail-open: a write error
    # must not affect the dispatch decision.
    try:
        _p = Path.home() / ".hermes" / "bot" / "dispatch_headroom.json"
        _p.parent.mkdir(parents=True, exist_ok=True)
        _p.write_text(_json.dumps(result), encoding="utf-8")
    except Exception:
        pass
    return result


def _board_no_llm_dispatch(slug: str) -> bool:
    """True when a board opts out of LLM worker dispatch (board.json ``no_llm_dispatch``)."""
    try:
        import json as _json
        from hermes_constants import get_hermes_home
        bj = get_hermes_home() / "kanban" / "boards" / slug / "board.json"
        return bool((_json.loads(bj.read_text(encoding="utf-8")) or {}).get("no_llm_dispatch"))
    except Exception:
        return False


def _board_dispatch_priority(slug: str) -> int:
    """Board scheduling priority from board.json ``dispatch_priority`` (higher first)."""
    try:
        import json as _json
        from hermes_constants import get_hermes_home
        bj = get_hermes_home() / "kanban" / "boards" / slug / "board.json"
        return int((_json.loads(bj.read_text(encoding="utf-8")) or {}).get("dispatch_priority") or 0)
    except Exception:
        return 0


def _board_now_count(slug: str) -> int:
    """Count of ready+'now' tasks on a board (priority tiebreak input; fail-open 0)."""
    conn = None
    try:
        conn = _kbc().connect(board=slug)
        return int(conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='ready' AND urgency='now'"
        ).fetchone()[0])
    except Exception:
        return 0
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()


RECLAIM_BACKOFF_BASE_S = 300          # 5 minutes
RECLAIM_BACKOFF_MAX_S = 3600          # 1 hour ceiling
RECLAIM_BLOCK_AFTER = 3               # dead-pid reclaims before a hard block

def _reclaim_backoff_path() -> Path:
    """Profile-safe path for the strike/backoff ledger (resolved per call)."""
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "bot" / "reclaim_backoff.json"


def _load_reclaim_backoff() -> dict:
    try:
        data = json.loads(_reclaim_backoff_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_reclaim_backoff(data: dict) -> None:
    try:
        path = _reclaim_backoff_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _reclaim_backoff_seconds(strikes: int) -> int:
    """Exponential backoff for the Nth dead-pid reclaim (5m, 10m, 20m, ...)."""
    return min(RECLAIM_BACKOFF_BASE_S * (2 ** max(strikes - 1, 0)),
               RECLAIM_BACKOFF_MAX_S)


def _strike_dead_pid_workers(kb, boards, *, pid_alive=None, now_fn=None,
                             grace_seconds=None) -> list[str]:
    """Strike dead-pid workers: backoff-block them, hard-block after N strikes.

    Runs on **every** dispatcher tick, deliberately *outside* the
    ``running >= target`` deadlock guard. The pass originally lived inside that
    guard (commit ``10bbb0a8d8``), which left the breaker inert exactly when it
    was needed: with the fleet below its capacity target the guard never opened,
    so dead workers fell through to the per-board ``dispatch_once`` ->
    ``detect_crashed_workers`` path, which re-readies the card as ``crashed``
    and (by design) clears ``consecutive_failures``. A card whose worker dies
    immediately was therefore re-spawned every tick forever and never accrued a
    single strike — and because the failure counter stayed clean, no other
    breaker saw it either (``fleet_loop_guard`` reported "no looping cards"
    while the digest showed the card burning worker slots).

    Two guards, both mirroring ``kanban_db.detect_crashed_workers``, keep the
    now-per-tick pass from striking *healthy* workers:

    * **host-local claims only** — ``worker_pid`` from another host's claim is
      meaningless here, because ``_pid_alive`` is a local probe;
    * **launch-window grace** — ``/proc`` visibility can transiently report a
      freshly forked worker as dead. Measured from ``tasks.started_at`` (the
      first time the task ever started), exactly as
      ``detect_crashed_workers`` does; measuring from the *current run* would
      reset the grace on every re-spawn and neuter the breaker for the
      fast-crash loop it exists to stop.

    Returns the ids it acted on (struck or hard-blocked). Fail-open per board:
    a board that raises is skipped, never fatal to the tick.
    """
    acted: list[str] = []
    if not boards:
        return acted
    alive_fn = pid_alive or getattr(kb, "_pid_alive", None)
    if alive_fn is None:
        return acted
    if now_fn is None:
        now_fn = time.time
    if grace_seconds is None:
        resolve_grace = getattr(kb, "_resolve_crash_grace_seconds", None)
        try:
            grace_seconds = (
                int(resolve_grace()) if resolve_grace is not None
                else int(getattr(kb, "DEFAULT_CRASH_GRACE_SECONDS", 30))
            )
        except Exception:
            grace_seconds = 30
    try:
        host_prefix = f"{kb._claimer_id().split(':', 1)[0]}:"
    except Exception:
        host_prefix = ""
    now = now_fn()
    ledger = _load_reclaim_backoff()
    dirty = False
    for board in boards:
        slug = (board or {}).get("slug") or kb.DEFAULT_BOARD
        conn = None
        try:
            conn = kb.connect(board=slug)
            rows = conn.execute(
                "SELECT id, worker_pid, claim_lock, started_at "
                "FROM tasks WHERE status='running'"
            ).fetchall()
            for row in rows:
                tid, wpid = row[0], row[1]
                lock = row[2] or ""
                started = row[3]
                if not wpid:
                    continue
                if host_prefix and not lock.startswith(host_prefix):
                    continue
                if (started is not None and grace_seconds > 0
                        and now - int(started) < grace_seconds):
                    continue
                if alive_fn(wpid):
                    continue
                entry = ledger.get(tid) or {}
                strikes = int(entry.get("count", 0)) + 1
                if strikes >= RECLAIM_BLOCK_AFTER:
                    kb.block_task(
                        conn, tid,
                        reason=(f"reclaim loop: worker died {strikes}x "
                                f"(dispatcher guard) — auto-blocked; "
                                f"needs human"),
                    )
                    ledger.pop(tid, None)
                    logger.warning(
                        "kanban reclaim-loop breaker: hard-blocked %s "
                        "(worker died %dx)", tid, strikes)
                else:
                    until = now + _reclaim_backoff_seconds(strikes)
                    kb.block_task(
                        conn, tid,
                        reason=(f"reclaim backoff until {int(until)} "
                                f"(dead worker pid, strike {strikes})"),
                    )
                    ledger[tid] = {"count": strikes, "until": until,
                                   "board": slug}
                acted.append(tid)
                dirty = True
        except Exception:
            continue
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    if dirty:
        _save_reclaim_backoff(ledger)
    return acted
