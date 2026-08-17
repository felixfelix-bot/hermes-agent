import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

/**
 * Vite build config for the Kanban dashboard plugin.
 *
 * Produces an IIFE bundle (dist/index.js + dist/style.css) that registers
 * itself with the host dashboard via window.__HERMES_PLUGINS__.register().
 *
 * The plugin uses the host's React/UI components via the global
 * window.__HERMES_PLUGIN_SDK__ — so we externalize react and only bundle
 * the plugin's own source code.
 */
export default defineConfig({
  plugins: [react()],
  build: {
    lib: {
      entry: path.resolve(__dirname, "src/index.ts"),
      name: "HermesKanbanPlugin",
      formats: ["iife"],
      fileName: () => "index.js",
    },
    outDir: "dist",
    emptyOutDir: false, // Don't clobber style.css if it was already built
    cssCodeSplit: false,
    rollupOptions: {
      // React and the SDK are provided by the host at runtime
      external: ["react", "react-dom"],
      output: {
        // Inline CSS into the JS bundle as the host loads dist/index.js
        // as a plain script tag (no CSS link injection for plugins).
        assetFileNames: "style.css",
        globals: {
          react: "React",
          "react-dom": "ReactDOM",
        },
      },
    },
    // Don't minify for readability — the bundle is ~163KB and minification
    // saves little for a plugin loaded once at dashboard startup.
    minify: false,
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  // Plugin runs in the host dashboard's browser context, not Node.
  // The SDK globals (window.__HERMES_PLUGIN_SDK__) are available at runtime.
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
});
