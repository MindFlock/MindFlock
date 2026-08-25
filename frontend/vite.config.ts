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
      output: {
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
