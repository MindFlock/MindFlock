/** The keyboard shortcuts sheet (docs/flights.md "Round 17": task
 * `zoom-shortcuts`), opened with `?` like Linear, Figma and Gmail. The sky
 * passes the groups it knows (lib/skyShortcuts `SKY_SHORTCUT_GROUPS`); the
 * sheet draws them as a centred card over a dimmed backdrop — never a native
 * dialog — the groups in two columns, each key a `<kbd>` cap (⌘ / ⌥ on a
 * Mac: `withPlatform`). A filter at the top hides the rows that don't match
 * by key or by words (lib/skyShortcuts `filterShortcuts`).
 *
 * While open it listens to keydown (capture, so the sky's own Esc doesn't
 * also clear a selection): Esc closes; `?` closes unless the filter holds
 * text (then it is a character to search for); Tab stays inside the card.
 * The focus goes to the filter on open and back where it was on close; a
 * press on the backdrop closes. Portalled to <body> so no transformed
 * ancestor can trap the fixed card. `SkyShortcutsCard` (the card alone) and
 * `sheetKey` are exported for the tests (no DOM in the node environment). */

import { useEffect, useMemo, useRef, useState, type ReactElement, type RefObject } from "react";
import { createPortal } from "react-dom";
import { filterShortcuts, isMacLike, withPlatform } from "../../lib/skyShortcuts";
import "./SkyShortcuts.css";

export interface ShortcutItem {
  /** The keys as pressed, each a cap: ["Ctrl", "Z"], ["Space", "drag"], ["0"]. */
  keys: string[];
  what: string;
}

export interface ShortcutGroup {
  title: string;
  items: ShortcutItem[];
}

export interface SkyShortcutsProps {
  open: boolean;
  onClose(): void;
  groups: readonly ShortcutGroup[];
}

/** What a key does while the sheet is open, the filter holding `filter`:
 * "close", "trap" (Tab: keep the focus in the card) or null (the key's own). */
export function sheetKey(key: string, filter: string): "close" | "trap" | null {
  if (key === "Escape") return "close";
  if (key === "?" && !filter) return "close";
  if (key === "Tab") return "trap";
  return null;
}

/** A cap that is a word of the pointer ("drag", "click", "Wheel") reads as a
 * gesture, not a key: drawn lighter. */
const GESTURES = new Set(["drag", "click", "double-click", "right-click", "Wheel"]);

export interface SkyShortcutsCardProps {
  groups: readonly ShortcutGroup[];
  filter: string;
  onFilter(q: string): void;
  onClose(): void;
  filterRef?: RefObject<HTMLInputElement | null>;
  cardRef?: RefObject<HTMLDivElement | null>;
}

/** The card itself: head (title, ×), the filter, the groups (filtered). */
export function SkyShortcutsCard(p: SkyShortcutsCardProps): ReactElement {
  const shown = filterShortcuts(p.groups, p.filter);
  return (
    <div ref={p.cardRef} className="sky-keys-card" role="dialog" aria-modal="true" aria-label="Keyboard shortcuts" onPointerDown={(e) => e.stopPropagation()}>
      <div className="sky-keys-head">
        <strong>Keyboard shortcuts</strong>
        <span className="sky-keys-hint">
          <kbd>?</kbd> or <kbd>Esc</kbd> to close
        </span>
        <button type="button" className="fl-icon-btn sky-keys-close" aria-label="Close" title="Close (Esc)" onClick={p.onClose}>
          ×
        </button>
      </div>
      <input
        ref={p.filterRef}
        className="sky-keys-filter"
        type="search"
        placeholder="Filter — a key or a word (undo, shift, zoom…)"
        aria-label="Filter the shortcuts"
        value={p.filter}
        onChange={(e) => p.onFilter(e.target.value)}
        spellCheck={false}
        autoComplete="off"
      />
      {shown.length ? (
        <div className="sky-keys-body">
          <div className="sky-keys-cols">
          {shown.map((g) => (
            <section key={g.title} className="sky-keys-group" aria-label={g.title}>
              <h4>{g.title}</h4>
              <ul>
                {g.items.map((it, i) => (
                  <li key={i} className="sky-keys-row">
                    <span className="sky-keys-caps">
                      {it.keys.map((k, j) => (
                        <kbd key={j} className={GESTURES.has(k) ? "is-gesture" : undefined}>
                          {k}
                        </kbd>
                      ))}
                    </span>
                    <span className="sky-keys-what">{it.what}</span>
                  </li>
                ))}
              </ul>
            </section>
          ))}
          </div>
        </div>
      ) : (
        <p className="sky-keys-none">No shortcut matches “{p.filter}”.</p>
      )}
    </div>
  );
}

export function SkyShortcuts(p: SkyShortcutsProps): ReactElement | null {
  const [filter, setFilter] = useState("");
  const filterRef = useRef<HTMLInputElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const groups = useMemo(() => withPlatform(p.groups, isMacLike()), [p.groups]);
  const filterNow = useRef("");
  filterNow.current = filter;
  const onClose = useRef(p.onClose);
  onClose.current = p.onClose;

  useEffect(() => {
    if (!p.open) return;
    const back = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setFilter("");
    filterRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      const act = sheetKey(e.key, filterNow.current);
      if (act === "close") {
        e.preventDefault();
        e.stopPropagation();
        onClose.current();
      } else if (act === "trap") {
        const all = [...(cardRef.current?.querySelectorAll<HTMLElement>("input, button") || [])];
        if (!all.length) return;
        e.preventDefault();
        e.stopPropagation();
        const i = all.indexOf(document.activeElement as HTMLElement);
        all[(i + (e.shiftKey ? -1 : 1) + all.length) % all.length].focus();
      } else if (!e.ctrlKey && !e.metaKey && !e.altKey && document.activeElement !== filterRef.current && e.key.length === 1) {
        // A character typed while the focus wandered (the × button): it
        // filters, and never reaches the sky's keys (T, V, H, 0…) under the
        // sheet.
        e.stopPropagation();
        filterRef.current?.focus();
      } else {
        // Everything else (a character in the filter, Backspace, arrows)
        // belongs to the sheet, not the sky behind it.
        e.stopPropagation();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      if (back?.isConnected) back.focus();
    };
  }, [p.open]);

  if (!p.open) return null;
  const sheet = (
    <div
      className="sky-keys-backdrop"
      data-no-drag
      onPointerDown={(e) => {
        e.stopPropagation();
        if (e.target === e.currentTarget) p.onClose();
      }}
    >
      <SkyShortcutsCard groups={groups} filter={filter} onFilter={setFilter} onClose={p.onClose} filterRef={filterRef} cardRef={cardRef} />
    </div>
  );
  return typeof document !== "undefined" && document.body ? createPortal(sheet, document.body) : sheet;
}
