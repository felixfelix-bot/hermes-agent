# ADR: Kanban dispatch transparency for unassigned cards

- **Status:** Proposed
- **Date:** 2026-08-17
- **Scope:** `hermes_cli/kanban.py` (CLI create path), `hermes_cli/kanban_db.py` (`create_task`)
- **Incident:** plebeian-adr, 2026-08-16 (four manager-gate cards auto-assigned ~53s after creation)

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

## Decision

Three candidate fixes were considered (handover doc
`~/plans/hermes-fix-handover/HANDOVER-kanban-assignee-default.md`, task
`t_95a2ea0d`). This changeset ships A and B; C is follow-up work.

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

### Fix C — dispatcher session clamp (NOT in this changeset)

Clamping concurrent sessions per profile in `dispatch_once` is a
dispatcher-loop change with its own test matrix (spawner behavior,
`max_in_progress` interactions, reclaim semantics). It is tracked as
follow-up work; this ADR deliberately does not bundle it.

## Consequences

- Operators get the routing signal at the moment they can still act (create
  time) instead of after a worker has spawned.
- The "unassigned = idle" and "unassigned = fallback-routed" semantics are
  now legible per-installation at creation time, without changing
  dispatcher behavior for existing boards.
- `--json` output is unchanged — machine consumers are not broken.
- The event trail now distinguishes held cards (`reason: "held"`) from
  generic initial-status parks, which makes hold/release audits possible.
- The zh-Hans translation of the user guide does not yet carry the new
  `default_assignee` row text or `--hold` flag; translation sync happens
  with the next docs pass.

## Alternatives considered

- **Remove the dispatcher fallback entirely** — rejected: the fallback
  fixes a real problem (#27145: decomposer children with unknown profiles
  would sit `assignee=None` forever). Removing it trades one surprise for
  a worse one.
- **Warn from the dispatcher log only** — rejected: the dispatcher log is
  the right audit trail but the wrong place for a decision signal; the
  operator is not tailing it at create time (the incident proves exactly
  that).
- **Make the CLI refuse to create unassigned ready cards when
  `default_assignee` is set** — rejected: too aggressive; scripted callers
  (and `--json` consumers) rely on create being non-interactive. A warning
  plus a first-class hold flag preserves automation.

## References

- Incident writeup + handover: `~/plans/hermes-fix-handover/HANDOVER-kanban-assignee-default.md` (private host)
- Sticky-block emission for `initial_status="blocked"`: commit `8de7012383`
- Dispatcher fallback introduction: #27145 (`kanban.default_assignee`)
- Tests: `tests/hermes_cli/test_kanban_create_default_assignee_warning.py`
