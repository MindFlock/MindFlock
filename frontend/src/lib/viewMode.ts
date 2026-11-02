/** The main area's two views — the grid and the sky — and every way between
 * them (docs/web-ui.md "Two views: the grid and the sky").
 *
 * The sky is the grid drawn another way: the same sessions, the same focused
 * one (`useUi.focused` is the only selection either view has), the same open
 * tab per session (`lastTab`, which the pane and a line's opened box share).
 * Switching keeps all of that; this module only flips `useUi.view` and makes
 * the flip look like one picture turning into the other:
 *
 *   setView / toggleView   the top bar's Grid · Sky toggle, the sidebar View
 *                          row's Sky chip, Ctrl+Y, the palette
 *   showInSky              a session's "Show in the Sky": the sky on it alone
 *   newSession             Ctrl/Alt+N and the top bar's New: the New dialog
 *                          in the grid, a new-session card mid-sky in the sky
 *
 * The transition (installViewFlip): the store change is seen synchronously,
 * before React paints the other view, so each session's rectangle on screen
 * (its pane, or its line) is measured then; once the other view has laid out,
 * a ghost of each flies from the old rectangle to the new one (~250 ms) while
 * the new view fades in. `prefers-reduced-motion`: neither. Terminals are
 * never re-attached: they live in lib/terminals' registry, and a pane that
 * mounts again (in the other view) adopts the same xterm. */

import { useUi, type MainView } from "../state/store";

export const VIEWS: readonly MainView[] = ["grid", "sky"];

export const viewLabel = (v: MainView) => (v === "sky" ? "Sky" : "Grid");

/** The toggle's and the palette's words for a view. */
export const VIEW_BLURB: Record<MainView, string> = {
  grid: "Every session as a pane — the terminal, its tabs, side by side",
  sky: "Every session as a line on one canvas — where each one is, and what is next",
};

export function setView(v: MainView) {
  useUi.getState().setView(v);
}

export function toggleView() {
  setView(useUi.getState().view === "sky" ? "grid" : "sky");
}

/** The sky on one session alone ("This session") — selects it, like
 * clicking its row. */
export function showInSky(title: string) {
  useUi.getState().showSky(title);
}

// --- The sky's own hooks (SkyView registers them while mounted) ---------------

export interface SkyHost {
  /** Open a new-session card in the middle of the view. */
  newHere(): void;
  /** Laid out and fitted: line rectangles can be read. */
  ready(): boolean;
  /** Each drawn line's rectangle on screen (client px). */
  lineRects(): Map<string, Box>;
}

let _sky: SkyHost | null = null;

export function registerSkyHost(h: SkyHost): () => void {
  _sky = h;
  return () => {
    if (_sky === h) _sky = null;
  };
}

/** Ctrl/Alt+N and New: in the sky, a card where you are looking (the sky's
 * New dialog); anywhere else, or on a phone-narrow window, the dialog. */
export function newSession() {
  const s = useUi.getState();
  if (s.view === "sky" && !s.narrow && _sky && !s.openDialog) _sky.newHere();
  else s.openDialogFor("new-session");
}

// --- The transition --------------------------------------------------------------

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** How long a ghost flies; the incoming view's fade is shorter (Sky.css). */
export const FLIP_MS = 250;
const MAX_GHOSTS = 12;

export function reducedMotion(): boolean {
  try {
    return typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

const boxOf = (r: DOMRect): Box => ({ x: r.left, y: r.top, w: r.width, h: r.height });
const visible = (b: Box) =>
  b.w > 4 && b.h > 4 && b.x + b.w > 0 && b.y + b.h > 0 && b.x < (window.innerWidth || 0) && b.y < (window.innerHeight || 0);

/** Each session pane's rectangle in the grid, by title. */
export function paneRects(root: ParentNode = document): Map<string, Box> {
  const out = new Map<string, Box>();
  for (const el of root.querySelectorAll<HTMLElement>(".pane[data-title]")) {
    const t = el.getAttribute("data-title") || "";
    if (t && !t.startsWith("\u0000")) out.set(t, boxOf(el.getBoundingClientRect()));
  }
  return out;
}

/** The pairs to fly: sessions on screen in both views, the focused one first
 * (it is the one the eye follows), capped. Pure, for the test. */
export function flipPairs(from: Map<string, Box>, to: Map<string, Box>, focused: string | null, max = MAX_GHOSTS): Array<{ title: string; from: Box; to: Box }> {
  const titles = [...from.keys()].filter((t) => to.has(t) && visible(from.get(t)!) && visible(to.get(t)!));
  titles.sort((a, b) => (a === focused ? -1 : b === focused ? 1 : 0));
  return titles.slice(0, max).map((title) => ({ title, from: from.get(title)!, to: to.get(title)! }));
}

function fly(pairs: ReturnType<typeof flipPairs>, focused: string | null) {
  for (const p of pairs) {
    const g = document.createElement("div");
    g.className = "view-ghost" + (p.title === focused ? " is-focused" : "");
    g.setAttribute("aria-hidden", "true");
    const at = (b: Box) => ({ left: b.x + "px", top: b.y + "px", width: b.w + "px", height: b.h + "px" });
    Object.assign(g.style, at(p.from));
    document.body.appendChild(g);
    const done = () => g.remove();
    if (typeof g.animate !== "function") {
      done();
      continue;
    }
    const a = g.animate([{ ...at(p.from), opacity: 0.9 }, { ...at(p.to), opacity: 0 }], { duration: FLIP_MS, easing: "cubic-bezier(.2,.7,.2,1)" });
    a.onfinish = done;
    a.oncancel = done;
  }
}

/** Wait (a frame at a time, up to ~20) until `ok()`, then run `go`. */
function whenReady(ok: () => boolean, go: () => void, frames = 20) {
  const step = (left: number) => {
    if (ok()) go();
    else if (left > 0) requestAnimationFrame(() => step(left - 1));
  };
  requestAnimationFrame(() => step(frames));
}

/** The grid's own scroll, kept across a trip to the sky. */
let gridScroll = 0;

/** Watch the store for a view switch and play it; returns the unhook. */
export function installViewFlip(): () => void {
  return useUi.subscribe((s, prev) => {
    if (s.view === prev.view || typeof document === "undefined") return;
    const toSky = s.view === "sky";
    const grid = document.getElementById("grid");
    if (toSky && grid) gridScroll = grid.scrollTop;
    const calm = reducedMotion();
    // Measured NOW: React has not painted the other view yet.
    const from = calm ? new Map<string, Box>() : toSky ? paneRects() : _sky?.lineRects() || new Map<string, Box>();
    // The fade starts on the next frame — the incoming view is committed by
    // then but not painted, and the outgoing one is gone (adding it now would
    // fade the old view in for a frame first).
    requestAnimationFrame(() => {
      if (calm) return;
      document.body.classList.add("view-switch");
      setTimeout(() => document.body.classList.remove("view-switch"), FLIP_MS + 40);
    });
    whenReady(
      () => (toSky ? !!_sky?.ready() : !!document.getElementById("grid")),
      () => {
        if (!toSky) {
          const g = document.getElementById("grid");
          if (g && gridScroll) g.scrollTop = gridScroll;
        }
        if (calm || !from.size) return;
        const to = toSky ? _sky?.lineRects() || new Map<string, Box>() : paneRects();
        fly(flipPairs(from, to, useUi.getState().focused), useUi.getState().focused);
      }
    );
  });
}
