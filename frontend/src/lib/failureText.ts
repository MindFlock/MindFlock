/** Shortening a recorded failure reason for a chip — pure, so it is testable.
 *
 * The panels annotate every work row with why auto-pickup did or didn't take it.
 * Most reasons are a phrase ("queued for ingestion (pending)"); one is not — a
 * `failed` ledger entry carries the whole git error, branch name and absolute
 * worktree path included. Wrapping it inside the chip (the previous fix) kept the
 * remedy readable but turned one row into a paragraph, pushing the row's controls
 * around and making the list unscannable.
 *
 * So the chip gets the FRONT of the sentence and the rest moves to a bottom-right
 * error card on click (see lib/errorPop.ts). Nothing is hidden: the full string is
 * still one hover or one click away, in a place with room for it.
 */

/** Where a chip stops being a label and starts being a paragraph. */
const CHIP_BUDGET = 58;

export interface ShortReason {
  /** What the chip shows. */
  short: string;
  /** Whether anything was cut — i.e. whether there is more to open. */
  clipped: boolean;
}

export function shortReason(reason: string, budget = CHIP_BUDGET): ShortReason {
  const text = String(reason || "")
    .split(/\s+/)
    .join(" ")
    .trim();
  if (text.length <= budget) return { short: text, clipped: false };
  // Cut on a word boundary so the chip never ends mid-path; fall back to a hard
  // cut for a single unbroken token (a branch name, a URL).
  const window = text.slice(0, budget);
  const lastSpace = window.lastIndexOf(" ");
  // A word that ends exactly at the edge is a whole word — dropping it would
  // throw away a word that fit.
  const head =
    text[budget] === " " || lastSpace <= budget * 0.5
      ? window
      : window.slice(0, lastSpace);
  return { short: head.replace(/[\s,;:.]+$/, "") + "…", clipped: true };
}
