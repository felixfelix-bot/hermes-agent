# Architecture Decision Records

ADRs record *why* a change was made, not just what changed. They live in
`docs/adr/`, numbered or slugged, with status lifecycle
`proposals/ → accepted/ → superseded-by-<n>/`.

| ADR | Status | One-liner |
|---|---|---|
| [kanban-dispatch-transparency](adr-kanban-dispatch-transparency.md) | Proposed | Create-time stderr warning when `kanban.default_assignee` will auto-route an unassigned ready card, plus a `--hold` alias that parks gate cards sticky-blocked (`reason: "held"`). Fixes C+D add a dispatcher clamp to the TARGET profile's `max_concurrent_sessions` (new `skipped_per_profile_session_capped` bucket, `session_capped=` in the dispatcher log), boot-time logging of WHICH config file supplied `default_assignee` (with SHADOWED warnings), and an `unassigned_default_assignee` diagnostic rule. |
