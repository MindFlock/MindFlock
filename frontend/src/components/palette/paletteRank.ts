/** How the command palette ranks what it offers — pure, so vitest covers it.
 *
 *  - An EMPTY query is a short menu, not the whole index: the few commands a
 *    person reaches for (New, the sessions they last looked at, Intake,
 *    Verify, Schedule, Setup, Settings, help) and one "All commands…" row. Sixty rows
 *    on open read as a wall.
 *  - A typed query matches labels AND a few plain synonyms ("help" finds the
 *    shortcuts and the tour, "install" finds Setup and the Doctor).
 *  - An OUTWARD verb — Commit, Push, Open PR, Merge, Close, Delete — is never
 *    the Enter default: typing a session's name highlights "Focus: <name>",
 *    and Push sits below it. One Enter must never push code.
 *  - "Join with code …" is offered only for something shaped like a real
 *    code (an `mfp1:`/`mfp2:` invite, or a dashed XXXX-XXXX device code) —
 *    "worktree" or "parallel" are words, not codes. */

import { routeCode } from "../../lib/fleet";
import { extractPeerCode } from "../../lib/peer";

export interface PaletteAction {
  label: string;
  hint?: string;
  run(): void;
  /** Extra words that should find it (beyond its label). */
  keywords?: string;
  /** Acts outside this screen (git, a remote, ending a session): never the
   * Enter default for a query that also matches something harmless. */
  outward?: boolean;
  /** On the empty-query menu, at this rank (lower first). */
  top?: number;
  /** Running it keeps the palette open (the "All commands…" row). */
  keepOpen?: boolean;
}

/** Contiguous substring beats a spread-out subsequence; earlier/tighter
 * matches rank higher. -1 = no match. (Port of fuzzyScore.) */
export function fuzzyScore(query: string, text: string): number {
  query = String(query || "").toLowerCase();
  text = String(text || "").toLowerCase();
  if (!query) return 0;
  const sub = text.indexOf(query);
  if (sub >= 0) return sub;
  let ti = 0,
    score = 1000; // any subsequence ranks below every substring
  for (const ch of query) {
    const at = text.indexOf(ch, ti);
    if (at < 0) return -1;
    score += at - ti;
    ti = at + 1;
  }
  return score;
}

/** Plain words people type for a command, by the command's label prefix.
 * The label still wins: a synonym hit ranks after every label substring. */
export const SYNONYMS: Array<[string, string]> = [
  ["New session", "create start add agent task"],
  ["Keyboard shortcuts", "help keys hotkeys keyboard"],
  ["Tour", "help intro welcome guide how getting started"],
  ["Open Setup checklist", "install setup get started onboarding sign in login"],
  ["Open Doctor", "install fix health check dependencies missing tools"],
  ["Open Settings", "preferences options config configure"],
  ["Recently closed", "undo reopen restore closed"],
  ["Open Intake", "tickets inbox jira linear shortcut github issues"],
  ["Verify", "checklist test qa check"],
  ["Toggle sidebar", "hide show panel"],
  ["Customize", "bars sidebar layout"],
  ["Select sessions", "bulk several many"],
  ["Focus:", "go open switch show"],
];

function synonymsFor(label: string): string {
  for (const [prefix, words] of SYNONYMS) if (label.startsWith(prefix)) return words;
  return "";
}

/** The menu an empty query shows: the `top` entries, best first, at most
 * this many, then "All commands…". */
export const TOP_LIMIT = 9;

/** The palette's rows for a query. `showAll` lists every command for an
 * empty query (after "All commands…"). `allRow` builds that row. */
export function rankActions(
  actions: PaletteAction[],
  query: string,
  opts: { showAll?: boolean; allRow?: (count: number) => PaletteAction } = {}
): PaletteAction[] {
  const q = String(query || "").trim();
  if (!q) {
    if (opts.showAll || !opts.allRow) return actions.slice();
    const top = actions
      .map((a, i) => ({ a, i }))
      .filter((x) => x.a.top !== undefined)
      .sort((x, y) => (x.a.top as number) - (y.a.top as number) || x.i - y.i)
      .slice(0, TOP_LIMIT)
      .map((x) => x.a);
    return top.concat(opts.allRow(actions.length));
  }
  const scored: Array<{ a: PaletteAction; i: number; s: number }> = [];
  actions.forEach((a, i) => {
    let s = fuzzyScore(q, a.label);
    const words = [a.keywords || "", synonymsFor(a.label)].join(" ").trim();
    if (words) {
      // A synonym is a whole-word hit only ("help", not "elp"), after every
      // label substring and before any spread-out label subsequence.
      // A query with a space ("every day") matches as a phrase.
      const lw = words.toLowerCase();
      const lq = q.toLowerCase();
      const hit = /\s/.test(lq.trim())
        ? lw.includes(lq.trim())
        : lw.split(/\s+/).some((w) => w.startsWith(lq));
      if (hit && (s < 0 || s >= 1000)) s = 500;
    }
    if (s >= 0) scored.push({ a, i, s });
  });
  // Equal scores: harmless before outward, then the builder's order.
  scored.sort((x, y) => x.s - y.s || Number(!!x.a.outward) - Number(!!y.a.outward) || x.i - y.i);
  const out = scored.map((x) => x.a);
  // Never an outward verb as the Enter default while anything harmless
  // matched at all: the best harmless match goes first.
  if (out.length && out[0].outward) {
    const k = out.findIndex((a) => !a.outward);
    if (k > 0) out.unshift(out.splice(k, 1)[0]);
  }
  return out;
}

/** A device-pairing code or a peer invite in what was typed — only the real
 * shapes: `mfp1:`/`mfp2:` anywhere, a dashed XXXX-XXXX code (bare, after a
 * device name, or in `mindflock devices join …`). An eight-letter word is not
 * a code. */
export function paletteCode(query: string): ReturnType<typeof routeCode> {
  const none = { kind: "" as const, code: "", device: "" };
  const t = String(query || "").trim();
  if (!t) return none;
  if (extractPeerCode(t)) return routeCode(t);
  if (!/(^|\s)[A-Za-z0-9]{4}-[A-Za-z0-9]{4}(\s|$)/.test(t)) return none;
  return routeCode(t);
}

/** Whether a row's hint is a key ("Ctrl+B", "?", "Alt+N") — drawn as a
 * keycap — rather than words ("fresh session, prompt sent"), which are drawn
 * as plain muted text so they never read as a shortcut. A hint is words as
 * soon as it holds an all-lowercase word of three letters or more. */
export function isKeyHint(hint: string): boolean {
  return !/\b[a-z]{3,}\b/.test(hint);
}
