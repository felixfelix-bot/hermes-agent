/**
 * Vitest config for the Kanban dashboard plugin.
 *
 * Plugin source lives outside the web workspace, so it needs its own
 * config. Uses node environment (no DOM) for pure-function tests.
 *
 * Run: cd plugins/kanban/dashboard && npx vitest run
 */
import { defineConfig } from "vitest/config";
import path from "path";

export default defineConfig({
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "../../web/src"),
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
