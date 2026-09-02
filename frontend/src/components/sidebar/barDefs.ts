/** Shared registry + ordering for the movable sidebar sections. The
 * customizable bars (Usage, Ticket Ingestion, PR Review, Issue Handling,
 * Assistant) are draggable; the session list is a fixed anchor (the
 * SESSIONS_KEY sentinel) that bars can be dropped above or below but which
 * never itself moves.
 *
 * Both the sidebar renderer and the footer Customize popover read from here so
 * the drag order and the menu order stay in lockstep. */

export interface BarDef {
  key: string;
  label: string;
}

/** The draggable, hideable bars — also the fallback order for a fresh user. */
export const SIDEBAR_BARS: BarDef[] = [
  { key: "usage", label: "Usage" },
  { key: "ingestion", label: "Ticket Ingestion" },
  { key: "pr-review", label: "PR Review" },
  { key: "issue-handling", label: "Issue Handling" },
  // Last of the automations, and after the three that START work: Verify is the
  // other end of the same pipeline — what came in through ingestion comes back
  // here once it has actually shipped.
  { key: "verify", label: "Verify" },
  { key: "assistant", label: "Assistant" },
];

/** Bars shown out of the box to a brand-new user. Ticket Ingestion is in here
 * because it is the headline feature — connecting a tracker is the first thing a
 * new user does, and hiding its bar meant they had to go find the product before
 * they could use it. The remaining three (PR Review, Issue Handling, Verify)
 * start hidden so a first run isn't overwhelming; they're one click away in the
 * footer Customize menu. Only applied when the user has never touched the
 * Customize menu (no persisted hiddenBars) — order and length carry no meaning,
 * this is only a membership set (see `defaultHiddenBars`). */
export const DEFAULT_VISIBLE_BARS = ["usage", "ingestion", "assistant"];

/** The default hidden set for a fresh user: every bar not in the essentials. */
export function defaultHiddenBars(): string[] {
  const visible = new Set(DEFAULT_VISIBLE_BARS);
  return SIDEBAR_BARS.map((b) => b.key).filter((k) => !visible.has(k));
}

/** Sentinel for the session-list block in the section order. Not a bar: it is
 * never draggable and never appears in the Customize menu. */
export const SESSIONS_KEY = "sessions";

/** Full orderable section list, default order: the bars, then the session
 * list. Bars can be reordered among themselves and moved below the sessions. */
export const DEFAULT_SECTION_ORDER: string[] = [
  ...SIDEBAR_BARS.map((b) => b.key),
  SESSIONS_KEY,
];

/** Key prefix for extension-contributed bars ("ext:" + extension id) — the
 * extra keys the resolvers below accept alongside the built-ins. */
export const EXT_BAR_PREFIX = "ext:";

/** Resolve a persisted order into the concrete section sequence: honour the
 * saved order, drop keys we no longer know, and append any section added since
 * the order was saved (including the sessions anchor for older saves).
 *
 * `extraKeys` are the extension bars currently installed. They count as known
 * (a saved order that mentions one keeps its place), but ones the saved order
 * has never seen are inserted immediately BEFORE the sessions anchor rather
 * than appended at the tail: a user with a years-old saved order would
 * otherwise get a brand-new extension's bar below the session list, where
 * nothing suggests it exists. Missing BUILT-IN keys keep the existing
 * tail-append so older saves resolve exactly as they always have. */
export function orderedSections(order: string[], extraKeys: string[] = []): string[] {
  const known = new Set([...DEFAULT_SECTION_ORDER, ...extraKeys]);
  const seen = new Set<string>();
  const out: string[] = [];
  for (const key of order) {
    if (known.has(key) && !seen.has(key)) {
      out.push(key);
      seen.add(key);
    }
  }
  for (const key of DEFAULT_SECTION_ORDER) {
    if (!seen.has(key)) {
      out.push(key);
      seen.add(key);
    }
  }
  const missingExtras = extraKeys.filter((key) => {
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  if (missingExtras.length) {
    // The built-in pass above guarantees the anchor is present.
    out.splice(out.indexOf(SESSIONS_KEY), 0, ...missingExtras);
  }
  return out;
}

/** The bar defs alone, in section order (drops the sessions anchor) — used by
 * the Customize menu so it mirrors the sidebar's live order. `extraDefs` are
 * the extension bars (key + label), resolved by the same rules as above. */
export function orderedBars(order: string[], extraDefs: BarDef[] = []): BarDef[] {
  const byKey = new Map([...SIDEBAR_BARS, ...extraDefs].map((b) => [b.key, b]));
  return orderedSections(order, extraDefs.map((b) => b.key))
    .map((key) => byKey.get(key))
    .filter((b): b is BarDef => !!b);
}
