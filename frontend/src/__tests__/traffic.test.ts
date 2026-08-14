import { describe, it, expect } from "vitest";
import type { TrafficResponse } from "../api/types";
import { hasVisitorData, niceTicks, shownMetric } from "../lib/traffic";

/** A payload from a click Worker that predates visitor attribution: clicks
 * only, with every people-shaped section absent rather than zeroed. */
const withoutVisitors = {
  clicks: {
    days: 90,
    series: [{ day: "2026-08-10", slug: "mac", os: "mac", clicks: 3 }],
    totals_by_slug: { mac: 3 },
    visitors_by_day: [],
    visitors_by_slug: [],
    totals: null,
    downloads: null,
    error: "",
  },
} as unknown as TrafficResponse;

const withVisitors = {
  clicks: {
    ...withoutVisitors.clicks,
    totals: { clicks: 3, visitors: 2, new_visitors: 1 },
  },
} as unknown as TrafficResponse;

describe("hasVisitorData", () => {
  it("is false for a Worker that reports no visitor totals", () => {
    expect(hasVisitorData(withoutVisitors)).toBe(false);
  });

  it("is false before the payload has loaded", () => {
    expect(hasVisitorData(undefined)).toBe(false);
    expect(hasVisitorData(null)).toBe(false);
  });

  it("is true once totals are present", () => {
    expect(hasVisitorData(withVisitors)).toBe(true);
  });
});

describe("shownMetric", () => {
  it("honours the request when visitor data exists", () => {
    expect(shownMetric("people", withVisitors)).toBe("people");
    expect(shownMetric("clicks", withVisitors)).toBe("clicks");
  });

  it("falls back to clicks when there is no visitor data", () => {
    // "people" is the INITIAL state, so without this the screen drew a clicks
    // chart under a "Visitors over time" heading with People styled active.
    expect(shownMetric("people", withoutVisitors)).toBe("clicks");
  });

  it("never traps the user on clicks once visitor data arrives", () => {
    // The regression this file exists for. The toggle used to render People
    // `disabled` whenever visitor data was missing — and since "people" is the
    // default, pressing Clicks moved you into a state with no way back. The
    // screen now only offers the toggle when both options are real, and the
    // requested metric has to survive the payload gaining visitor data so that
    // a deploy mid-session restores the People view rather than pinning it.
    const requested = "people";
    expect(shownMetric(requested, withoutVisitors)).toBe("clicks");
    expect(shownMetric(requested, withVisitors)).toBe("people");
  });

  it("shows clicks while the payload is still loading", () => {
    expect(shownMetric("people", undefined)).toBe("clicks");
  });
});

describe("niceTicks", () => {
  it("keeps the step whole for the small counts this screen mostly shows", () => {
    // The reason this helper exists rather than a bare 1/2/5×10ⁿ ladder: at a
    // peak of 3 that ladder wants a step of 0.75, labelling three positions no
    // count can occupy.
    expect(niceTicks(3)).toEqual({ max: 3, ticks: [0, 1, 2, 3] });
    expect(niceTicks(1)).toEqual({ max: 1, ticks: [0, 1] });
    expect(niceTicks(6)).toEqual({ max: 6, ticks: [0, 2, 4, 6] });
  });

  it("rounds the axis top up to a tick, above the data's own peak", () => {
    // 90 is what a busy click day looks like; the axis stops at a round 100 so
    // the top gridline is a number worth reading.
    expect(niceTicks(90)).toEqual({ max: 100, ticks: [0, 25, 50, 75, 100] });
    expect(niceTicks(142).max).toBe(150);
    // A peak that is already round is left alone rather than padded past it.
    expect(niceTicks(1000)).toEqual({ max: 1000, ticks: [0, 250, 500, 750, 1000] });
  });

  it("always spans the data", () => {
    for (const peak of [1, 2, 7, 13, 99, 100, 101, 999, 4321, 87654]) {
      const { max, ticks } = niceTicks(peak);
      expect(max).toBeGreaterThanOrEqual(peak);
      expect(ticks[0]).toBe(0);
      expect(ticks[ticks.length - 1]).toBe(max);
      expect(ticks.every(Number.isInteger)).toBe(true);
      // Enough gridlines to interpolate between, few enough to stay quiet. The
      // floor is 2 because a peak of 1 has nowhere to put a third whole tick.
      expect(ticks.length).toBeGreaterThanOrEqual(2);
      expect(ticks.length).toBeLessThanOrEqual(7);
    }
  });

  it("gives an empty or all-zero window a real 0–1 axis", () => {
    // Math.max() of no arguments is -Infinity, and an all-zero window is 0 —
    // both reach this helper, and neither may divide the scale by zero.
    expect(niceTicks(0)).toEqual({ max: 1, ticks: [0, 1] });
    expect(niceTicks(-Infinity)).toEqual({ max: 1, ticks: [0, 1] });
    expect(niceTicks(NaN)).toEqual({ max: 1, ticks: [0, 1] });
  });
});
