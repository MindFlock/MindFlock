/** Find-in-page for the full-history overlay (Ctrl+F on a terminal pane).
 *
 * Pure so the matching and "which hit do we land on" rules are testable
 * without a DOM; HistoryOverlay does the highlighting and scrolling. */

/** Upper bound on hits we track/highlight — a one-letter query over a long
 * history would otherwise build hundreds of thousands of DOM ranges. */
export const FIND_MAX = 5000;

/** Offsets of every case-insensitive, non-overlapping occurrence of
 * ``query`` in ``text``, capped at ``max``. A RegExp (not toLowerCase +
 * indexOf) because lower-casing can change a string's length (İ → i̇),
 * which would skew every offset after it. */
export function findAll(text: string, query: string, max: number = FIND_MAX): number[] {
  const out: number[] = [];
  if (!query || !text) return out;
  const re = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
  let m: RegExpExecArray | null;
  while (out.length < max && (m = re.exec(text)) !== null) out.push(m.index);
  return out;
}

/** Which hit a fresh search lands on, given the text offset at the bottom
 * of what the reader is looking at: the last hit at or above it (terminal
 * readers want the most recent occurrence, and the view usually opens at
 * the tail), else the first hit below. -1 when there are none. */
export function pickStart(matches: number[], anchor: number): number {
  if (!matches.length) return -1;
  let lo = 0,
    hi = matches.length - 1,
    best = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (matches[mid] <= anchor) {
      best = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return best >= 0 ? best : 0;
}

/** Step ``cur`` by ``dir`` through ``n`` hits, wrapping at both ends. */
export function stepMatch(cur: number, dir: 1 | -1, n: number): number {
  if (n <= 0) return -1;
  if (cur < 0) return dir === 1 ? 0 : n - 1;
  return (cur + dir + n) % n;
}
