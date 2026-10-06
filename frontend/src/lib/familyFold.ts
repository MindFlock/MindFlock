/** Folding an orchestrator's sub-sessions away in the sidebar.
 *
 * A folded parent keeps its own row — and its roll-up line ("2 of 3
 * reported · 1 needs you"), so nothing that wants you is hidden — while every
 * session below it (children, their children, …) leaves the rail. The fold is
 * applied to the rail entries BEFORE they are numbered and published as
 * railOrder, the way a folded run group or device is, so Alt+N / Ctrl+Tab can
 * never land on a row that is not drawn.
 *
 * Two exceptions keep the rail honest: a search filter shows every match
 * (the caller passes no folds while filtering), and the focused session is
 * never hidden — focusing a worker from its pane draws it under its parent
 * rather than leaving the rail with nothing selected. */

export interface FoldEntry {
  key: string;
  inst?: { title: string; parent?: string; device?: string } | undefined;
}

/** `list` without the entries hidden under a folded ancestor. An ancestor
 * counts only while it is itself on this rail (a parent filtered away or on
 * another device can't fold anything here); cycle-safe. */
export function hideFoldedFamilies<E extends FoldEntry>(
  list: E[],
  folded: ReadonlySet<string>,
  keep?: string | null
): E[] {
  if (!folded.size) return list;
  const parentOf = new Map<string, string>();
  for (const e of list) {
    if (e.inst && !e.inst.device && e.inst.parent) parentOf.set(e.inst.title, e.inst.parent);
  }
  const onRail = new Set(list.filter((e) => e.inst && !e.inst.device).map((e) => e.inst!.title));
  const hidden = (title: string): boolean => {
    const seen = new Set<string>([title]);
    let p = parentOf.get(title);
    while (p && onRail.has(p) && !seen.has(p)) {
      if (folded.has(p)) return true;
      seen.add(p);
      p = parentOf.get(p);
    }
    return false;
  };
  return list.filter((e) => !e.inst || e.inst.device || e.inst.title === keep || !hidden(e.inst.title));
}
