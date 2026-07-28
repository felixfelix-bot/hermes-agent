/**
 * Kanban dashboard plugin — IIFE entry point.
 *
 * This is the single entry module for the Vite IIFE build. It imports the
 * main ``KanbanPage`` component and registers it with the host dashboard's
 * plugin registry (``window.__HERMES_PLUGINS__``).
 *
 * At runtime, React + UI components arrive from the host SDK
 * (``window.__HERMES_PLUGIN_SDK__``); they are NOT bundled.
 */

import { KanbanPage } from "./KanbanPage";

// Register with the host dashboard. The host loads this script after
// exposing __HERMES_PLUGIN_SDK__ and __HERMES_PLUGINS__ on window.
if (
  typeof window !== "undefined" &&
  window.__HERMES_PLUGINS__ &&
  typeof window.__HERMES_PLUGINS__.register === "function"
) {
  window.__HERMES_PLUGINS__.register("kanban", KanbanPage);
}
