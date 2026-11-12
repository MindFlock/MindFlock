import { defineConfig } from "vitest/config";

// Unit tests cover the pure, side-effect-free logic modules (layout / diff /
// keymap / ordering / stage). They need no DOM, so the lightweight "node"
// environment is used. This config is separate from vite.config.ts on
// purpose: the production build (vite build) is untouched by test settings.
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
    setupFiles: ["./src/__tests__/setup.ts"],
    // CSS is skipped by default, which turns `import X from "./foo.css?raw"`
    // into "". The structural tests read stylesheets as text (`?raw` only — a
    // component's plain `import "./Foo.css"` stays unprocessed).
    css: { include: [/\.css\?raw$/] },
    // Many tests start with `await import("../components/…")`, which compiles
    // that component's whole module graph inside the test's own clock. On a
    // busy machine (the Python suite running beside it, a loaded CI runner)
    // that alone passed vitest's 5 s default: r5wSetup timed out importing
    // the sidebar, and passed in 3 s on its own. A test that really hangs
    // still fails, later.
    testTimeout: 20_000,
  },
});
