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
  { key: "assistant", label: "Assistant" },
];

/** Bars shown out of the box to a brand-new user — the two essentials. The
 * rest start hidden so a first run isn't overwhelming; they're one click away
 * in the footer Customize menu. Only applied when the user has never touched
 * the Customize menu (no persisted hiddenBars). */
export const DEFAULT_VISIBLE_BARS = ["usage", "assistant"];

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

const KNOWN = new Set(DEFAULT_SECTION_ORDER);
const BY_KEY = new Map(SIDEBAR_BARS.map((b) => [b.key, b]));

/** Resolve a persisted order into the concrete section sequence: honour the
 * saved order, drop keys we no longer know, and append any section added since
 * the order was saved (including the sessions anchor for older saves). */
export function orderedSections(order: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const key of order) {
    if (KNOWN.has(key) && !seen.has(key)) {
      out.push(key);
      seen.add(key);
    }
  }
  for (const key of DEFAULT_SECTION_ORDER) {
    if (!seen.has(key)) out.push(key);
  }
  return out;
}

/** The bar defs alone, in section order (drops the sessions anchor) — used by
 * the Customize menu so it mirrors the sidebar's live order. */
export function orderedBars(order: string[]): BarDef[] {
  return orderedSections(order)
    .map((key) => BY_KEY.get(key))
    .filter((b): b is BarDef => !!b);
}
