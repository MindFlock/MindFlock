import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Builds straight into the Python package's static dir with STABLE names:
// the served URLs (/app.js, /style.css, /) are a contract — the backend's
// cache middleware pattern-matches them and the test suite asserts on their
// content. Minification stays off: this is a localhost tool, gzip does the
// wire-size work, and readable output keeps the bundle debuggable and lets
// backend tests assert on real string literals.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../backend/web/static",
    emptyOutDir: false, // static/ also holds core/, addons/, vendor/, theme.css…
    // 'hidden' still emits app.js.map for local debugging (it's gitignored),
    // but omits the //# sourceMappingURL comment so the committed bundle has no
    // dangling reference to a map that isn't shipped.
    minify: false,
    sourcemap: "hidden",
    cssCodeSplit: false,
    rollupOptions: {
      // Rolldown inlines cross-module consts by default, which erases the named
      // constant a reader (and the backend's bundle assertions) look for:
      // `EMERGE_MS` stops existing and only a bare `2e4` remains. Same reason
      // minification is off above — this bundle is meant to stay readable.
      optimization: { inlineConst: false },
      output: {
        // Vite 8 bundles with Rolldown, which preserves JSDoc blocks that
        // Rollup+esbuild used to drop. Our source comments are long and frank;
        // shipping them verbatim would both bloat the bundle and publish notes
        // meant for us. Legal comments stay (dependency licences must survive)
        // and so do annotations (@__PURE__, @vite-ignore); only JSDoc goes.
        // Plain // and /* */ comments are stripped by Rolldown either way.
        comments: { legal: true, annotation: true, jsdoc: false },
        entryFileNames: "app.js",
        chunkFileNames: "js/chunks/[name].js",
        assetFileNames: (info) =>
          info.names?.some((n) => n.endsWith(".css")) ? "style.css" : "assets/[name][extname]",
      },
    },
  },
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      [
        "/api",
        "/core",
        "/addons",
        "/vendor",
        "/theme.css",
        "/mobile.css",
        "/m",
        "/favicon.png",
        "/logo.png",
        "/bird.png",
        "/apple-touch-icon.png",
      ].map((p) => [
        p,
        { target: "http://127.0.0.1:8765", changeOrigin: true, ws: true },
      ])
    ),
  },
});
