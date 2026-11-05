/** Shell-style Tab completion for a folder field — the owner's "auto-fill the
 * next string by hitting Tab, like a Linux terminal". Pure: the field asks
 * /api/browse for the folder named by `splitForCompletion(text).dir` and hands
 * the entry names to `completeFrom`; a second Tab on an ambiguous partial walks
 * the matches with `cycle`.
 *
 * The rules, as bash has them where they make sense for a path you are about
 * to hand a server:
 * - The text is split at its LAST `/`: what is before it is the folder to list,
 *   what is after it is the partial name. `~/Mind` lists `~` for `Mind`;
 *   `/home/me/` lists `/home/me` for everything; `/` lists the root.
 * - A bare `~` completes to `~/` (a shell does the same).
 * - Text that starts with neither `/` nor `~` (`Mi`, `src/ap`) is read from
 *   home: `dir` is `""` (the server's default, home) or `~/src`, and the
 *   completion is written back as `~/…`. The server would resolve a relative
 *   path against ITS working directory, which is never what anyone typing a
 *   folder meant, so the result is made explicit.
 * - Matching is by prefix, exact case first; only when no name has the exact
 *   case does it fall back to ignoring case (so `mind` finds `MindFlock`, but
 *   `Mind` never offers `mindmap` beside `MindFlock`).
 * - Hidden folders (a leading `.`) are offered only when the partial starts
 *   with `.`.
 * - One match fills the whole name plus a trailing `/`, so Tab, Tab, Tab walks
 *   down the tree; several fill their longest common prefix and are returned
 *   as `matches` for the field to show. */

import { isWindowsFullPath, serverIsWindows } from "./serverPaths";

/** The separator a completed folder name gets: "\\" after a Windows path
 * typed with backslashes, else "/". */
function sepOf(prefix: string): string {
  return isWindowsFullPath(prefix) && prefix.endsWith("\\") ? "\\" : "/";
}

export interface CompletionSplit {
  /** The folder to list: "" = home (the server's default), else as typed
   * (`~` and absolute paths are expanded by the server). */
  dir: string;
  /** The name being completed (may be empty). */
  partial: string;
  /** What the completed name is appended to — always ends with `/`. */
  prefix: string;
}

export function splitForCompletion(text: string): CompletionSplit {
  if (text === "~") return { dir: "~", partial: "", prefix: "~/" };
  if (serverIsWindows() && isWindowsFullPath(text)) {
    // A native-Windows server: split at the last \ or /; "C:\" is a root.
    const cut = Math.max(text.lastIndexOf("\\"), text.lastIndexOf("/"));
    const head = text.slice(0, cut + 1);
    const dir = /^[A-Za-z]:[\\/]$/.test(head) ? head : head.slice(0, -1);
    return { dir, partial: text.slice(cut + 1), prefix: head };
  }
  const cut = text.lastIndexOf("/");
  const partial = text.slice(cut + 1);
  const head = cut >= 0 ? text.slice(0, cut + 1) : "";
  if (text.startsWith("/")) {
    // "/" alone is the root; "/home/me/" is /home/me (no trailing slash).
    return { dir: head === "/" ? "/" : head.slice(0, -1), partial, prefix: head };
  }
  if (text.startsWith("~")) {
    // "~Mi" has no slash yet: read as "~/Mi" (no ~user lookups here).
    if (cut < 0) return { dir: "~", partial: text.slice(1), prefix: "~/" };
    return { dir: head.slice(0, -1), partial, prefix: head };
  }
  // Relative: read from home, written back as ~/….
  const rel = head.replace(/\/+$/, "");
  return { dir: rel ? "~/" + rel : "", partial, prefix: rel ? "~/" + rel + "/" : "~/" };
}

/** The longest prefix every name shares, compared without case when `fold`
 * (the characters kept are the first name's). */
