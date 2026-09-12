# ADR: Kanban dispatch transparency for unassigned cards

- **Status:** Proposed
- **Date:** 2026-08-17 (Fixes A+B); 2026-09-12 (Fixes C+D)
- **Scope:** `hermes_cli/kanban.py` (CLI create / dispatch / show paths),
  `hermes_cli/kanban_db.py` (`create_task`, `dispatch_once`),
  `gateway/kanban_watchers.py` (dispatcher boot log),
  `hermes_cli/kanban_diagnostics.py` (diagnostic rules)
- **Incident:** plebeian-adr, 2026-08-16 (four manager-gate cards auto-assigned ~53s after creation)
- **Branches:** `fix/kanban-dispatch-transparency` — Fixes A+B (task `t_95a2ea0d`);
  `fix/kanban-dispatch-session-clamp` — Fixes C+D (task `t_8a9dcabc`, stacked on it)

## Context

`kanban.default_assignee` (#27145) exists so decomposer children never land
`assignee=None` when the LLM picks an unknown profile. But the dispatcher's
fallback is applied to **any unassigned `ready` card** on the board —
including cards a human created with the CLI deliberately unassigned:

> Four manager-gate cards were created (`hermes kanban create`, no
> `--assignee`) intending "unassigned = won't dispatch until I route it."
> ~53 seconds later the dispatcher's `default_assignee=worker-base`
> re-assigned all four and spawned four workers into a profile with
> `max_in_progress = 1`, each summarizing an empty card three times.

The routing decision was correct per config; the failure was **opacity**.
The operator had no signal at creation time that "unassigned" had been
silently redefined by their own config to mean "route to worker-base on the
next tick."

Relevant existing behavior:

- `_cmd_create` already warns when a **ready+assigned** card will sit idle
  because no dispatcher is present (`_check_dispatcher_presence`). The
  complementary case — ready+**un**assigned — was silent.
- `create_task(initial_status="blocked")` parks a card sticky-blocked since
  8de7012383 (the `blocked` event keeps `_has_sticky_block` set, so
  `recompute_ready` never auto-promotes it). So a correct hold mechanism
  already existed at the DB layer; only its CLI ergonomics were missing.
- The gateway dispatcher reads `kanban.default_assignee` from
  profile-scoped config at boot (`gateway/kanban_watchers.py`), with the
  normalization `(cfg.get("default_assignee") or "").strip() or None`.
  The gateway reads exactly ONE file — `<HERMES_HOME>/config.yaml`, i.e.
  `profiles/<profile>/config.yaml` under a profile gateway — and never merges
  the root `~/.hermes/config.yaml`. In this incident the root file said
  `worker-tollgate` while the live manager-profile file said `worker-base`;
  nothing in the logs named the winner, and that precedence trap is the reason
  four workers were aimed at a one-slot profile.
- The dispatcher's per-profile accounting (#21582) consulted only the
  dispatcher-side `kanban.max_in_progress_per_profile`. The TARGET profile's own
  `max_concurrent_sessions` (the ceiling the worker process itself enforces at
  startup: `hermes_cli/active_sessions.py`, "Hermes is at the active session
  limit (1/1)") was invisible to it, so the fan-out could exceed it freely.

## Decision

Four candidate fixes were considered (handover doc
`~/plans/hermes-fix-handover/HANDOVER-kanban-assignee-default.md`, tasks
`t_95a2ea0d` and `t_8a9dcabc`). Fixes A+B shipped first on
`fix/kanban-dispatch-transparency`; Fixes C+D ship on the stacked branch
`fix/kanban-dispatch-session-clamp`. Fix E is deliberately rejected (see
Alternatives) — it would delete the #27145 feature rather than secure it.

### Fix A — create-time warning (shipped, `fix(kanban)`)

`hermes kanban create` now prints, **on stderr only** (stdout and `--json`
stay machine-parseable):

- When the created card is `ready`, unassigned, and
  `kanban.default_assignee` **is set**: a warning naming the fallback
  profile and tick interval, recommending `--hold` for gate-style cards,
  `--assignee` for deliberate routing, or unsetting the config.
- When the card is `ready`, unassigned, and `default_assignee` is **unset**:
  a short note that the card will NOT be dispatched, with the
  `assign`/`claim` commands that make it dispatchable.

The config is resolved through the same `load_config()` + `.strip()`
normalization the dispatcher uses, so the CLI and dispatcher agree on the
value the operator sees applied. Resolution is fail-open: any config error
degrades to the idle note; card creation is never blocked by the warning
path.

Warnings fire only for `status=ready` + `assignee is None` — triage, todo,
blocked, and assigned cards keep their existing semantics (blocked cards
are outside the dispatch loop by construction; assigned cards are never
re-routed by `default_assignee`).

### Fix B — `--hold` alias (shipped, `feat(kanban)`)

`hermes kanban create --hold` is the intent-revealing alias for
`--initial-status blocked`: the card lands sticky-blocked (never
dispatched, never auto-assigned by `default_assignee`, never
auto-promoted) until an explicit `hermes kanban unblock <id>`.

- The sticky `blocked` event records reason `"held"`, so the event trail
  distinguishes a deliberate hold from a generic initial-status park.
  `create_task` gained a `blocked_reason` parameter for this; its default
  (`None` → reason `"initial-status"`) preserves existing callers
  (`kanban_swarm`, dashboard API) unchanged.
- `--hold` combined with an explicit non-blocked `--initial-status` is a
  usage error (exit 2). To support that distinction, argparse's
  `--initial-status` default changed from `"running"` to `None`; an absent
  flag and an explicit `--initial-status running` are now distinguishable
  in `_cmd_create` (both still create a normal `ready` card).

### Fix C — dispatcher session clamp (shipped, `fix(kanban)`)

`dispatch_once` now computes the effective per-assignee ceiling as

```
min(kanban.max_in_progress_per_profile,   # dispatcher-side cap (#21582)
    <assignee> max_concurrent_sessions)   # the TARGET profile's own cap
```

`resolve_profile_session_cap()` resolves the target profile's directory with
`hermes_cli.profiles.get_profile_dir()` and delegates key semantics to the
gateway's own `hermes_cli.active_sessions.resolve_max_concurrent_sessions()`
(top-level `max_concurrent_sessions` with `gateway.*` fallback). `null`, `0`,
or an absent key all mean UNCAPPED — never 0; a missing or unparsable
`profiles/<assignee>/config.yaml` also means uncapped. The read is fail-open
and cached per assignee for the tick: a config problem can never wedge
dispatch. Delegating to the same resolver is deliberate — the clamp must agree
with the limit the worker process itself enforces.

Deferred cards land in the new
`DispatchResult.skipped_per_profile_session_capped` bucket
(`[(task_id, assignee, current_running), …]`) when the *profile's* cap is the
binding constraint, and keep using the pre-existing
`skipped_per_profile_capped` bucket when the dispatcher's own cap binds — so
existing #21582 telemetry, tests, and dashboards keep their meaning. Both
buckets defer rather than drop: the next tick after a slot frees picks the card
up (regression-tested).

Surfaces:

- the gateway dispatcher log line gains `session_capped=<n>`;
- a sustained clamp raises the existing stuck-queue warning with the
  remediation ("raise that profile's `max_concurrent_sessions` or lower
  `kanban.max_in_progress_per_profile`");
- `hermes kanban dispatch --json` reports `skipped_per_profile_session_capped`,
  and the human output prints
  `Deferred (<profile> at its own max_concurrent_sessions cap, N running): <id>`.

One counting change is implied: the per-assignee in-flight aggregate is now
queried on every tick (it used to run only when the dispatcher-side cap was
set), because the profile's own cap can bind with
`max_in_progress_per_profile` unset. That is one cheap `GROUP BY` per tick.

### Fix D — config-source transparency + a diagnostics rule (shipped)

Additive and read-only:

1. `gateway/kanban_watchers.py:kanban_config_source()` resolves WHICH config
   file supplied a `kanban.<key>` value, plus every other candidate that
   declares a DIFFERENT value for the same key — the "you edited the wrong
   file" list. The dispatcher boot log now names the winning path
   (`[config source: …]`) and emits a SHADOWED warning for each shadowed
   candidate that disagrees, so the §Context precedence trap is visible in
   `gateway.log` instead of costing a forensic session.
2. `kanban_diagnostics.py:_rule_unassigned_default_assignee()` (ordered last in
   `_RULES`, registered in `DIAGNOSTIC_KINDS` as `unassigned_default_assignee`):
   a `ready`, unassigned, unclaimed card on a host whose config sets
   `kanban.default_assignee` is a card the dispatcher is ABOUT to hand to a
   profile the operator did not choose. The rule warns with the remediation
   (`--initial-status blocked` / `--hold`, assign it explicitly, or unset
   `kanban.default_assignee` in the HOST profile's config — the root config is
   not merged). It deliberately stays silent for assigned, claimed, and
   non-`ready` cards: that is exactly the gap `_rule_stranded_in_ready` leaves
   open on purpose.
   `hermes kanban show` now passes the runtime config into
   `compute_task_diagnostics`, so the rule fires where an operator looks right
   after creating a card.

## Consequences

- Operators get the routing signal at the moment they can still act (create
  time) instead of after a worker has spawned.
- The "unassigned = idle" and "unassigned = fallback-routed" semantics are
  now legible per-installation at creation time, without changing
  dispatcher behavior for existing boards.
- `--json` output is unchanged — machine consumers are not broken.
- The event trail now distinguishes held cards (`reason: "held"`) from
  generic initial-status parks, which makes hold/release audits possible.
- The dispatcher can no longer fire more workers at a profile than that
  profile's own session limit admits, so "N workers aimed at a 1-slot profile"
  (the incident's amplifier) cannot form from a single misconfigured
  `default_assignee`. Cards over the ceiling are deferred, not failed, and the
  failure counter is not touched.
- `session_capped=` in the dispatcher log, the SHADOWED config warning, and the
  `unassigned_default_assignee` diagnostic make all three incident causes
  visible without a DB query.
- Bounded cost: one aggregate `GROUP BY assignee` per tick plus at most one
  `config.yaml` read per assignee per tick.
- The zh-Hans translation of the user guide does not yet carry the new
  `default_assignee` row text or `--hold` flag; translation sync happens
  with the next docs pass.

## Alternatives considered

- **Remove the dispatcher fallback entirely (= Fix E)** — rejected: the
  fallback fixes a real problem (#27145: decomposer children with unknown
  profiles would sit `assignee=None` forever) and is tested and documented.
  Removing it trades one surprise for a worse one, and violates the
  "do not destroy the feature you are securing" rule. Fix C is the systemic
  guard that makes the fallback safe to keep.
- **Warn from the dispatcher log only** — rejected: the dispatcher log is
  the right audit trail but the wrong place for a decision signal; the
  operator is not tailing it at create time (the incident proves exactly
  that).
- **Make the CLI refuse to create unassigned ready cards when
  `default_assignee` is set** — rejected: too aggressive; scripted callers
  (and `--json` consumers) rely on create being non-interactive. A warning
  plus a first-class hold flag preserves automation.
- **Enforce the session limit only worker-side (let the extra workers exit and
  retry)** — rejected: that is precisely the incident. A worker that dies on
  the session limit bounces the card back to `ready`, burns its failure
  counter, and can trip the circuit breaker — losing the card instead of
  deferring it.
- **Resolve the target profile cap through the full agent config loader** —
  rejected for Fix C: the clamp must agree with the limit the worker process
  enforces, so it reuses that resolver on a raw file read, which keeps the
  dispatcher loop free of CLI startup cost and fail-opens on any error.
- **Only clamp `default_assignee` fan-outs** — rejected: the amplifier is
  assignee-agnostic. Explicitly assigned cards hit the same wall (the incident
  spawned an explicitly assigned card too), so the clamp belongs in the
  per-profile accounting shared by every spawn path.

## References

- Incident writeup + handover: `~/plans/hermes-fix-handover/HANDOVER-kanban-assignee-default.md` (private host)
- Sticky-block emission for `initial_status="blocked"`: commit `8de7012383`
- Dispatcher fallback introduction: #27145 (`kanban.default_assignee`)
- Per-profile dispatcher cap introduction: #21582 (`kanban.max_in_progress_per_profile`)
- Tests (A+B): `tests/hermes_cli/test_kanban_create_default_assignee_warning.py`
- Tests (C): `tests/hermes_cli/test_kanban_dispatch_session_clamp.py`
- Tests (D): `tests/hermes_cli/test_kanban_default_assignee_diagnostics.py`
- Fix C+D task: `t_8a9dcabc` (branch `fix/kanban-dispatch-session-clamp`)
