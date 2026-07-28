import { describe, it, expect, afterEach, vi } from "vitest";
import type { Instance } from "../api/types";
import {
  asUsageWindows,
  usageHeadline,
  usageRows,
  fmtResetIn,
  isPlanMode,
  usageProviders,
  planStripRows,
  periodRows,
  providerSummaryRow,
  type UsageAgg,
  type UsageProviderEntry,
  type PlanWindow,
} from "../components/usage/usageModel";

const info = (o: Partial<Instance>): Instance => o as unknown as Instance;

// A fixed clock so the "resets in" durations are deterministic.
const NOW_MS = 1_700_000_000_000;
const NOW_SEC = NOW_MS / 1000;
const clock = () => vi.spyOn(Date, "now").mockReturnValue(NOW_MS);
afterEach(() => vi.restoreAllMocks());

describe("asUsageWindows", () => {
  it("passes an object through and maps null-ish to null", () => {
    const obj = { providers: [] };
    expect(asUsageWindows(obj as never)).toBe(obj);
    expect(asUsageWindows(null)).toBeNull();
    expect(asUsageWindows(undefined)).toBeNull();
  });
});

describe("usageHeadline", () => {
  it("prefixes the provider and shows cost with the context window", () => {
    expect(
      usageHeadline(
        info({
          provider: "codex",
          tokens_cost: 0.62,
          tokens_ctx: 128000,
          tokens_ctx_window: 200000,
        })
      )
    ).toBe("Codex · ~$0.62 · 128k/200k");
  });
  it("omits the window fragment when there is no context window", () => {
    expect(usageHeadline(info({ provider: "claude", tokens_cost: 0 }))).toBe("Claude · ~$0");
  });
  it("drops the provider prefix when unknown/absent", () => {
    expect(usageHeadline(info({ tokens_cost: 1.5 }))).toBe("~$1.50");
  });
});

describe("usageRows", () => {
  it("builds the labeled breakdown, including a context percentage", () => {
    const rows = usageRows(
      info({
        tokens_cost: 0.62,
        tokens_ctx: 100000,
        tokens_ctx_window: 200000,
        tokens_in: 12,
        tokens: 34,
        tokens_cache_read: 5,
        tokens_cache_write: 6,
        tokens_model: "gpt-x",
      })
    );
    expect(rows).toEqual([
      ["Est. cost", "~$0.62"],
      ["Context", "100k / 200k (50%)"],
      ["Input", "12"],
      ["Output", "34"],
      ["Cache read", "5"],
      ["Cache write", "6"],
      ["Model", "gpt-x"],
    ]);
  });
  it("relabels cost on a plan, drops the Context row without a window, drops Model when unset", () => {
    const rows = usageRows(
      info({ tokens_cost: 0, tokens_in: 1, tokens: 2, tokens_cache_read: 3, tokens_cache_write: 4 }),
      true
    );
    expect(rows[0]).toEqual(["≈ API-equiv. cost", "~$0"]);
    expect(rows.some((r) => r[0] === "Context")).toBe(false);
    expect(rows.some((r) => r[0] === "Model")).toBe(false);
  });
});

describe("fmtResetIn", () => {
  it("says 'now' once the deadline has passed", () => {
    clock();
    expect(fmtResetIn(NOW_SEC)).toBe("now");
    expect(fmtResetIn(NOW_SEC - 100)).toBe("now");
  });
  it("formats the remaining time via fmtDurationShort", () => {
    clock();
    expect(fmtResetIn(NOW_SEC + 7 * 60)).toBe("~7m");
    expect(fmtResetIn(NOW_SEC + 2 * 3600 + 11 * 60)).toBe("2h 11m");
  });
});

describe("usageProviders / isPlanMode", () => {
  it("returns [] when providers are missing or not an array", () => {
    expect(usageProviders(null)).toEqual([]);
    expect(usageProviders({ providers: null } as never)).toEqual([]);
    expect(isPlanMode(null)).toBe(false);
  });
  it("detects a windowed (subscription) provider as plan mode", () => {
    const providers = [{ name: "a", label: "A", mode: "metered" }];
    expect(usageProviders({ providers })).toEqual(providers);
    expect(isPlanMode({ providers })).toBe(false);
    expect(isPlanMode({ providers: [{ name: "b", label: "B", mode: "windowed" }] })).toBe(true);
  });
});

