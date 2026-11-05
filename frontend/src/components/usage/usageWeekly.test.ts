/** Weekly-only Claude plans (no 5-hour window) and the stale shared reading.
 *
 * The owner's plan has only a weekly limit, yet the sidebar kept showing a
 * 5-hour window "resetting at 3pm". With window_kind "weekly" the headline is
 * the weekly meter — percent + reset weekday/time — and no 5-hour countdown
 * appears anywhere; a reading served through the 429 backoff says how old it
 * is. Kept next to usageModel.ts (the vitest include is src/**). */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fmtResetInDays,
  fmtWeeklyReset,
  isWeekly,
  planHeadLabel,
  planStripRows,
  providerSummaryRow,
  staleLabel,
  windowPillText,
  type PlanWindow,
  type UsageProviderEntry,
} from "./usageModel";

const NOW_MS = 1_700_000_000_000;
const NOW_SEC = NOW_MS / 1000;
const clock = () => vi.spyOn(Date, "now").mockReturnValue(NOW_MS);
afterEach(() => vi.restoreAllMocks());

const WEEK_END = NOW_SEC + 2 * 86400 + 4 * 3600; // 2d 4h ahead

const weeklyOnly = (o: Partial<PlanWindow> = {}): PlanWindow => ({
  window_kind: "weekly",
  source: "live",
  percent_used: 35,
  end: WEEK_END,
  session: null,
  weekly: { percent_used: 35, end: WEEK_END },
  cost: 12.5,
  ...o,
});

describe("weekly-only headline", () => {
  it("the pill shows the week's percent and reset weekday, no countdown", () => {
    clock();
    const text = windowPillText(weeklyOnly());
    expect(text).toBe("Week 35% · resets " + fmtWeeklyReset(WEEK_END));
    // A weekday, never an "Xh Ym" 5-hour-style countdown.
    expect(text).not.toMatch(/\d+h \d+m/);
    expect(fmtWeeklyReset(WEEK_END)).toMatch(/[A-Za-z]{2,}/);
  });

  it("still names the week when the percent is unknown (estimate)", () => {
    clock();
    expect(windowPillText(weeklyOnly({ percent_used: null, source: "estimate" }))).toBe(
      "Week · resets " + fmtWeeklyReset(WEEK_END),
    );
    expect(windowPillText(weeklyOnly({ percent_used: null, end: null }))).toBeNull();
  });

  it("a session plan keeps its 5-hour pill text", () => {
    clock();
    expect(windowPillText({ end: NOW_SEC + 7 * 60, percent_used: 42 })).toBe("58% left · resets ~7m");
    expect(windowPillText({ end: null, percent_used: 42 })).toBeNull();
  });

  it("popover rows lead with the weekly meter and carry no 5-hour rows", () => {
    clock();
    const rows = planStripRows(
      weeklyOnly({ weekly_opus: { percent_used: 3, end: WEEK_END } }),
    );
    expect(rows[0]).toEqual(["Weekly limit used", "35%"]);
    expect(rows[1]).toEqual(["Week resets", fmtWeeklyReset(WEEK_END) + " (in 2d 4h)"]);
    expect(rows).toContainEqual(["Week cost (≈API)", "~$12.5"]);
    expect(rows.find((r) => r[0] === "Weekly (Opus)")?.[1]).toMatch(/^3% used · resets /);
    const labels = rows.map((r) => r[0]);
    expect(labels).not.toContain("Window resets");
    expect(labels).not.toContain("Plan window used");
    expect(labels).not.toContain("Weekly cap"); // the headline IS the weekly cap
  });

  it("per-model weekly limits get their own rows", () => {
    clock();
    const rows = planStripRows(
      weeklyOnly({ weekly_models: [{ label: "Fable", percent_used: 0, end: WEEK_END }] }),
    );
    expect(rows.find((r) => r[0] === "Weekly (Fable)")?.[1]).toMatch(/^0% used · resets /);
  });

  it("an estimate on a weekly-only plan says the percent is unknown", () => {
    clock();
    const rows = planStripRows(weeklyOnly({ source: "estimate", percent_used: null }));
    expect(rows[0][0]).toBe("Weekly limit used");
    expect(rows[0][1]).toContain("not known");
    expect(rows.map((r) => r[0])).not.toContain("Window resets");
  });

  it("a plan with both windows shows the session first, the week second", () => {
    clock();
    const rows = planStripRows({
      window_kind: "session",
      source: "live",
      end: NOW_SEC + 3600,
      percent_used: 20,
      weekly: { percent_used: 50, end: WEEK_END },
    });
    const labels = rows.map((r) => r[0]);
    expect(labels.indexOf("Plan window used")).toBe(0);
    expect(labels.indexOf("Window resets")).toBe(1);
    expect(labels.indexOf("Weekly cap")).toBeGreaterThan(1);
  });

  it("the combined view's summary row and the heading follow the kind", () => {
    clock();
    const p: UsageProviderEntry = { name: "claude", label: "Claude", mode: "windowed", window: weeklyOnly() };
    expect(providerSummaryRow(p)).toEqual(["Claude", "65% left this week · resets " + fmtWeeklyReset(WEEK_END)]);
    expect(planHeadLabel(p.window)).toBe("weekly limit");
    expect(planHeadLabel({ end: 1 })).toBe("plan window");
    expect(isWeekly(p.window)).toBe(true);
    expect(isWeekly({ end: 1 })).toBe(false);
  });

  it("fmtResetInDays shows days past a day, the short form under one", () => {
    clock();
    expect(fmtResetInDays(WEEK_END)).toBe("2d 4h");
    expect(fmtResetInDays(NOW_SEC + 7 * 60)).toBe("~7m");
  });
});

describe("stale label", () => {
  it("says how old a stale reading is", () => {
    expect(staleLabel({ stale: true, fetched_at: NOW_SEC - 12 * 60 }, NOW_MS)).toBe("as of 12 min ago");
    expect(staleLabel({ stale: true, fetched_at: NOW_SEC - 3 * 3600 }, NOW_MS)).toBe("as of 3 h ago");
  });

  it("is silent for a fresh reading or one with no timestamp", () => {
    expect(staleLabel({ stale: false, fetched_at: NOW_SEC - 30 }, NOW_MS)).toBeNull();
    expect(staleLabel({ stale: true }, NOW_MS)).toBeNull();
    expect(staleLabel(null, NOW_MS)).toBeNull();
  });

  it("rides as the last popover row", () => {
    clock();
    const rows = planStripRows(weeklyOnly({ stale: true, fetched_at: NOW_SEC - 12 * 60 }));
    expect(rows[rows.length - 1]).toEqual(["Live reading", "as of 12 min ago"]);
  });
});
