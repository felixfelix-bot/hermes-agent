# Kanban Dashboard Plugin

A multi-agent collaboration board for the Hermes dashboard. Visualises tasks as
drag-and-drop cards across 8 status columns, with live WebSocket updates,
inline comments, task links, diagnostics, and orchestration controls.

## What It Does

The Kanban plugin turns Hermes's task store into a visual board:

- **8 columns** — triage → todo → scheduled → ready → running → blocked →
  review → done (plus an archived lane).
- **Drag-and-drop** — move task cards between columns to change status.
- **Live updates** — a WebSocket stream pushes task events in real time.
- **Task detail drawer** — read comments, events, runs, attachments, links,
  and worker logs.
- **Multi-board** — tasks can be partitioned into named boards (default,
  sprint-1, etc.) with a board switcher persisted to localStorage.
- **Diagnostics** — surface suspected hallucinations, blocked completions,
  and suggested recovery actions (reclaim, reassign, specify, decompose).
- **Orchestration** — configure the orchestrator profile, default assignee,
  and auto-decompose settings.

## Architecture

The plugin is built as a **self-contained IIFE bundle**. It ships no React
runtime — instead it receives React, UI components, and utility functions from
the host dashboard at runtime via `window.__HERMES_PLUGIN_SDK__`.

```
┌─────────────────────────────────────────────────────┐
│  Host Dashboard (web app)                            │
│                                                       │
│  ┌──────────────┐     ┌───────────────────────────┐  │
│  │ Plugin Loader │────▶│ window.__HERMES_PLUGIN_   │  │
│  │ (reads        │     │   SDK__ = { React, hooks,  │  │
│  │  manifest.json)    │   fetchJSON, components,    │  │
│  └──────────────┘     │   buildWsUrl, utils, ... }  │  │
│         │              └───────────────────────────┘  │
│         ▼                        ▲                     │
│  ┌──────────────┐                │                     │
│  │ <script>     │────────────────┘                     │
│  │ dist/index.js│ (IIFE reads SDK from window at       │
│  │ (78 KB IIFE) │  runtime, not from bundle)            │
│  └──────────────┘                                      │
│         │                                               │
│         ▼ calls                                         │
│  window.__HERMES_PLUGINS__.register("kanban", KanbanPage)
└─────────────────────────────────────────────────────────┘
         │
         ▼ HTTP / WebSocket
┌─────────────────────────────────────────────────────┐
│  Backend (plugin_api.py)                             │
│  FastAPI router mounted at /api/plugins/kanban/      │
│  35 endpoints + 1 WebSocket                         │
└─────────────────────────────────────────────────────┘
```

### How the Plugin Loads

1. The host dashboard reads `manifest.json` to discover the plugin.
2. It exposes `window.__HERMES_PLUGIN_SDK__` (React, hooks, components,
   `fetchJSON`, `buildWsUrl`, utils) and `window.__HERMES_PLUGINS__` (the
   registry) on the global scope.
3. It injects `<script src="dist/index.js">` — the IIFE bundle.
4. The IIFE's entry point (`src/index.ts`) imports `KanbanPage` and calls
   `window.__HERMES_PLUGINS__.register("kanban", KanbanPage)`.
5. The host renders `KanbanPage` inside a tab at `/kanban`.

## File Structure

| File | Description |
|---|---|
| `src/index.ts` | IIFE entry point — registers `KanbanPage` with the host plugin registry. |
| `src/constants.ts` | API path, column order, MIME type, staleness thresholds, CSS class maps. |
| `src/types.ts` | TypeScript interfaces for all API response shapes and request bodies. |
| `src/sdk.ts` | SDK access layer — wraps `window.__HERMES_PLUGIN_SDK__`; contains `withBoard()`, `readSelectedBoard()`, `writeSelectedBoard()`, `selectChangeHandler()`. |
| `src/api.ts` | API client — wraps all `/api/plugins/kanban/*` endpoints using SDK `fetchJSON`. |
| `src/useKanbanEvents.ts` | WebSocket hook — live event streaming with debounce + exponential backoff reconnect. |
| `src/KanbanPage.tsx` | Main page component — board state, data fetching, layout orchestration. |
| `src/board-ui.tsx` | Board-level UI — columns, drag-and-drop handlers, filter bar, board switcher. |
| `src/components.tsx` | Reusable card/column components — task cards, status dots, badges. |
| `src/drawer.tsx` | Task detail drawer — comments, events, runs, attachments, diagnostics, recovery actions. |
| `src/ErrorBoundary.tsx` | React error boundary wrapper (class component, uses SDK React at runtime). |
| `src/i18n.ts` | English fallback strings for column labels, actions, and diagnostic messages. |
| `src/kanban.css` | Plugin styles (compiled to `dist/style.css`). |
| `manifest.json` | Plugin manifest — name, label, icon, tab route, entry/css paths. |
| `plugin_api.py` | Python FastAPI backend — 35 REST endpoints + 1 WebSocket under `/api/plugins/kanban/`. |
| `vite.config.ts` | Vite build config — IIFE output, React/UI components externalised. |
| `vitest.config.ts` | Vitest test config — node environment, `src/**/*.test.{ts,tsx}`. |

