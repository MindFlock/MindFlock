/** A click OR a double-click places — once (docs/flights.md "Round 14" → "The
 * palette fills itself"). The owner double-clicks a block to add it, as in any
 * diagram tool; a plain click places too. A double-click is two clicks and a
 * dblclick to the browser, so a bare onClick would place twice. Here a click
 * waits `delay` ms and a second click cancels it; the dblclick then places
 * once. A keyboard click (Enter / Space on a button: `detail` 0) places at
 * once.
 *
 * Two slow clicks (past `delay` but inside the OS's double-click time) still
 * fire a dblclick after the first click has placed: a dblclick within `pair`
 * ms of the click that placed is that pair's, and places nothing more. */

import { useEffect, useRef } from "react";

export interface ClickOrDouble {
  /** A click: `detail` 0 (the keyboard) places now; else it waits. */
  onClick(e?: { detail?: number }): void;
  onDoubleClick(): void;
  /** Drop a waiting click (unmount, a drag began). */
  cancel(): void;
}

export function clickOrDouble(place: () => void, { delay = 250, pair = 500 }: { delay?: number; pair?: number } = {}): ClickOrDouble {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let armedAt = 0;
  let placedFrom = -Infinity;
  const cancel = () => {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
  };
  return {
    onClick(e) {
      if (e && e.detail === 0) {
        cancel();
        return place();
      }
      // The second click of a double: the dblclick that follows places.
      if (timer !== undefined) return cancel();
      armedAt = Date.now();
      timer = setTimeout(() => {
        timer = undefined;
        placedFrom = armedAt;
        place();
      }, delay);
    },
    onDoubleClick() {
      cancel();
      if (Date.now() - placedFrom < pair) return;
      placedFrom = -Infinity;
      place();
    },
    cancel,
  };
}

/** `clickOrDouble` for a component: one per mount (a waiting click survives a
 * re-render), always calling the newest `place`, cancelled on unmount. No
 * `place` → the handlers do nothing. */
export function useClickOrDouble(place: (() => void) | undefined, opts?: { delay?: number; pair?: number }): ClickOrDouble {
  const latest = useRef(place);
  latest.current = place;
  const cd = useRef<ClickOrDouble | null>(null);
  if (!cd.current) cd.current = clickOrDouble(() => latest.current?.(), opts);
  useEffect(() => () => cd.current?.cancel(), []);
  return cd.current;
}
