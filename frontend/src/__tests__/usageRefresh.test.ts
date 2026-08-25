import { describe, it, expect } from "vitest";
import { nudgeUsage, USAGE_COALESCE_MS } from "../lib/usageRefresh";

/** Replays a sequence of event timestamps through the rule the way
 * state/queries.ts drives it, and returns when refreshes actually happened.
 *
 * The trailing refresh is modelled the way setTimeout delivers it: queued at
 * the decision, fired at the scheduled instant. Any event whose timestamp is at
 * or past a queued firing time sees that timer as already spent. */
function replay(events: number[], coalesceMs = USAGE_COALESCE_MS): number[] {
  const refreshes: number[] = [];
  let lastRefreshAt: number | null = null;
  let trailingAt: number | null = null;

  const fireDue = (upTo: number) => {
    if (trailingAt != null && trailingAt <= upTo) {
      refreshes.push(trailingAt);
      lastRefreshAt = trailingAt;
      trailingAt = null;
    }
  };

  for (const now of events) {
    fireDue(now);
    const decision = nudgeUsage(now, lastRefreshAt, trailingAt != null, coalesceMs);
    if (decision.action === "refresh") {
      refreshes.push(now);
      lastRefreshAt = now;
    } else if (decision.action === "schedule") {
      trailingAt = now + decision.delayMs;
    }
  }
  fireDue(Infinity);
  return refreshes;
}

describe("nudgeUsage", () => {
  it("refreshes immediately for the first signal", () => {
    // A single session finishing its turn is the common case and has to feel
    // instant — that is the whole point of not waiting for the poll. Small
    // timestamps included: "never refreshed" must not depend on Date.now()
    // being a big number.
    expect(nudgeUsage(10_000, null, false)).toEqual({ action: "refresh" });
    expect(nudgeUsage(0, null, false)).toEqual({ action: "refresh" });
  });

  it("collapses a burst into one immediate and one trailing refresh", () => {
    // Twelve sessions flipping between working and idle inside a second is an
    // ordinary busy grid, not an edge case; it must not become twelve requests.
    const burst = [1_000, 1_050, 1_090, 1_120, 1_400, 1_800, 1_950];
    expect(replay(burst)).toEqual([1_000, 6_000]);
  });

  it("keeps the tail — the last signal of a burst is never dropped", () => {
    // Dropping it would pin the pill to the usage from the FIRST event of the
    // burst, which is the exact staleness this path exists to remove.
    const refreshes = replay([0, 100, 4_900]);
    expect(refreshes.length).toBe(2);
    expect(refreshes[refreshes.length - 1]).toBe(USAGE_COALESCE_MS);
  });

  it("refreshes every signal once they are spaced out", () => {
    expect(replay([0, 6_000, 12_000, 18_000])).toEqual([0, 6_000, 12_000, 18_000]);
  });

  it("treats the coalesce window as inclusive at its edge", () => {
    expect(nudgeUsage(5_000, 0, false)).toEqual({ action: "refresh" });
    expect(nudgeUsage(4_999, 0, false)).toEqual({ action: "schedule", delayMs: 1 });
  });

  it("skips rather than stacking timers while one is queued", () => {
    expect(nudgeUsage(1_100, 1_000, true)).toEqual({ action: "skip" });
  });

  it("refreshes through a backwards clock jump instead of stalling", () => {
    // A wake-from-sleep NTP correction makes `now` earlier than the last
    // refresh; the naive delay would be longer than the window, holding the
    // number back by however far the clock moved.
    expect(nudgeUsage(1_000, 90_000, false)).toEqual({ action: "refresh" });
  });

  it("never schedules further out than the window itself", () => {
    for (const elapsed of [0, 1, 2_500, 4_999]) {
      const d = nudgeUsage(elapsed, 0, false);
      if (d.action === "schedule") {
        expect(d.delayMs).toBeGreaterThan(0);
        expect(d.delayMs).toBeLessThanOrEqual(USAGE_COALESCE_MS);
      }
    }
  });
});
