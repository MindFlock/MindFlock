/** When to refetch the usage pill in response to a push event.
 *
 * The "% left" figure only moves when an agent burns tokens, and the server
 * announces exactly that over the event bus — so the pill is refreshed off
 * those events rather than left to its poll, which is what made it feel
 * delayed. The catch is volume: one busy grid can flip a dozen sessions
 * between working and idle within a second, and each flip is a signal that
 * usage changed. Refetching per event would put a request storm behind an
 * ordinary burst of activity.
 *
 * So signals are coalesced: the first one refreshes immediately (the common
 * case is a single session finishing a turn — that must feel instant), and
 * anything arriving inside the coalesce window collapses into ONE trailing
 * refresh at the end of it. The trailing refresh is the part that matters for
 * correctness: dropping the tail would leave the pill showing the usage from
 * the first event of a burst, which is precisely the staleness this is meant
 * to fix.
 *
 * Kept separate from the wiring in state/queries.ts because "one now, one at
 * the end, never more" is a rule with a wrong answer, and a rule with a wrong
 * answer belongs somewhere it can be tested. */

/** Long enough that a grid-wide flurry costs two refreshes rather than twelve,
 * short enough that the second one still feels like a live number. */
export const USAGE_COALESCE_MS = 5_000;

export type UsageNudge =
  /** Refresh now (and record the time as the new anchor). */
  | { action: "refresh" }
  /** Too soon — refresh once, this many ms from now. */
  | { action: "schedule"; delayMs: number }
  /** Too soon, and a trailing refresh is already queued to cover it. */
  | { action: "skip" };

/** What to do about a usage-changing signal that just arrived.
 *
 * `lastRefreshAt` is when a refresh last actually happened, or **null** for
 * "not yet" — spelled as null rather than 0 on purpose. A zero sentinel only
 * behaves because `Date.now()` is a large number, so it would quietly defer the
 * very first refresh under any other clock (a test's, a fake-timer's), which is
 * a rule that works by accident. `trailingQueued` says whether a scheduled
 * refresh is already pending. */
export function nudgeUsage(
  now: number,
  lastRefreshAt: number | null,
  trailingQueued: boolean,
  coalesceMs: number = USAGE_COALESCE_MS
): UsageNudge {
  if (lastRefreshAt == null) return { action: "refresh" };
  // A backwards clock jump (NTP correction, a laptop waking) would otherwise
  // compute a delay LONGER than the window and stall the refresh for as long
  // as the jump was big. Treating it as "long enough ago" is the safe read:
  // the cost of an extra refresh is one request, the cost of skipping one is a
  // wrong number on screen.
  const elapsed = now - lastRefreshAt;
  if (elapsed < 0 || elapsed >= coalesceMs) return { action: "refresh" };
  if (trailingQueued) return { action: "skip" };
  return { action: "schedule", delayMs: coalesceMs - elapsed };
}
