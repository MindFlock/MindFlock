/** Row matching for the list dialogs' Ctrl+F filter — Recently closed, which
 * lists both closed sessions and the workspace directories left on disk.
 *
 * Whitespace-separated tokens, ALL of which must appear somewhere in the row —
 * "shortcut 210" finds `shortcut-21018` without the user having to remember
 * whether the separator was a dash, a slash, or a space. A single-token query
 * is therefore just the sidebar's substring match (see matchesFilter in
 * components/sidebar/ordering.ts), so the two searches behave the same for the
 * way people actually type. */

/** Split a raw query into lowercased tokens; empty means "match everything". */
export function searchTokens(query: string): string[] {
  return query.toLowerCase().split(/\s+/).filter(Boolean);
}

/** True when every token appears in one of the row's fields. Nullish fields are
 * dropped, so a caller can pass optional columns without guarding each one. */
export function matchesTokens(
  fields: (string | null | undefined)[],
  tokens: string[]
): boolean {
  if (!tokens.length) return true;
  const hay = fields.filter(Boolean).join(" ").toLowerCase();
  return tokens.every((t) => hay.includes(t));
}
