/** Top bar (port of the 030 partial + section 17's chrome wiring):
 * sidebar/theme cluster, notifications bell, the main menu (New / Recent
 * dropdown / Prompts / Command / Settings), centered wordmark, and the
 * Electron drag region.
 *
 * On macOS the whole thing mirrors: the window shows the native traffic lights
 * top-left (electron/main.js `titleBarStyle: 'hidden'`), so the bar leaves room
 * for them and moves the logo / theme / bell cluster to the right — where a Mac
 * has nothing else, since our own – □ ✕ aren't drawn there. Same elements, same
 * behaviour, mirrored placement. */

import { useEffect, useState } from "react";
import { useUi } from "../state/store";
import { useTestPlans } from "../state/queries";
import { dueCount } from "./dialogs/verify";
import { rethemeAll } from "../lib/terminals";
import { NotificationsBell } from "./NotificationsBell";
import { redrawFavicon } from "./EventToasts";
import { api } from "../api/client";
import { hasNativeWindowControls, isFullScreen, onFullScreenChanged } from "../lib/shell";

function applyTheme(light: boolean) {
  document.documentElement.classList.toggle("light", light);
}

/* The wordmark carries the RUNNING ENGINE's version (GET /api/doctor), not the
 * desktop shell's: this frontend ships inside the engine wheel, so that string
 * is the provenance of the UI you're looking at and of the sessions behind it.
 * When the shell is a different release the drift toast (electron/main.js) says
 * so explicitly — this only has to answer "what am I running". Module-level so
 * a remount doesn't flash an empty slot; one fetch per page load, and the
 * server caches the payload anyway. */
let engineVersion = "";

