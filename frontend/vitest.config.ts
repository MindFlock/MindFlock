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
  },
});
