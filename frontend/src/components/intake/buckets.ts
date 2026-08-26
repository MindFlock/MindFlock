/** Which workflow-state buckets the Tickets tab is showing.
 *
 * Shared by the panel and by the tab strip's count, because they must agree.
 * They didn't: the badge counted every ticket the provider returned — a
 * lifetime total including everything ever Completed or Won't-do (1221 on the
 * author's Shortcut) — while the panel showed the actionable ones (52). A badge
 * that doesn't match the list under it is just wrong, so both now go through
 * `visibleBuckets` / `countInBuckets`.
 */

/** Bucket label for tickets whose provider doesn't report a workflow state.
 * Mirrors `NO_STATE_BUCKET` in `backend/web/core/ticket_start.py`. */
export const NO_STATE_BUCKET = "No state";

/** Which buckets the user wants in the panel. null (nothing saved yet) = show
 * the actionable ones; once they hide something the explicit list takes over. */
const BUCKETS_LS_KEY = "mf_ticket_buckets";

export function loadShownBuckets(): string[] | null {
  try {
    const raw = localStorage.getItem(BUCKETS_LS_KEY);
    if (raw === null) return null;
    const v = JSON.parse(raw);
    return Array.isArray(v) ? v.map(String) : null;
  } catch {
    return null;
  }
}

export function saveShownBuckets(v: string[] | null) {
  try {
    if (v === null) localStorage.removeItem(BUCKETS_LS_KEY);
    else localStorage.setItem(BUCKETS_LS_KEY, JSON.stringify(v));
  } catch {
    /* storage unavailable */
  }
}

/** The buckets on screen, in the provider's own workflow order.
 *
 * Nothing chosen yet: the actionable ones. Done-type buckets (Completed, Won't
 * do, …) typically dwarf them — thousands of rows against dozens — so they
 * start parked behind the "+ Add bucket…" menu rather than burying the work. */
export function visibleBuckets(
  buckets: string[],
  doneBuckets: string[],
  shown: string[] | null
): string[] {
  return shown === null
    ? buckets.filter((b) => !doneBuckets.includes(b))
    : buckets.filter((b) => shown.includes(b));
}

/** Whether the panel is narrowed to your own tickets. A source set to ingest
 * anyone's tickets lists other people's work by design; this is the way back to
 * just yours. null (nothing saved) = show everything the sources returned. */
const MINE_LS_KEY = "mf_ticket_mine_only";

export function loadMineOnly(): boolean {
  try {
    return localStorage.getItem(MINE_LS_KEY) === "1";
  } catch {
    return false;
  }
}

export function saveMineOnly(v: boolean) {
  try {
    if (v) localStorage.setItem(MINE_LS_KEY, "1");
    else localStorage.removeItem(MINE_LS_KEY);
  } catch {
    /* storage unavailable */
  }
}

/** The tickets on screen: in a visible bucket, and yours when narrowed to
 * yours. The one place both the panel and the tab badge ask, because they have
 * to agree — see the header. `mine` is absent on older payloads, which only
 * ever contained your own tickets, so undefined reads as yours. */
export function shownTickets<T extends { bucket?: string; mine?: boolean }>(
  tickets: T[],
  visible: string[],
  mineOnly: boolean
): T[] {
  const set = new Set(visible);
  return tickets.filter(
    (t) =>
      set.has(t.bucket || NO_STATE_BUCKET) && (!mineOnly || t.mine !== false)
  );
}

/** How many of `tickets` fall in `visible` — the number the tab badge shows,
 * which is therefore the number of rows you'll find when you open it. */
export function countInBuckets(
  tickets: Array<{ bucket?: string; mine?: boolean }>,
  visible: string[],
  mineOnly = false
): number {
  return shownTickets(tickets, visible, mineOnly).length;
}
