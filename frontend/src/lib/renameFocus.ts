/** The keyboard stays on a session you rename (lib/renameSession).
 *
 * A rename changes the session's title, and the rail row, the grid pane and
 * the Sky line are all keyed by title, so each unmounts and mounts again
 * under the new name. Whatever of theirs had the keyboard (the row you
 * pressed F2 on, the line whose bar you renamed, the terminal the Rename
 * dialog returned to) goes with it and focus fell to <body>: the selection
 * followed the rename, the keyboard did not, so arrows, Delete and typing
 * stopped reaching the session.
 *
 * `focusSpot` notes which of the session's elements has the keyboard;
 * `keepFocusAcrossRename` puts it back on the same kind of element under the
 * new title once that is mounted — unless the person has moved on by then. */

/** The attributes that tie an element to a session's title. */
const ATTRS = ["data-title", "data-line"] as const;

export interface FocusSpot {
  attr: (typeof ATTRS)[number];
  /** The session element's tag and first class: the same kind of element is
   * looked for under the new title (a rail row, not the pane beside it). */
  tag: string;
  cls: string;
  /** The keyboard was in its terminal (xterm's input), not on the element. */
  term: boolean;
}

/** Which of `title`'s elements has the keyboard now, or null (none of them,
 * or no DOM at all). */
export function focusSpot(title: string): FocusSpot | null {
  if (!title || typeof document === "undefined") return null;
  const active = document.activeElement as HTMLElement | null;
  if (!active || active === document.body) return null;
  // The nearest ancestor naming THIS title: a pane holds rows naming other
  // sessions (its Thread's workers), so the first [data-title] is not enough.
  for (let el: HTMLElement | null = active; el; el = el.parentElement) {
    for (const attr of ATTRS) {
      if (el.getAttribute(attr) === title) {
        return {
          attr,
          tag: el.tagName,
          cls: el.classList[0] || "",
          term: active.classList.contains("xterm-helper-textarea"),
        };
      }
    }
  }
  return null;
}

/** The selected session's terminal: where the keyboard goes back to when a
 * rename started from something that is gone by now (a menu, the palette)
 * left it on the page — as picking the session puts it there. */
export const PANE_TERMINAL: FocusSpot = { attr: "data-title", tag: "SECTION", cls: "pane", term: true };

function findSpot(spot: FocusSpot, title: string): HTMLElement | null {
  for (const el of Array.from(document.querySelectorAll<HTMLElement>(`[${spot.attr}]`))) {
    if (el.getAttribute(spot.attr) !== title || el.tagName !== spot.tag) continue;
    if (spot.cls && !el.classList.contains(spot.cls)) continue;
    return el;
  }
  return null;
}

/** Once `to`'s element of the kind `spot` names is mounted, give it the
 * keyboard back (its terminal when that is where it was). Gives up after
 * `budgetMs`, and leaves focus alone when the person has put it somewhere
 * else meanwhile. A no-op without a spot or a DOM. */
export function keepFocusAcrossRename(spot: FocusSpot | null, to: string, budgetMs = 2000): void {
  if (!spot || !to || typeof document === "undefined" || typeof requestAnimationFrame !== "function") return;
  const was = document.activeElement;
  const until = Date.now() + budgetMs;
  const tick = () => {
    if (Date.now() > until) return;
    const active = document.activeElement as HTMLElement | null;
    const lost = !active || active === document.body || !active.isConnected;
    // Somewhere else on purpose (another row, a dialog, the composer): theirs.
    if (!lost && active !== was) return;
    const target = findSpot(spot, to);
    // Kept, not remounted (the element just took the new name): nothing to do.
    if (!lost && target && target.contains(active)) return;
    if (target && lost) {
      const into = spot.term ? target.querySelector<HTMLElement>(".xterm-helper-textarea") : target;
      if (into) {
        into.focus({ preventScroll: true });
        return;
      }
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}
