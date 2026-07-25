import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { queryClient } from "./state/queries";
import { publishToast } from "./lib/toast";
import { installGlobalDropGuards } from "./lib/clipboard";
import { applyStoredAppearance } from "./components/settings/screens/Appearance";
import App from "./App";
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
installGlobalDropGuards();
applyStoredAppearance();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>
);

// core/slots.js (addon bars + provider picker) needs its mount points
// (#addon-bars, #provider-list) — inject it after React's first paint.
requestAnimationFrame(() => {
  const s = document.createElement("script");
  s.type = "module";
  s.src = "/core/slots.js";
  document.body.appendChild(s);
});
