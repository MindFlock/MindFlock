/** "Start together" picks on the Tickets tab: only a ticket the list still
 * offers to start — no session here or on another device — takes a pick,
 * and a pick counts only while its ticket is listed so. */

export interface Pickable {
  has_session?: boolean;
  elsewhere?: unknown;
}

/** Whether a row can be picked to start together. */
export function canPick(t: Pickable): boolean {
  return !t.has_session && !t.elsewhere;
}

/** The picked tickets that can still start, in pick order, as they are listed
 * now; `listed` null (not read yet) keeps the picks as they are. */
export function startablePicks<T extends Pickable>(picked: ReadonlyMap<string, T>, listed: readonly T[] | null | undefined, key: (t: T) => string): T[] {
  if (!listed) return [...picked.values()];
  const now = new Map(listed.map((t) => [key(t), t]));
  const out: T[] = [];
  for (const k of picked.keys()) {
    const t = now.get(k);
    if (t && canPick(t)) out.push(t);
  }
  return out;
}
