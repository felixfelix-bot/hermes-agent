/**
 * Kanban dashboard plugin — constants.
 */

import type { TaskStatus } from "./types";

/** Base API path for all kanban endpoints. */
export const API = "/api/plugins/kanban";

/** MIME type used for HTML5 drag-and-drop of task cards. */
export const MIME_TASK = "text/x-hermes-task";

/** Column order — matches BOARD_COLUMNS in plugin_api.py (8 columns). */
export const COLUMN_ORDER: TaskStatus[] = [
  "triage",
  "todo",
  "scheduled",
  "ready",
  "running",
  "blocked",
  "review",
  "done",
];

/** localStorage key for the user's selected board. */
export const LS_BOARD_KEY = "hermes.kanban.selectedBoard";

/** Documentation links. */
export const DOCS_URL =
  "https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban";
export const DOCS_TUTORIAL_URL =
  "https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban-tutorial";

/** CSS class names for column status dots. */
export const COLUMN_DOT: Record<string, string> = {
  triage: "hermes-kanban-dot-triage",
  todo: "hermes-kanban-dot-todo",
  ready: "hermes-kanban-dot-ready",
  running: "hermes-kanban-dot-running",
  blocked: "hermes-kanban-dot-blocked",
  done: "hermes-kanban-dot-done",
  archived: "hermes-kanban-dot-archived",
};

/** Staleness tiers in seconds. Amber after grace window, red when clearly stuck. */
export const STALENESS: Record<string, { amber: number; red: number }> = {
  ready: { amber: 1 * 60 * 60, red: 24 * 60 * 60 },
  running: { amber: 10 * 60, red: 60 * 60 },
  blocked: { amber: 1 * 60 * 60, red: 24 * 60 * 60 },
  todo: { amber: 7 * 24 * 60 * 60, red: 30 * 24 * 60 * 60 },
};

/** Diagnostic event kind → i18n key mapping. */
export const DIAGNOSTIC_EVENT_KIND_KEYS: Record<string, string> = {
  completion_blocked_hallucination: "completionBlockedHallucination",
  suspected_hallucinated_references: "suspectedHallucinatedReferences",
};

/** Destructive status → i18n key mapping. */
export const DESTRUCTIVE_KEYS: Record<string, string> = {
  done: "confirmDone",
  archived: "confirmArchive",
  blocked: "confirmBlocked",
};