export function commonPrefix(names: string[], fold = false): string {
  if (!names.length) return "";
  const norm = (s: string) => (fold ? s.toLowerCase() : s);
  let n = names[0].length;
  const first = norm(names[0]);
  for (const name of names.slice(1)) {
    const s = norm(name);
    let i = 0;
    while (i < n && i < s.length && s[i] === first[i]) i++;
    n = i;
  }
  return names[0].slice(0, n);
}

/** The names `partial` could complete to, by the rules above. */
export function matchNames(partial: string, names: string[]): string[] {
  const pool = partial.startsWith(".") ? names : names.filter((n) => !n.startsWith("."));
  const exact = pool.filter((n) => n.startsWith(partial));
  if (exact.length) return exact;
  const low = partial.toLowerCase();
  return pool.filter((n) => n.toLowerCase().startsWith(low));
}

export interface Completion {
  /** The field's new text (unchanged when nothing could be added). */
  text: string;
  /** Every name that matched; more than one means "show them". */
  matches: string[];
  /** What a match is appended to (for `cycle`). */
  prefix: string;
}

/** Complete `text` against the entries of its folder (`names`, as listed for
 * `splitForCompletion(text).dir`). */
export function completeFrom(text: string, names: string[]): Completion {
  const { partial, prefix } = splitForCompletion(text);
  if (text === "~") return { text: "~/", matches: [], prefix }; // a shell's first step too
  const matches = matchNames(partial, names);
  if (matches.length === 1) return { text: prefix + matches[0] + sepOf(prefix), matches, prefix };
  if (!matches.length) return { text, matches, prefix };
  const exact = matches.every((m) => m.startsWith(partial));
  const lcp = commonPrefix(matches, !exact);
  // Never shorten what was typed (a case-folded prefix can be no longer than
  // the partial, and then the user's own spelling stays).
  const grown = lcp.length > partial.length ? lcp : null;
  return { text: grown ? prefix + grown : text, matches, prefix };
}

/** The next (step 1) or previous (step -1) match after the one `current`
 * holds — a second Tab on an ambiguous partial. From text that holds none of
 * them (the common prefix), forward starts at the first, backward at the last.
 * Each candidate is written as `prefix + name + "/"`, ready to type into. */
export function cycle(matches: string[], current: string, prefix: string, step: 1 | -1 = 1): string {
  if (!matches.length) return current;
  const at = matches.findIndex((m) => current === prefix + m + sepOf(prefix) || current === prefix + m);
  const n = matches.length;
  const next = at < 0 ? (step > 0 ? 0 : n - 1) : (at + step + n) % n;
  return prefix + matches[next] + sepOf(prefix);
}

/** One folder's listing, kept between Tabs so a Tab in the same folder asks
 * the server nothing. */
export interface Listing {
  dir: string;
  names: string[];
}

/** Tab's whole lookup: list the text's folder through `list` (or reuse
 * `cache` when it already holds that folder) and complete against it. null =
 * the folder could not be listed (it doesn't exist, or isn't readable). */
export async function tabComplete(
  text: string,
  list: (dir: string) => Promise<string[]>,
  cache: { current: Listing | null }
): Promise<Completion | null> {
  const { dir } = splitForCompletion(text);
  let names: string[];
  if (cache.current?.dir === dir) names = cache.current.names;
  else {
    try {
      names = await list(dir);
    } catch {
      return null;
    }
    cache.current = { dir, names };
  }
  return completeFrom(text, names);
}

/** What the field's polite live region says after a Tab, and whether it was
 * a miss (the field shakes). */
export function completionHint(text: string, r: Completion | null): { hint: string; miss: boolean } {
  if (!r) return { hint: "No folder at " + (splitForCompletion(text).dir || "~"), miss: true };
  if (r.matches.length > 1) return { hint: r.matches.length + " folders match", miss: false };
  if (r.text !== text) return { hint: "completed to " + r.text, miss: false };
  return { hint: "No folder matches", miss: true };
}
