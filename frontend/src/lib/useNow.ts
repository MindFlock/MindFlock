/** A clock for labels that count by themselves ("working · 6m", an invite's
 * countdown, "lost 12s ago"): re-renders the caller every `ms` while `ms` is
 * truthy, and returns Date.now() (milliseconds) as of THIS render.
 *
 * Reading the time at render — not the time of the last tick — means any
 * other re-render (fresh data, a click) gets a current clock too, so a caller
 * never has to bump a tick by hand after an action. Pass false/0 to stop
 * ticking while nothing is counting. */

import { useEffect, useReducer } from "react";

export function useNow(ms: number | false | null | undefined): number {
  const [, tick] = useReducer((n: number) => n + 1, 0);
  useEffect(() => {
    if (!ms) return;
    const t = setInterval(tick, ms);
    return () => clearInterval(t);
  }, [ms]);
  return Date.now();
}
