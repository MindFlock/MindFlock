# Frontend static assets

The web UI is a React + TypeScript app whose SOURCE lives in `frontend/` at
the repo root. This directory holds what the server actually serves:

| File | What it is |
|------|------------|
| `index.html`, `app.js`, `style.css` | **Build output** — regenerate with `npm run build` in `frontend/`; do not edit by hand. Stable, unminified names so the backend's cache middleware and the test suite's content assertions keep working. |
| `core/` | The public extension runtime, deliberately outside the bundle: `events.js` (the `window.mindflock` event bus, loaded before the app), `slots.js` (addon bars + provider picker, injected by the app after first paint), `ws-xterm.js`, `addon-modal.js`. |
| `addons/` | Optional feature modules attaching via `window.mindflockAddons`. |
| `theme.css` | Theme palettes shared with the mobile page — linked at runtime, not bundled. |
| `mobile.*` | The single-terminal phone UI served at `/m` (still framework-free). |
| `vendor/` | xterm.js for the mobile page (the desktop app bundles its own copy and re-exports it as `window.Terminal` for `core/ws-xterm.js`). |

Dev loop: `cd frontend && npm run dev` starts Vite on :5173 with `/api` (and
websockets) proxied to the FastAPI server on :8765 — hot reload against live
data. `npm run build` typechecks and writes the three build artifacts here;
they are committed so installing the Python package needs no Node.

Frontend tests live in the Python suite: they fetch `/app.js` / `/style.css`
over HTTP and assert on string literals (ids, API paths, user-visible copy)
that survive the unminified build.
