/** The rail follows the focus: when the focused session changes — a voice
 * switch ("switch to…", "session 3", "next session", "go back"), a keyboard
 * switch (Alt+N, Ctrl+Tab), the sky — and its row is outside the rail's
 * visible scroll area, the rail scrolls just enough to show it
 * (`scrollIntoView({block: "nearest"})`, smooth unless the person asked for
 * reduced motion). A row already fully in view never moves, and a row the
 * person just clicked never jumps (a click on a half-visible row is theirs).
 * A run group or a device section folded over the row is unfolded first —
 * the rail's own toggles, so the person can fold it again.
 *
 * A VOICE switch also pulses the row once (~1 s, `.voice-pulse`, a static
 * highlight under reduced motion) so the eye finds it — the store's
 * `railPulse`, set by selectSession(…, {voice: true}).
 *
 * Mounted where the rail renders (Sidebar.tsx, {@link useRailFollow}), so
 * every way of switching benefits. The pure parts ({@link foldOver},
 * {@link outOfView}) are pinned by tests (vitest runs in node: no DOM). */

import { useEffect, useRef } from "react";

/** How long the pulse class stays on a row (its CSS animation is ~1 s). */
export const PULSE_MS = 1100;
/** A focus change this soon after a press inside the rail is the click's:
 * the rail never scrolls under the pointer. */
export const CLICK_GRACE_MS = 600;
/** How many frames a scroll waits for a just-unfolded row to render. */
const FIND_FRAMES = 4;

/** A section of the rail that can fold its rows away. */
export interface RailFold {
  /** "run": a run group (store.toggleRunCollapsed); "device": a device or
   * peer section (store.toggleDeviceCollapsed). */
  kind: "run" | "device";
  /** The run id, or the device key ("__self", a device name, "peer:<name>"). */
  id: string;
  collapsed: boolean;
  /** The keys of the rows under it (folded or not). */
  keys: readonly string[];
}

/** The folded sections holding `key` (outermost first: a device before a
 * run group inside it), or [] when its row is on the rail. */
export function foldOver(key: string, folds: readonly RailFold[]): RailFold[] {
  if (!key) return [];
  const hit = folds.filter((f) => f.collapsed && f.keys.includes(key));
  return [...hit.filter((f) => f.kind === "device"), ...hit.filter((f) => f.kind === "run")];
}

/** Whether a row (its top and bottom) is not wholly inside the view (the
 * scroll container's top and bottom): only then does the rail scroll. */
export function outOfView(row: { top: number; bottom: number }, view: { top: number; bottom: number }): boolean {
  return row.top < view.top - 0.5 || row.bottom > view.bottom + 0.5;
}

/** Reduced motion asked for (no smooth scroll, no animated pulse). */
export function reducedMotion(): boolean {
  try {
    return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

/** The rail row for `key` — compared by attribute value, never a CSS
 * selector: a window's key starts with U+0000, which no selector matches. */
export function railRowEl(key: string): HTMLElement | null {
  if (typeof document === "undefined") return null;
  const list = document.getElementById("instance-list");
  if (!list) return null;
  for (const el of Array.from(list.querySelectorAll<HTMLElement>("li[data-title]")))
    if (el.getAttribute("data-title") === key) return el;
  return null;
}

/** The nearest ancestor of `el` that scrolls vertically (the rail's list). */
function scroller(el: HTMLElement): HTMLElement | null {
  for (let p = el.parentElement; p; p = p.parentElement) {
    const oy = getComputedStyle(p).overflowY;
    if ((oy === "auto" || oy === "scroll") && p.scrollHeight > p.clientHeight) return p;
  }
  return null;
}

/** Scroll the rail so `el` is wholly in view (nothing when it is). */
export function revealRow(el: HTMLElement): boolean {
  const box = scroller(el);
  const view = box ? box.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
  if (!outOfView(el.getBoundingClientRect(), view)) return false;
  el.scrollIntoView({ block: "nearest", inline: "nearest", behavior: reducedMotion() ? "auto" : "smooth" });
  return true;
}

const pulseTimers = new WeakMap<HTMLElement, ReturnType<typeof setTimeout>>();
/** Pulse `el` once (restarted when it is already pulsing). */
export function pulseRow(el: HTMLElement): void {
  clearTimeout(pulseTimers.get(el));
  el.classList.remove("voice-pulse");
  void el.offsetWidth; // restart the animation
  el.classList.add("voice-pulse");
  pulseTimers.set(
    el,
    setTimeout(() => el.classList.remove("voice-pulse"), PULSE_MS)
  );
}

let lastRailPress = 0;
let pressWired = false;
/** Notes presses inside the rail ({@link CLICK_GRACE_MS}). */
function wirePress(): void {
  if (pressWired || typeof document === "undefined") return;
  pressWired = true;
  document.addEventListener(
    "pointerdown",
    (e) => {
      const t = e.target as Element | null;
      if (t?.closest?.("#instance-list")) lastRailPress = Date.now();
    },
    true
  );
}

/** Find `key`'s row over the next few frames (an unfolded section renders
 * it a frame later), then `fn` it. */
function withRow(key: string, fn: (el: HTMLElement) => void): () => void {
  let left = FIND_FRAMES;
  let raf = 0;
  const tick = () => {
    const el = railRowEl(key);
    if (el) return fn(el);
    if (--left > 0) raf = requestAnimationFrame(tick);
  };
  raf = requestAnimationFrame(tick);
  return () => cancelAnimationFrame(raf);
}

/** Sidebar's half: follow `focused` (and a window brought forward, `head`:
 * the MRU's head when it is a window row), unfold what hides it, pulse a
 * voice switch's row. `folds`: the rail's foldable sections now; `unfold`:
 * open one. */
export function useRailFollow(o: {
  focused: string | null;
  head: string | null;
  pulse: { key: string; n: number } | null;
  folds: readonly RailFold[];
  unfold(f: RailFold): void;
}): void {
  const foldsRef = useRef(o.folds);
  foldsRef.current = o.folds;
  const unfoldRef = useRef(o.unfold);
  unfoldRef.current = o.unfold;
  useEffect(wirePress, []);

  const follow = (key: string, pulse: boolean, unfold = true) => {
    if (unfold) for (const f of foldOver(key, foldsRef.current)) unfoldRef.current(f);
    const clicked = !pulse && Date.now() - lastRailPress < CLICK_GRACE_MS;
    return withRow(key, (el) => {
      if (!clicked) revealRow(el);
      if (pulse) pulseRow(el);
    });
  };

  // Every focus change (a voice switch's pulse comes on top, below). The
  // first focus of the page (a reload) scrolls but never unfolds what the
  // person folded.
  const seen = useRef(false);
  useEffect(() => {
    if (!o.focused) return;
    const first = !seen.current;
    seen.current = true;
    return follow(o.focused, false, !first);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [o.focused]);
  // A window brought forward (selectWindow: the MRU's head).
  useEffect(() => {
    if (!o.head) return;
    return follow(o.head, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [o.head]);
  // A voice switch: scroll and pulse, even onto the row already focused.
  useEffect(() => {
    if (!o.pulse) return;
    return follow(o.pulse.key, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [o.pulse?.n]);
}
