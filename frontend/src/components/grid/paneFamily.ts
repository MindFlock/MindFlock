/** What a pane needs from the WHOLE instances list, and nothing more: its
 * family (parent + workers, for the Thread tab and its badge) and its rank by
 * memory among this machine's sessions (the memory chip's colour).
 *
 * A pane used to call useInstances() for this, so every 4 s poll re-rendered
 * every pane even when nothing about its family had moved. usePaneFamily
 * reads the same cache entry, but only re-renders the pane when the derived
 * value changes: the instances query keeps unchanged rows' identity across
 * polls (structural sharing), so "same children, same objects" is the test. */
import { useCallback, useRef, useSyncExternalStore } from "react";
import type { Instance } from "../../api/types";
import { queryClient } from "../../state/queries";
import { familyOf } from "../../lib/thread";
import { memoryRank } from "../../lib/sessionMemory";
import { isPeerRow } from "../../lib/peerSessions";

export interface PaneFamily {
  parent: string;
  children: Instance[];
  /** This session's index by memory among local sessions, -1 when unranked. */
  memRank: number;
}

export function derivePaneFamily(title: string, rows: readonly Instance[]): PaneFamily {
  const { parent, children } = familyOf(title, rows);
  const memRank = memoryRank(
    rows.filter((r) => !r.device && !isPeerRow(r)),
    title
  );
  return { parent, children, memRank };
}

/** Equal when nothing a pane draws from it differs: same parent, same rank,
 * the same child rows (by identity — an updated row is a new object). */
export function samePaneFamily(a: PaneFamily, b: PaneFamily): boolean {
  if (a === b) return true;
  if (a.parent !== b.parent || a.memRank !== b.memRank) return false;
  if (a.children.length !== b.children.length) return false;
  return a.children.every((c, i) => c === b.children[i]);
}

/** A snapshot getter that hands back the PREVIOUS value while it is still
 * equal, so useSyncExternalStore sees no change and skips the render. */
export function familySnapshot(title: string, read: () => readonly Instance[] | undefined) {
  let lastRows: readonly Instance[] | undefined | null = null;
  let last: PaneFamily | null = null;
  return (): PaneFamily => {
    const rows = read();
    if (last && rows === lastRows) return last;
    const next = derivePaneFamily(title, rows ?? []);
    lastRows = rows;
    if (last && samePaneFamily(last, next)) return last;
    last = next;
    return next;
  };
}

const readRows = () => queryClient.getQueryData<Instance[]>(["instances"]);

function subscribeInstances(cb: () => void): () => void {
  return queryClient.getQueryCache().subscribe((event) => {
    if (event.query.queryKey[0] === "instances") cb();
  });
}

export function usePaneFamily(title: string): PaneFamily {
  const ref = useRef<{ title: string; get: () => PaneFamily } | null>(null);
  if (!ref.current || ref.current.title !== title) {
    ref.current = { title, get: familySnapshot(title, readRows) };
  }
  const get = useCallback(() => ref.current!.get(), []);
  return useSyncExternalStore(subscribeInstances, get, get);
}