## Build

```bash
cd plugins/kanban/dashboard
npx vite build
```

This compiles `src/index.ts` → `dist/index.js` (IIFE, ~78 KB) and
`src/kanban.css` → `dist/style.css`. React and UI components are marked
external — they are never bundled because they arrive from the host SDK at
runtime.

## Test

```bash
cd plugins/kanban/dashboard
npx vitest run
```

Tests use the `node` environment (no jsdom/DOM required). They cover pure
functions only — constant values, URL construction, localStorage helpers, and
request shapes. React components are not tested here.

## API Endpoints

All endpoints are mounted under `/api/plugins/kanban/`.

### Board

| Method | Path | Description |
|---|---|---|
| GET | `/board` | Fetch the kanban board (columns, tasks, tenants, assignees). |
| GET | `/boards` | List all boards with task counts. |
| POST | `/boards` | Create a new board. |
| PATCH | `/boards/{slug}` | Update board metadata. |
| DELETE | `/boards/{slug}` | Archive or hard-delete a board. |
| POST | `/boards/{slug}/switch` | Switch the active board. |

### Tasks

| Method | Path | Description |
|---|---|---|
| GET | `/tasks/{task_id}` | Fetch full task detail (comments, events, runs, links, attachments). |
| POST | `/tasks` | Create a new task. |
| PATCH | `/tasks/{task_id}` | Update task fields (status, assignee, priority, title, body, result). |
| DELETE | `/tasks/{task_id}` | Delete a task. |
| POST | `/tasks/bulk` | Bulk update multiple tasks. |
| GET | `/tasks/{task_id}/log` | Fetch the worker log for a task. |

### Task Actions

| Method | Path | Description |
|---|---|---|
| POST | `/tasks/{task_id}/comments` | Add a comment to a task. |
| POST | `/tasks/{task_id}/reclaim` | Reclaim a stuck task. |
| POST | `/tasks/{task_id}/specify` | AI-clarify an under-specified task. |
| POST | `/tasks/{task_id}/decompose` | Break a task into sub-tasks. |
| POST | `/tasks/{task_id}/reassign` | Reassign a task to a different profile. |

### Attachments

| Method | Path | Description |
|---|---|---|
| GET | `/tasks/{task_id}/attachments` | List attachments for a task. |
| POST | `/tasks/{task_id}/attachments` | Upload an attachment. |
| GET | `/attachments/{attachment_id}` | Download an attachment. |
| DELETE | `/attachments/{attachment_id}` | Delete an attachment. |

### Links

| Method | Path | Description |
|---|---|---|
| POST | `/links` | Create a parent→child task link. |
| DELETE | `/links` | Remove a task link. |

### Home Channels

| Method | Path | Description |
|---|---|---|
| GET | `/home-channels` | List home channels for a task. |
| POST | `/tasks/{task_id}/home-subscribe/{platform}` | Subscribe a task to a home channel. |
| DELETE | `/tasks/{task_id}/home-subscribe/{platform}` | Unsubscribe from a home channel. |

### Diagnostics & Workers

| Method | Path | Description |
|---|---|---|
| GET | `/diagnostics` | List active diagnostics across tasks. |
| GET | `/workers/active` | List currently active workers. |

### Runs

| Method | Path | Description |
|---|---|---|
| GET | `/runs/{run_id}` | Fetch details for a specific run. |
| GET | `/runs/{run_id}/inspect` | Inspect a run (full metadata). |
| POST | `/runs/{run_id}/terminate` | Terminate a running task. |

### Config, Stats & Orchestration

| Method | Path | Description |
|---|---|---|
| GET | `/config` | Fetch dashboard configuration. |
| GET | `/stats` | Fetch board statistics. |
| GET | `/assignees` | List all assignees with task counts. |
| POST | `/dispatch` | Dispatch a nudge batch to ready tasks. |
| GET | `/orchestration` | Fetch orchestration settings. |
| PUT | `/orchestration` | Update orchestration settings. |

### Profiles

| Method | Path | Description |
|---|---|---|
| GET | `/profiles` | List all profiles with descriptions. |
| PATCH | `/profiles/{name}` | Update a profile's description. |
| POST | `/profiles/{name}/describe-auto` | Auto-generate a profile description. |

### WebSocket

| Path | Description |
|---|---|
| `/events` | Live task event stream (params: `since` cursor, `board` slug). |

## Board Scoping

Most endpoints accept an optional `?board=<slug>` query parameter (appended by
the `withBoard()` helper in `sdk.ts`). When set, operations are scoped to that
board. The special value `"default"` is always sent explicitly (not omitted).
