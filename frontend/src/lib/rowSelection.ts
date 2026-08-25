/** Checkbox multi-select for the list dialogs (Recently closed, Workspaces on
 * disk) — the pure half, so the ordering and shift-range rules can be tested
 * without a DOM.
 *
 * Selection is pruned lazily against the live key list rather than in an effect:
 * these lists reload after every action, and a row that vanished must stop
 * counting toward "3 selected" the moment it's gone. Same trick the sidebar's
 * BulkBar uses. */

export type SelectAllState = "none" | "some" | "all";

/** Selected keys that still exist, in LIST order — not click order. A bulk
 * confirmation reads like the list it came from, which is how the user checks
 * they picked the right rows. */
export function selectedInOrder(selected: Set<string>, allKeys: string[]): string[] {
  return allKeys.filter((k) => selected.has(k));
}

/** Turn a set of keys on or off, returning a new Set (React needs the identity
 * change) or the SAME set when nothing moved. */
export function applyKeys(selected: Set<string>, keys: string[], on: boolean): Set<string> {
  const next = new Set(selected);
  for (const k of keys) {
    if (on) next.add(k);
    else next.delete(k);
  }
  if (next.size === selected.size) return selected;
  return next;
}

/** Header-checkbox tri-state over the rows currently on screen. "none" for an
 * empty list, so the header box of an empty list isn't ticked. */
export function selectAllState(selected: Set<string>, visibleKeys: string[]): SelectAllState {
  if (!visibleKeys.length) return "none";
  let n = 0;
  for (const k of visibleKeys) if (selected.has(k)) n++;
  if (!n) return "none";
  return n === visibleKeys.length ? "all" : "some";
}

/** Inclusive shift-click range between two visible rows, in either direction.
 * An anchor that has scrolled out of the filtered list degenerates to just the
 * target — better than selecting a range the user can't see. */
export function rangeBetween(visibleKeys: string[], anchor: string, target: string): string[] {
  const a = visibleKeys.indexOf(anchor);
  const b = visibleKeys.indexOf(target);
  if (a < 0 || b < 0) return b < 0 ? [] : [target];
  return visibleKeys.slice(Math.min(a, b), Math.max(a, b) + 1);
}

/** "and 4 more" tail for a confirmation that lists what it's about to destroy.
 * Every name would make the dialog unreadable (and unscrollable, in a native
 * confirm); no names at all makes it unverifiable. */
export function previewList(names: string[], limit = 8): string {
  const head = names.slice(0, limit).map((n) => "  • " + n);
  const rest = names.length - head.length;
  if (rest > 0) head.push(`  • …and ${rest} more`);
  return head.join("\n");
}
