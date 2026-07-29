# MindFlock web UI (React + TypeScript)

The desktop SPA, rewritten from the former 9k-line vanilla `app.js`. Builds
into `../backend/web/static/` with stable names (`app.js`,
`style.css`, `index.html`) so the served URLs, caching behavior, and the
Python test suite's content assertions are unchanged.

## Workflow

```bash
npm install        # once
npm run dev        # Vite on :5173, /api + websockets proxied to :8765
npm run build      # tsc --noEmit && vite build → ../backend/web/static/
```

Run the FastAPI server separately (`mindflock serve` or uvicorn) for the dev
proxy to talk to. Build output is committed — rebuild before committing UI
changes.

## Layout

- `src/api/` — typed fetch client + backend payload types.
- `src/state/` — TanStack Query hooks (the 4s instances poll is the app's
  heartbeat and feeds `window.mindflock.__setSessions` for addons) and the
  Zustand UI store (focus, layout, dialogs — same localStorage keys as the
  vanilla app, so upgrades keep user layouts).
- `src/lib/` — non-React cores: `terminals.ts` (the xterm registry — terminals
  live OUTSIDE React and are adopted into panes via refs, so re-renders never
  remount a PTY; all the tmux mouse/copy/wheel workarounds live here),
  `keymap.ts` (bindings + Ctrl+K chords + rebind store), `sessionActions.ts`,
  `stage.ts`, `diff.ts`, `presets.ts`, `clipboard.ts`, `toast.ts`, `format.ts`,
  `shell.ts` (what the surrounding desktop shell is: `inShell`, `isMacShell`,
  `hasNativeWindowControls`, `onFullScreenChanged`).
- **`lib/shell.ts` is the only place allowed to touch `window.mfshell` /
  `window.winctl`.** Two rules go with it: never branch on
  `navigator.userAgent`/`platform` for shell behavior (a Mac user in Safari has
  no traffic lights to dodge, and must keep the standard layout), and when a
  capability flag exists, gate on the flag rather than on `isMacShell()` — the
  shell and the engine-served frontend update independently, so an older shell
  has to be able to keep the layout it was built for.
- `src/components/` — the UI: `sidebar/`, `grid/` (panes, diff/queue tabs,
  special panes), `settings/` (one file per screen), `dialogs/`, `palette/`,
  plus TopBar / NotificationsBell / EventToasts / ConnBanner / VoiceInput.
- **Styles** live next to the component they style — `Foo.css` beside
  `Foo.tsx`. Only the cross-cutting sheets stay in `src/styles/`: `tokens.css`
  (the palette, including the `.light` swap), `base.css` (reset + page
  skeleton), `theme-light.css` (light-mode patches for colors a variable swap
  can't reach), and `index.css`, which declares the cascade layer order
  (`tokens, base, components, theme`) and lists every component sheet.
  Class names are global and unchanged, so the UI is pixel-identical to the
  vanilla app — and the backend tests that assert on `/style.css` selectors
  keep passing. Adding a component: drop `Foo.css` next to `Foo.tsx` and add
  one `@import … layer(components)` line to `index.css`. Anything that must
  override component rules belongs in `theme-light.css`, not in a specificity
  war at the call site.

## Contracts to preserve

- **Addon API**: `core/events.js` loads before the bundle; `core/slots.js`
  is injected after first paint and needs the `#addon-bars` and
  `#provider-list` mount points plus `window.reloadProviderPicker`. The
  bundled xterm is re-exported as `window.Terminal` / `window.FitAddon` /
  `window.WebLinksAddon` for `core/ws-xterm.js` and addon panes.
- **Build shape**: unminified, stable filenames, single CSS file. Backend
  tests assert on bundle string literals — keep ids, API paths, and
  user-visible copy stable or update the tests with them.
- **Mobile**: `/m` (`static/mobile.*`) is separate and still vanilla.
