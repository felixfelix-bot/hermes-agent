# Architecture Decision Records

ADRs record *why* a change was made, not just what changed. They live in
`docs/adr/`, numbered or slugged, with status lifecycle
`proposals/ → accepted/ → superseded-by-<n>/`.

| ADR | Status | One-liner |
|---|---|---|
| [kanban-dispatch-transparency](adr-kanban-dispatch-transparency.md) | Proposed | Create-time stderr warning when `kanban.default_assignee` will auto-route an unassigned ready card, plus a `--hold` alias that parks gate cards sticky-blocked (`reason: "held"`). |