describe("planStripRows", () => {
  it("shows the 'fresh' placeholder when there is no active window", () => {
    expect(planStripRows(null)).toEqual([
      ["Plan window", "fresh — your next message starts a new one", "wrap"],
    ]);
    expect(planStripRows({ end: 0 })).toEqual([
      ["Plan window", "fresh — your next message starts a new one", "wrap"],
    ]);
  });
  it("reports a live percentage, reset time, and window cost", () => {
    clock();
    const w: PlanWindow = { end: NOW_SEC + 7 * 60, source: "live", percent_used: 42, cost: 1.5 };
    const rows = planStripRows(w);
    expect(rows[0]).toEqual(["Plan window used", "42%"]);
    expect(rows[1][0]).toBe("Window resets");
    expect(rows[1][1]).toContain("(in ~7m)");
    expect(rows[2]).toEqual(["Window cost (≈API)", "~$1.50"]);
  });
  it("marks an estimated (non-live) percentage", () => {
    clock();
    const rows = planStripRows({ end: NOW_SEC + 60, percent_used: 10 });
    expect(rows[0]).toEqual(["Plan window used", "10% (est.)"]);
  });
  it("emits one row per model-group quota and skips the single reset row", () => {
    clock();
    const w: PlanWindow = {
      end: NOW_SEC + 60,
      groups: [
        { label: "Fast", percent_used: 30, end: NOW_SEC + 7 * 60 },
        { label: "Empty", percent_used: null, end: null }, // skipped: nothing to show
      ],
    };
    const rows = planStripRows(w);
    expect(rows).toContainEqual(["Fast", "30% used · resets ~7m"]);
    expect(rows.some((r) => r[0] === "Empty")).toBe(false);
    expect(rows.some((r) => r[0] === "Window resets")).toBe(false);
  });
  it("appends weekly-cap and billed-extra rows when present", () => {
    clock();
    const w: PlanWindow = {
      end: NOW_SEC + 60,
      percent_used: 5,
      weekly: { percent_used: 20, end: NOW_SEC + 3 * 86400 },
      extra: { used: 5, limit: 100 },
    };
    const rows = planStripRows(w);
    const weekly = rows.find((r) => r[0] === "Weekly cap");
    expect(weekly?.[1].startsWith("20% used · resets ")).toBe(true);
    expect(rows).toContainEqual(["Extra usage (billed)", "$5 / $100 this month"]);
  });
});

describe("periodRows", () => {
  const agg: UsageAgg = { cost: 0.62, in: 12, out: 34, cache_read: 5, cache_write: 6 };

  it("uses the session aggregate for the 'session' period", () => {
    expect(periodRows(agg, null, "session", false)).toEqual([
      ["Est. cost", "~$0.62"],
      ["Input", "12"],
      ["Output", "34"],
      ["Cache read", "5"],
      ["Cache write", "6"],
    ]);
  });
  it("relabels cost on a plan and reads the named period from history", () => {
    const rows = periodRows(null, { day: agg }, "day", true);
    expect(rows[0]).toEqual(["≈ API-equiv. cost", "~$0.62"]);
  });
  it("returns the empty-state row when the period has no data", () => {
    expect(periodRows(null, null, "session", false)).toEqual([["—", "no data yet"]]);
    expect(periodRows(null, {}, "week", false)).toEqual([["—", "no data yet"]]);
  });
});

describe("providerSummaryRow", () => {
  const p = (o: Partial<UsageProviderEntry>): UsageProviderEntry =>
    ({ name: "x", label: "X", ...o }) as UsageProviderEntry;

  it("shows remaining percent and reset for a windowed provider", () => {
    clock();
    expect(
      providerSummaryRow(p({ label: "Codex", mode: "windowed", window: { end: NOW_SEC + 7 * 60, percent_used: 42 } }))
    ).toEqual(["Codex", "58% left · resets ~7m"]);
  });
  it("says 'active' when a windowed provider has no percentage", () => {
    clock();
    expect(
      providerSummaryRow(p({ label: "Codex", mode: "windowed", window: { end: NOW_SEC + 7 * 60, percent_used: null } }))
    ).toEqual(["Codex", "active · resets ~7m"]);
  });
  it("says 'fresh window' for a windowed provider with no active window", () => {
    expect(providerSummaryRow(p({ label: "Codex", mode: "windowed", window: null }))).toEqual([
      "Codex",
      "fresh window",
    ]);
  });
  it("labels a metered provider as using its own API key", () => {
    expect(providerSummaryRow(p({ label: "Claude", mode: "metered" }))).toEqual([
      "Claude",
      "metered (own API key)",
    ]);
  });
});
