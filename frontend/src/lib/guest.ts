/** Guest links (docs/people.md, backend.web.core.guests): this browser may be
 * signed in with a link that opens a few of the owner's sessions. The server
 * then lists every other session as a crossed-out stub (`locked: true`, title
 * and status only), and answers every call on it with a 403.
 *
 * The stubs must reach exactly one place — the rail, which draws them crossed
 * out — and never the grid (it would open a terminal socket that closes at
 * once), the prompt-target pickers, the bell, the New dialog's duplicate
 * check… So `useInstances()` strips them on the way in (`splitLockedRows`)
 * and parks them here; the rail reads them back with `useLockedRows()`. */

import { useSyncExternalStore } from "react";
import type { Instance } from "../api/types";

export interface LockedRow {
  title: string;
  status: string;
  device?: string;
}

let locked: LockedRow[] = [];
const listeners = new Set<() => void>();

/** Split a `GET /api/instances` list: the usable rows come back, the locked
 * stubs are stored for `useLockedRows()`. Identity-stable when nothing is
 * locked (the owner's every poll) so subscribers don't re-render for nothing. */
export function splitLockedRows(list: Instance[]): Instance[] {
  if (!Array.isArray(list)) {
    setLocked([]);
    return list;
  }
  if (!list.some((i) => i.locked)) {
    if (locked.length) setLocked([]);
    return list;
  }
  const keep: Instance[] = [];
  const out: LockedRow[] = [];
  for (const i of list) {
    if (i.locked) out.push({ title: i.title, status: i.status, device: i.device });
    else keep.push(i);
  }
  setLocked(out);
  return keep;
}

function setLocked(rows: LockedRow[]): void {
  if (
    rows.length === locked.length &&
    rows.every((r, i) => r.title === locked[i].title && r.status === locked[i].status && r.device === locked[i].device)
  )
    return;
  locked = rows;
  for (const l of listeners) l();
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

function snapshot(): LockedRow[] {
  return locked;
}

/** The crossed-out rows of a guest's rail (empty for the owner). */
export function useLockedRows(): LockedRow[] {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}

/** The stubs as last split (what useLockedRows renders), for tests. */
export function lockedRows(): LockedRow[] {
  return locked;
}

/** Test seam. */
export function _resetLockedRows(): void {
  setLocked([]);
}

/** What the rail's guest note says: who the owner made the link for and how
 * much of the rail it opens. */
export function guestNote(guest: { label: string; name?: string; sessions: string[] } | null | undefined): string {
  if (!guest) return "";
  const n = guest.sessions.length;
  const where = n === 1 ? "one session" : `${n} sessions`;
  const who = guest.name || guest.label;
  return who ? `You're here as ${who} — ${where}` : `Guest link — ${where}`;
}
