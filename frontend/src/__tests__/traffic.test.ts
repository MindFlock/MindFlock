import { describe, it, expect } from "vitest";
import type { TrafficResponse } from "../api/types";
import {
  clickView,
  hasVisitorData,
  niceTicks,
  osMatchedDownloads,
  releaseDownloads,
  sharePct,
  shownMetric,
} from "../lib/traffic";

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

/* --- Human/automated classification --------------------------------------- */

const human = {
  series: [
    { day: "2026-11-25", slug: "mac", os: "mac", clicks: 2 },
    { day: "2026-11-25", slug: "windows", os: "mac", clicks: 1 },
  ],
  visitors_by_day: [
    { day: "2026-11-25", visitors: 2, new_visitors: 2, returning_visitors: 0, unknown_visitors: 0 },
  ],
  visitors_by_slug: [{ slug: "mac", visitors: 2, new_visitors: 2, clicks: 2 }],
  totals: { clicks: 3, visitors: 2, new_visitors: 2 },
  downloads: { new_visitors: 2, new_visitors_clicked: 2, by_slug: [] },
};

const classification = (over: Record<string, unknown> = {}) => ({
  since: "2026-11-25",
  unclassified_clicks: 40,
  by_class: {
    human: { clicks: 3, visitors: 2 },
    unverified: { clicks: 1, visitors: 1 },
    automated: { clicks: 36, visitors: 9 },
  },
  reasons: [],
  by_slug: [],
  ...over,
});

const classified = (over: Record<string, unknown> = {}) =>
  ({
    clicks: { ...withVisitors.clicks, human, classification: classification(over) },
  }) as unknown as TrafficResponse;

describe("clickView", () => {
  it("leads with the unfiltered figures against a Worker without classification, and says so", () => {
    const v = clickView(withVisitors)!;
    expect(v.state).toBe("old-worker");
    expect(v.scope).toBe("all");
    expect(v.totals).toEqual({ clicks: 3, visitors: 2, new_visitors: 1 });
    expect(v.series).toBe(withVisitors.clicks.series);
    expect(v.classified).toBeNull();
  });

  it("treats an older backend that omits the keys entirely as an old Worker", () => {
    // The app and the Worker ship independently, and the payload type has the
    // new keys optional for exactly this.
    const v = clickView(withoutVisitors)!;
    expect(v.state).toBe("old-worker");
    expect(v.totals).toBeNull();
  });

  it("uses the human sections once the Worker classifies, labelled partial while old rows remain", () => {
    const v = clickView(classified())!;
    expect(v.state).toBe("partial");
    expect(v.scope).toBe("human");
    expect(v.since).toBe("2026-11-25");
    expect(v.totals).toEqual(human.totals);
    expect(v.series).toBe(human.series);
    // Shares are of CLASSIFIED clicks only — the same period as the human
    // figures — never of the 90-day unfiltered total.
    expect(v.classified!.clicks).toBe(40);
    expect(v.classified!.automated.clicks).toBe(36);
  });

  it("is full once nothing in the window predates classification", () => {
    expect(clickView(classified({ unclassified_clicks: 0 }))!.state).toBe("full");
  });

  it("is pending when classification is deployed but has seen no click yet", () => {
    const v = clickView(classified({ since: null }))!;
    expect(v.state).toBe("pending");
    expect(v.scope).toBe("human");
  });

  it("is null before the payload has loaded", () => {
    expect(clickView(undefined)).toBeNull();
  });
});

describe("osMatchedDownloads", () => {
  it("folds the click series when the Worker sends no os_match", () => {
    const m = osMatchedDownloads([
      { day: "d", slug: "mac", os: "mac", clicks: 2 },
      { day: "d", slug: "mac", os: "android", clicks: 5 },
      { day: "d", slug: "linux", os: "windows", clicks: 3 },
      { day: "d", slug: "github", os: "mac", clicks: 9 }, // not a download
    ]);
    expect(m.clicks).toBe(10);
    expect(m.matched).toBe(2);
    expect(m.by_slug.find((b) => b.slug === "linux")).toEqual({ slug: "linux", clicks: 3, matched: 0 });
  });

  it("prefers the Worker's own figure when present", () => {
    const fromWorker = { clicks: 4, matched: 3, by_slug: [] };
    expect(osMatchedDownloads([], fromWorker)).toBe(fromWorker);
  });
});

describe("releaseDownloads", () => {
  it("splits installers from every other asset", () => {
    const d = releaseDownloads({
      downloads_total: 4113,
      installer_downloads_total: 1703,
      downloads_by_kind: { installer: 1703, update: 681, update_check: 983, checksum: 1156, package: 258, other: 0 },
    } as unknown as TrafficResponse);
    expect(d.installers).toBe(1703);
    expect(d.other).toBe(2410);
    expect(d.byKind!.update_check).toBe(983);
  });

  it("reports null, not zero, against a backend from before the split", () => {
    expect(releaseDownloads({ downloads_total: 4113 } as unknown as TrafficResponse)).toEqual({
      installers: null,
      other: null,
      byKind: null,
    });
  });
});

describe("sharePct", () => {
  it("rounds, but never shows a non-zero share as 0% or a partial one as 100%", () => {
    expect(sharePct(36, 40)).toBe(90);
    expect(sharePct(1, 1000)).toBe(1);
    expect(sharePct(999, 1000)).toBe(99);
    expect(sharePct(0, 10)).toBe(0);
    expect(sharePct(10, 10)).toBe(100);
  });

  it("is null when there is no whole", () => {
    expect(sharePct(0, 0)).toBeNull();
  });
});
