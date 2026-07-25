/** E1 — connection-lost card (port of section 24's conn half): shows after 2
 * consecutive poll failures or when the event stream drops; names the actual
 * problem (WSL down / server erroring / stream dropped) and offers the
 * desktop shell's one-click WSL restart with a two-step arm. */

import { useEffect, useRef, useState } from "react";
import { queryClient } from "../state/queries";

declare global {
  interface Window {
    mfdiag?: {
      get?: () => Promise<{ state?: string; distro?: string }>;
      restartWsl?: () => Promise<void>;
    };
  }
}

export function ConnBanner() {
  const [pollFails, setPollFails] = useState(0);
  const [lastErr, setLastErr] = useState("");
  const [evtDropped, setEvtDropped] = useState(false);
  const [diag, setDiag] = useState<{ state?: string; distro?: string } | null>(null);
  const [lostAt, setLostAt] = useState(0);
  const [, setTick] = useState(0); // elapsed-label ticker
  const [fixArmed, setFixArmed] = useState(false);
  const [fixBusy, setFixBusy] = useState(false);
  const everConnected = useRef(false);

  // Consecutive instances-poll failures via the query cache.
  useEffect(() => {
    return queryClient.getQueryCache().subscribe((event) => {
      if (event.query.queryKey[0] !== "instances") return;
      if (event.type === "updated") {
        const action = (event as { action?: { type?: string; error?: Error } }).action;
        if (action?.type === "success") {
          setPollFails(0);
        } else if (action?.type === "error") {
          setPollFails((n) => n + 1);
          setLastErr(action.error?.message || "");
        }
      }
    });
  }, []);

  // Event-stream status from the client bus.
  useEffect(() => {
    const ev = window.mindflock?.events;
    if (!ev) return;
    return ev.onStatus((status) => {
      if (status === "connected") {
        everConnected.current = true;
        setEvtDropped(false);
      } else if (everConnected.current) {
        setEvtDropped(true);
      }
    });
  }, []);

  const lost = pollFails >= 2 || evtDropped;

  // Grid staleness dim + lost-at bookkeeping.
  useEffect(() => {
    document.body.classList.toggle("conn-lost", lost);
    if (!lost) {
      setLostAt(0);
      setDiag(null);
      setFixArmed(false);
      return;
    }
    setLostAt((t) => t || Date.now());
    const tick = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(tick);
  }, [lost]);

  // Ask the desktop shell for a diagnosis while the server isn't answering.
  useEffect(() => {
    if (!(lost && pollFails >= 2) || !window.mfdiag?.get) return;
    let live = true;
    window.mfdiag
      .get()
      .then((d) => {
        if (live && d) setDiag(d);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [lost, pollFails]);

  if (!lost) return null;

  let text: string;
  let fixLabel: string | undefined;
  if (diag && pollFails >= 2 && diag.state === "wsl-down") {
    text =
      "WSL (" +
      (diag.distro || "Ubuntu") +
      ") isn’t responding, so the server can’t run. Restarting WSL usually fixes this.";
    fixLabel = "Restart WSL";
  } else if (diag && pollFails >= 2 && diag.state === "wsl-missing") {
    text =
      "WSL isn’t installed on this computer. Run “wsl --install” in PowerShell, restart, then reopen MindFlock.";
  } else if (diag && pollFails >= 2 && diag.state === "not-installed") {
    text = "WSL is up, but MindFlock isn’t installed in it anymore. Reinstall it to continue.";
  } else if (diag && pollFails >= 2 && diag.state === "starting") {
    text = "The server is starting back up — this connects on its own, usually within a few seconds.";
  } else if (pollFails >= 2) {
    text = /^HTTP \d/.test(lastErr)
      ? "The server is reachable but answering with errors (" +
        lastErr +
        "). It may recover on its own; if not, check the server logs."
      : "The server isn’t answering — it may have stopped or be restarting. Reconnecting automatically.";
  } else {
    text =
      "The live update stream dropped — the page may lag a few seconds behind. Reconnecting automatically.";
  }

  const s = Math.max(0, Math.round((Date.now() - lostAt) / 1000));
  const elapsed = "Retrying… (" + (s < 90 ? s + "s" : Math.floor(s / 60) + "m " + (s % 60) + "s") + ")";

  const canFix = !!(fixLabel && window.mfdiag?.restartWsl);

  return (
    <div id="conn-banner" role="alert">
      <div className="conn-head">⚠ Connection to MindFlock lost</div>
      <div className="conn-detail" id="conn-detail">{text}</div>
      <div className="conn-foot">
        <span id="conn-elapsed">{elapsed}</span>
        {canFix && (
          <button
            id="conn-fix"
            type="button"
            disabled={fixBusy}
            onClick={() => {
              if (fixBusy) return;
              // --shutdown closes everything running in WSL: two-step arm.
              if (!fixArmed) {
                setFixArmed(true);
                return;
              }
              setFixBusy(true);
              setFixArmed(false);
              Promise.resolve(window.mfdiag!.restartWsl!())
                .catch(() => {})
                .then(() => setFixBusy(false));
            }}
          >
            {fixBusy ? "Restarting WSL…" : fixArmed ? "Really restart? (closes WSL apps)" : fixLabel}
          </button>
        )}
      </div>
    </div>
  );
}