export function TopBar() {
  const ui = useUi();
  const [light, setLight] = useState(() => document.documentElement.classList.contains("light"));
  const [version, setVersion] = useState(engineVersion);
  // How much verification is waiting on you, without opening the dialog. Same
  // query and same helper the dialog itself uses, so the badge and the list can
  // never disagree — the discipline the Intake tab counts follow. The top bar is
  // mounted for the life of the page, which is deliberately what keeps this
  // (cheap, local-file) poll running: the point of a badge is to tell you about
  // the thing you were not already looking at.
  const { data: testPlans } = useTestPlans();
  const due = dueCount(testPlans?.plans || []);
  // Evaluated once: the shell can't grow or lose its title bar mid-run.
  const [mac] = useState(hasNativeWindowControls);
  // macOS hides the traffic lights in fullscreen — stop reserving their room.
  const [fullScreen, setFullScreen] = useState(false);

  useEffect(() => {
    if (!mac) return;
    // Ask for the current state (a window already fullscreen at load never gets
    // a transition), then follow changes.
    let live = true;
    void isFullScreen().then((f) => {
      if (live) setFullScreen(f);
    });
    const off = onFullScreenChanged(setFullScreen);
    return () => {
      live = false;
      off();
    };
  }, [mac]);

  useEffect(() => {
    if (engineVersion) return; // already known this page load
    let live = true;
    api<{ version?: string }>("/api/doctor")
      .then((d) => {
        const v = (d?.version || "").trim();
        // A source tree with no installed dist reports "0+unknown" (see
        // backend/__init__.py). Show nothing rather than a fake-looking
        // version — such a run is a dev shell, which the -DEV badge marks.
        if (!v || !/^\d/.test(v) || v.startsWith("0+")) return;
        engineVersion = v;
        if (live) setVersion(v);
      })
      .catch(() => {}); // an unreachable backend is the conn-banner's job
    return () => {
      live = false;
    };
  }, []);


  const toggleTheme = () => {
    const next = !light;
    setLight(next);
    try {
      localStorage.setItem("cs_theme", next ? "light" : "dark");
    } catch {
      /* storage unavailable */
    }
    applyTheme(next);
    rethemeAll(); // re-theme already-open terminals
    redrawFavicon(); // the tab favicon inverts with the theme
  };

  // The cluster that swaps sides on macOS. Defined once and rendered in exactly
  // one place, so the two layouts can't drift apart.
  const logo = <span id="brand-logo" aria-hidden="true" />;
  const themeToggle = (
    <button
      id="theme-btn"
      type="button"
      title={light ? "Switch to dark mode" : "Switch to light mode"}
      aria-label="Toggle light / dark mode"
      onClick={toggleTheme}
    >
      {/* Monochrome, not ☀️/🌙: an emoji glyph paints its own colors, and a
          yellow moon on a yellow top bar (Goldfinch, Toucan) vanishes.
          currentColor follows --text, which regions.css rebinds per region,
          so these stay legible on a bar of any hue. */}
      <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">
        {light ? (
          <g fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <circle cx="12" cy="12" r="4.2" />
            <path d="M12 2.5v2.4M12 19.1v2.4M2.5 12h2.4M19.1 12h2.4M5.3 5.3l1.7 1.7M17 17l1.7 1.7M18.7 5.3L17 7M7 17l-1.7 1.7" />
          </g>
        ) : (
          <path
            fill="currentColor"
            d="M20.2 14.6A8.6 8.6 0 0 1 9.4 3.8a8.6 8.6 0 1 0 10.8 10.8z"
          />
        )}
      </svg>
    </button>
  );

  return (
    <div id="topbar" data-mac={mac ? "" : undefined} data-mac-lights={mac && !fullScreen ? "" : undefined}>
      <div className="tb-start">
        <div className="tb-left">
          {!mac && logo}
          <button
            id="sidebar-toggle"
            type="button"
            title={(ui.sidebarHidden ? "Show" : "Hide") + " sidebar (Ctrl+B / ⌘B)"}
            aria-label="Toggle sidebar"
            onClick={() => ui.toggleSidebar()}
          >
            <svg
              viewBox="0 0 24 24"
              width="16"
              height="16"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinejoin="round"
            >
              <rect x="3" y="4" width="18" height="16" rx="2.5" />
              <line x1="9" y1="4" x2="9" y2="20" />
            </svg>
          </button>
          {!mac && themeToggle}
          {!mac && <NotificationsBell />}
        </div>
        <nav className="tb-menu" aria-label="Main actions">
          <button
            id="new-btn"
            className="tb-new"
            title="New session (Ctrl+N)"
            onClick={() => ui.openDialogFor("new-session")}
          >
            New
          </button>
          {/* Intake sits next to New because both are about starting sessions —
              one from scratch, one from what came in. */}
          <button
            id="intake-btn"
            className="tb-item"
            type="button"
            title="Intake — tickets, pull requests and issues waiting to become sessions (Alt+I)"
            /* No aria-label: the visible "Intake" IS the name. The old one still
               said "Open work", the surface's name two renames ago, so a screen
               reader announced a button nobody could see. */
            onClick={() => ui.openDialogFor("intake")}
          >
            Intake
          </button>
          {/* Verify closes the loop Intake opens: work came in there, and this is
              where it comes back once it has actually shipped. The count is the
              whole reason it earns a slot on the bar — work that ships while you
              are elsewhere is exactly the thing nobody remembers to go and look
              for. It counts SHIPPED CHANGES NOBODY HAS FINISHED CHECKING, which
              is the same rule the dialog's first heading renders (`dueCount` and
              the "Not checked yet" group are one predicate) — deliberately not
              "things waiting on you personally", since one of them may be an
              agent mid-run. */}
          <button
            id="verify-btn"
            className="tb-item"
            type="button"
            title="Verify — shipped changes nobody has checked (Alt+V)"
            /* No aria-label, for the same reason as Intake above: the visible
               "Verify" IS the name, and a second wording would be the one a
               screen reader announced. */
            onClick={() => ui.openDialogFor("verify")}
          >
            Verify
            {/* Nothing at all on zero, the way the Intake tab counts do it: a "0"
                that becomes "3" a moment later reads as "nothing to do", which is
                the one thing this badge must never say while it is still finding
                out. */}
            {due > 0 && <span className="tb-count">{due}</span>}
          </button>
          {/* One page, so one button: the disk manager used to be the other
              half of a dropdown here, and it is now the same list seen from the
              other end (see RecentDialog). */}
          <button
            id="recent-btn"
            className="tb-item"
            type="button"
            title="Recently closed — reopen closed work, or clear out what it left on disk"
            onClick={() => ui.openDialogFor("recent")}
          >
            Recent
          </button>
          <button
            id="prompts-btn"
            className="tb-item"
            type="button"
            title="Prompt library — click a ready-made prompt to paste it into the selected session"
            aria-label="Open prompt library"
            onClick={() => ui.openDialogFor("prompts")}
          >
            Prompts
          </button>
          <button
            id="palette-btn"
            className="tb-item"
            type="button"
            title="Command palette — Ctrl+P / ⌘P"
            aria-label="Open command palette"
            onClick={() => ui.openDialogFor("palette")}
          >
            Command
          </button>
          <button
            id="settings-btn"
            className="tb-item"
            type="button"
            title="Settings"
            aria-label="Open settings"
            onClick={() => ui.openDialogFor("settings")}
          >
            Settings
          </button>
        </nav>
      </div>
      {/* No title= on the wordmark: .tb-brand is pointer-events:none so the
          window-drag works across it, which also means a tooltip can never
          fire. The version is visible, so nothing is lost. */}
      <span className="tb-brand">
        MindFlock
        {(window as unknown as { mfshell?: { dev?: boolean } }).mfshell?.dev && (
          <span style={{ color: "var(--red)", fontWeight: 700 }}>-DEV</span>
        )}
        {version && <span className="tb-version">v{version}</span>}
      </span>
      <div className="tb-drag" />
      {/* macOS: the mirror of .tb-left. Ordered so the logo anchors the far
          corner, the way it does on the left everywhere else. */}
      {mac && (
        <div className="tb-end">
          <NotificationsBell />
          {themeToggle}
          {logo}
        </div>
      )}
    </div>
  );
}
