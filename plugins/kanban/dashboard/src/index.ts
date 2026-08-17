/**
 * Kanban dashboard plugin — entry point.
 *
 * Registers the KanbanPage component with the host dashboard's plugin
 * registry. The host loads this file (compiled) via ``manifest.json``
 * and calls ``window.__HERMES_PLUGINS__.register("kanban", Component)``.
 */

import { KanbanPage } from "./components/KanbanPage";

declare global {
  interface Window {
    __HERMES_PLUGINS__?: {
      register: (name: string, component: React.ComponentType) => void;
    };
  }
}

if (window.__HERMES_PLUGINS__) {
  window.__HERMES_PLUGINS__.register("kanban", KanbanPage);
}