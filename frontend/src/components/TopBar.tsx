/** Top bar (port of the 030 partial + section 17's chrome wiring):
 * sidebar/theme cluster, notifications bell, the main menu (New / Recent
 * dropdown / Prompts / Command / Settings), centered wordmark, and the
 * Electron drag region. */

import { useEffect, useRef, useState } from "react";
import { useUi } from "../state/store";
import { rethemeAll } from "../lib/terminals";
import { NotificationsBell } from "./NotificationsBell";
import { redrawFavicon } from "./EventToasts";

function applyTheme(light: boolean) {
  document.documentElement.classList.toggle("light", light);
}

export function TopBar() {
  const ui = useUi();
  const [light, setLight] = useState(() => document.documentElement.classList.contains("light"));
  const [recentOpen, setRecentOpen] = useState(false);
  const dropRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!recentOpen) return;
    const close = (e: MouseEvent) => {
      if (!dropRef.current?.contains(e.target as Node)) setRecentOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setRecentOpen(false);
    };
    document.addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [recentOpen]);

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

  return (
    <div id="topbar">
      <div className="tb-start">
        <div className="tb-left">
          <span id="brand-logo" aria-hidden="true" />
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
          <NotificationsBell />
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
          <div className={"tb-drop" + (recentOpen ? " open" : "")} id="recent-menu" ref={dropRef}>
            <button
              type="button"
              className="tb-item tb-drop-btn"
              id="recent-menu-btn"
              aria-haspopup="true"
              aria-expanded={recentOpen}
              title="Reopen a recent session or manage workspaces on disk"
              onClick={(e) => {
                e.stopPropagation();
                setRecentOpen((v) => !v);
              }}
            >
              Recent
            </button>
            <div className="tb-drop-panel" role="menu">
              <button
                type="button"
                id="recent-btn"
                role="menuitem"
                onClick={() => {
                  setRecentOpen(false);
                  ui.openDialogFor("recent");
                }}
              >
                Recently closed…
              </button>
              <button
                type="button"
                id="workspaces-btn"
                role="menuitem"
                data-caps="git"
                onClick={() => {
                  setRecentOpen(false);
                  ui.openDialogFor("workspaces");
                }}
              >
                Workspaces on disk…
              </button>
            </div>
          </div>
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
      <span className="tb-brand">
        MindFlock
        {(window as unknown as { mfshell?: { dev?: boolean } }).mfshell?.dev && (
          <span style={{ color: "var(--red)", fontWeight: 700 }}>-DEV</span>
        )}
      </span>
      <div className="tb-drag" />
    </div>
  );
}
