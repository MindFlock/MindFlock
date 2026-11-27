import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { queryClient } from "./state/queries";
import { publishToast } from "./lib/toast";
import { publishWindowName } from "./lib/windowName";
import { installGlobalDropGuards } from "./lib/clipboard";
import { applyStoredAppearance } from "./components/settings/screens/Appearance";
import { installPrefsSync } from "./lib/prefsSync";
import { installJoinLink } from "./lib/joinLink";
import { installRepaintTriggers } from "./lib/wsTerm";
import { isPopoutWindow } from "./lib/popout";
import { installPopoutWindow } from "./popout/boot";
import App from "./App";
// After App: its sheets keep their order in the bundle, the pop-out's own
// CSS comes after them (docs: CSS order follows imports).
import { PopoutApp } from "./popout/PopoutApp";
import "./styles/index.css";

// Expose the bundled xterm as globals: core/ws-xterm.js (the addon-pane
// helper) and mobile.html's vendor scripts expect window.Terminal /
// window.FitAddon — one bundled copy serves both worlds.
Object.assign(window as unknown as Record<string, unknown>, {
  Terminal,
  FitAddon: { FitAddon },
  WebLinksAddon: { WebLinksAddon },
});

publishToast();
// Before the addons load: notify.js names its notifications through this.
publishWindowName();
installGlobalDropGuards();
applyStoredAppearance();
// Repaint every terminal on screen when the window comes back (focus, restore,
// un-occlusion, the desktop shell's "mindflock:repaint") — the fix for panes
// that went blank white until a scroll (lib/wsTerm.ts, docs/web-ui.md).
installRepaintTriggers(window, document);

// A pop-out window (lib/popout.ts, `?popout=<title>`): one session in a
// window of its own. It looks like the main window (the appearance and
// repaint wiring above) and follows its theme live, but runs none of what
// only the main window should: no extension slots or host, no join link, no
// prefs sync of its own (it never pushes a pref — popout/boot.ts).
const popout = isPopoutWindow();
if (popout) installPopoutWindow();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>{popout ? <PopoutApp /> : <App />}</QueryClientProvider>
  </StrictMode>
);

// core/slots.js (addon bars + provider picker) needs its mount points
// (#addon-bars, #provider-list) — inject it after React's first paint.
if (!popout) requestAnimationFrame(() => {
  const s = document.createElement("script");
  s.type = "module";
  s.src = "/core/slots.js";
  document.body.appendChild(s);
});

// Prefs that follow the person (lib/prefs.ts): reconcile this browser's
// cached keymap / prompts / theme / bars with the server's copy, and re-pull
// whenever settings sync brings one in from another device. After first paint:
// localStorage already painted the page, this only corrects it.
if (!popout) requestAnimationFrame(() => installPrefsSync());

// Join by link (lib/joinLink.ts): a `#join=<code>` the desktop shell put on
// the URL opens People with Join someone prefilled. After first paint, so the
// Settings dialog it opens has an App to open in.
if (!popout) requestAnimationFrame(() => installJoinLink());
