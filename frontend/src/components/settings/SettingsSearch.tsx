/** "Search settings…" at the top of the Settings nav.
 *
 * The command palette already finds settings well (synonyms included:
 * "parallel", "max sessions" → Sessions running at once), but nothing inside
 * Settings pointed at it. This box uses the palette's own index
 * (palette/settingsSearch.ts — read-only here, one list of words for both),
 * lists the matches under the box while you type (each with a small line
 * saying what is behind it), and on a pick opens
 * the screen, scrolls to the control (opening a fold around it) and
 * highlights it for a moment. */

import { useEffect, useRef, useState } from "react";
import { revealInSettings, searchSettings, type SettingsSearchEntry } from "../palette/settingsSearch";
import "./SettingsSearch.css";

const HIT_MS = 2000;

/** The box's placeholder. The Ctrl+F hint is the one the list dialogs'
 *  filters carry (DialogFilter); "settings" is dropped so the hint fits the
 *  172px nav — the aria-label still says "Search settings". */
export const SEARCH_PLACEHOLDER = "Search…  ( Ctrl+F )";

/** Ctrl+F / Cmd+F (no Alt): the shortcut the box answers to. */
export function isFindKey(e: { key: string; ctrlKey: boolean; metaKey: boolean; altKey: boolean }): boolean {
  return (e.key === "f" || e.key === "F") && (e.ctrlKey || e.metaKey) && !e.altKey;
}

/** Outline the matched control once its screen has rendered (the reveal
 * itself — scroll + open folds — is the palette's revealInSettings). */
function highlight(selector: string): void {
  let tries = 0;
  const look = () => {
    const el = document.querySelector<HTMLElement>("#settings-screens " + selector);
    if (!el) {
      if (++tries < 30) setTimeout(look, 35);
      return;
    }
    el.setAttribute("data-search-hit", "");
    setTimeout(() => el.removeAttribute("data-search-hit"), HIT_MS);
  };
  setTimeout(look, 0);
}

/** `onPick(key, advanced)`: `advanced` = the result sits in an AdvancedFold,
 * so the dialog turns advanced settings on before showing it. */
export function SettingsSearch({ onPick }: { onPick(key: string, advanced?: boolean): void }) {
  const [q, setQ] = useState("");
  const [cursor, setCursor] = useState(0);
  const query = q.trim();
  const hits = query ? searchSettings(query, 8) : [];
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Ctrl+F focuses this box instead of opening the browser's find bar, as in
  // Recently closed and every Intake tab (DialogFilter). Mounted only while
  // Settings is open, so the shortcut belongs to Settings only then. Capture,
  // so it beats the app's own document dispatcher and the browser.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!isFindKey(e)) return;
      e.preventDefault();
      e.stopPropagation();
      inputRef.current?.focus();
      inputRef.current?.select();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, []);

  const pick = (e: SettingsSearchEntry) => {
    setQ("");
    setCursor(0);
    onPick(e.key, !!e.advanced);
    if (e.anchor) {
      revealInSettings(e.anchor);
      highlight(e.anchor);
    } else {
      document.getElementById("settings-screens")?.scrollTo?.({ top: 0 });
    }
  };

  return (
    <div className="set-search">
      <input
        ref={inputRef}
        type="search"
        id="settings-search"
        className="set-search-input"
        placeholder={SEARCH_PLACEHOLDER}
        aria-keyshortcuts="Control+F Meta+F"
        aria-label="Search settings"
        aria-controls="settings-search-results"
        autoComplete="off"
        spellCheck={false}
        value={q}
        onChange={(e) => {
          setQ(e.target.value);
          setCursor(0);
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape" && q) {
            // Clear the box; the dialog itself closes only on a second press.
            e.preventDefault();
            setQ("");
          } else if (e.key === "ArrowDown" && hits.length) {
            e.preventDefault();
            setCursor((c) => (c + 1) % hits.length);
          } else if (e.key === "ArrowUp" && hits.length) {
            e.preventDefault();
            setCursor((c) => (c - 1 + hits.length) % hits.length);
          } else if (e.key === "Enter" && hits.length) {
            e.preventDefault();
            pick(hits[Math.min(cursor, hits.length - 1)]);
          }
        }}
      />
      {query ? (
        <div id="settings-search-results" className="set-search-results" role="listbox" aria-label="Matching settings">
          {hits.length ? (
            hits.map((h, i) => (
              <button
                type="button"
                role="option"
                aria-selected={i === cursor}
                key={h.label}
                className={"set-search-hit" + (i === cursor ? " active" : "")}
                onMouseEnter={() => setCursor(i)}
                onClick={() => pick(h)}
              >
                <span className="set-search-title">{h.label}</span>
                <span className="set-search-sub">{h.sub}</span>
              </button>
            ))
          ) : (
            <p className="set-search-none">No setting matches “{query}”.</p>
          )}
        </div>
      ) : null}
    </div>
  );
}
