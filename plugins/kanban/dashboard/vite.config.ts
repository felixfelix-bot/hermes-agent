/**
 * Vite build config for the Kanban dashboard plugin.
 *
 * Builds src/index.ts → dist/index.js (IIFE) and src/kanban.css → dist/style.css.
 * React + UI components are provided at runtime by the host dashboard's
 * plugin SDK (window.__HERMES_PLUGIN_SDK__), so they are marked as external
 * and never bundled.
 *
 * Usage:
 *   cd plugins/kanban/dashboard && npm run build
 *
 * The output replaces dist/index.js and dist/style.css that the dashboard
 * serves via the manifest.json entry.
 */

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { readFileSync, writeFileSync } from "fs";
import { resolve } from "path";

export default defineConfig({
  plugins: [
    react(),
    // Copy the CSS as a separate asset (the manifest expects dist/style.css).
    {
      name: "kanban-copy-css",
      closeBundle() {
        const src = readFileSync(
          resolve(__dirname, "src/kanban.css"),
          "utf-8",
        );
        writeFileSync(resolve(__dirname, "dist/style.css"), src);
        // eslint-disable-next-line no-console
        console.log(`[kanban] CSS written to dist/style.css (${src.length} bytes)`);
      },
    },
  ],
  // TypeScript paths — resolve via the web workspace's tsconfig.
  resolve: {
    alias: {
      "@": resolve(__dirname, "../../web/src"),
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: false, // Don't wipe dist/ — the CSS plugin writes after build.
    // IIFE format: a single self-executing script that registers the plugin.
    lib: {
      entry: resolve(__dirname, "src/index.ts"),
      name: "HermesKanbanPlugin",
      formats: ["iife"],
      fileName: () => "index.js",
    },
    rollupOptions: {
      // React + all UI components are provided by the host at runtime.
      // They must NOT be bundled — the IIFE references window.__HERMES_PLUGIN_SDK__.
      external: [
        "react",
        "react-dom",
        "react/jsx-runtime",
        "@/lib/api",
        "@/lib/utils",
        "@/i18n",
        "@nous-research/ui",
      ],
    },
    // Minify for production to match the existing 163KB bundle size.
    minify: "esbuild",
    cssCodeSplit: false,
  },
});
