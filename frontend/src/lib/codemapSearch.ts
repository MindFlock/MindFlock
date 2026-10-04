/** The Map's find box logic (components/grid/codemap/SearchBox.tsx), kept
 * out of the component so the stale-answer rule can be pinned in vitest:
 * debounce the query, ask the server, and apply an answer ONLY if no newer
 * query — including clearing the box — happened since it was asked.
 *
 * The bug this shape prevents: clearing (Escape, or picking a row) used to
 * leave the in-flight request "current", so its late answer refilled the list
 * with results for a query the user had abandoned, and Enter on the next
 * keystroke opened one of them. */

import { errMsg } from "./format";

export interface SearchResult<T> {
  /** The trimmed query these items answer ("" = cleared). */
  q: string;
  items: T[];
  err: string;
}

export interface SearchController {
  /** The box's text changed. */
  set(q: string): void;
  /** Unmount / new session: nothing in flight may land afterwards. */
  dispose(): void;
}

export function searchController<T>(
  fetch: (q: string) => Promise<T[]>,
  apply: (r: SearchResult<T>) => void,
  delayMs = 160
): SearchController {
  let seq = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const stop = () => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
  };
  return {
    set(raw: string) {
      const q = raw.trim();
      // Every change — clearing included — makes whatever is in flight stale.
      const my = ++seq;
      stop();
      if (!q) {
        apply({ q: "", items: [], err: "" });
        return;
      }
      timer = setTimeout(async () => {
        timer = null;
        try {
          const items = await fetch(q);
          if (my === seq) apply({ q, items, err: "" });
        } catch (x) {
          if (my === seq) apply({ q, items: [], err: errMsg(x) });
        }
      }, delayMs);
    },
    dispose() {
      seq++;
      stop();
    },
  };
}
