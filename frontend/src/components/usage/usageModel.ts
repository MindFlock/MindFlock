/** Ports the pure usage/cost helpers from 050-usage-cost.js: per-session
 * headline + breakdown rows, plan-window helpers (isPlanMode / planStripRows /
 * fmtResetIn), period rows, provider summary rows, and the
 * usage-note texts (verbatim). The /api/usage payload itself comes from
 * useUsage() in state/queries.ts — no fetching happens here. */

import type { Instance, UsageResponse } from "../../api/types";
import { fmtDurationShort, fmtTokens, fmtUsd, provLabel } from "../../lib/format";

// --- /api/usage payload shapes (what the server actually sends) ----------- //
// api/types.ts keeps UsageResponse deliberately loose; these are the fields
// the usage UI interprets ({day,week,month,year,mode,window,providers,default}).

export interface UsageAgg {
  cost: number;
  in: number;
  out: number;
  cache_read: number;
  cache_write: number;
}

export interface PlanWindowGroup {
  label?: string | null;
  percent_used?: number | null;
  end?: number | null;
}

export interface PlanWindow {
  end?: number | null;
  /** "live" = the provider's own meter; anything else is our estimate. */
  source?: string | null;
  percent_used?: number | null;
  cost?: number | null;
  groups?: PlanWindowGroup[] | null;
  weekly?: { percent_used?: number | null; end?: number | null } | null;
  extra?: { used: number; limit: number } | null;
}

export type PeriodTotals = {
  day?: UsageAgg;
  week?: UsageAgg;
  month?: UsageAgg;
  year?: UsageAgg;
};

export interface UsageProviderEntry {
  name: string;
  label: string;
  mode?: string | null;
  window?: PlanWindow | null;
  window_note?: string | null;
  periods?: PeriodTotals | null;
  /** Per-account breakdown (auth profiles): present only when the provider
   * has account profiles configured. `id` "default" = the ambient login. */
  accounts?: Array<{ id: string; label: string; periods?: PeriodTotals | null }>;
}

export interface UsageWindows extends PeriodTotals {
  providers?: UsageProviderEntry[];
  default?: string | null;
}

/** Interpret the loosely-typed useUsage() payload as the shape above. */
export function asUsageWindows(data: UsageResponse | null | undefined): UsageWindows | null {
  return data ? (data as unknown as UsageWindows) : null;
}

// [label, value] rows; the optional third entry is an extra class ("wrap")
// on the value cell, same as _usageTableEl's r[2].
export type UsageRowData = [string, string] | [string, string, string];

// --- Notes (verbatim from the vanilla fragment) ---------------------------- //

export const USAGE_NOTE =
  "Totals are cumulative across every turn — each turn re-reads " +
  "the whole context, so cache-read dwarfs real input. Cost is estimated from " +
  "daily feed prices.";

export const USAGE_WINDOW_NOTE: Record<"day" | "week" | "month" | "year", string> = {
  day: "Rolling last 24 hours across all sessions. Cost estimated from daily feed prices.",
  week: "Rolling last 7 days across all sessions. Cost estimated from daily feed prices.",
  month: "Rolling last 30 days across all sessions. Cost estimated from daily feed prices.",
  year: "Rolling last 365 days across all sessions. Cost estimated from daily feed prices.",
};

// Shown alongside the breakdown when the default CLI runs on a subscription
// plan (mode "windowed" from /api/usage): marginal spend is $0 there, so
// dollars are only an API-equivalent yardstick.
export const USAGE_NOTE_PLAN =
  "You're on a subscription plan — dollar figures are " +
  "API-equivalent estimates of what this usage would cost pay-per-token, not " +
  "billed spend. Window % is estimated against your budget in Settings; the " +
  "provider's real server-side meter may differ.";

// --- Periods --------------------------------------------------------------- //

export type UsagePeriodKey = "session" | "day" | "week" | "month" | "year";

export const USAGE_PERIODS: ReadonlyArray<readonly [UsagePeriodKey, string]> = [
  ["session", "Session"],
  ["day", "Day"],
  ["week", "Week"],
  ["month", "Month"],
  ["year", "Year"],
];

export const ZERO_AGG: UsageAgg = { cost: 0, in: 0, out: 0, cache_read: 0, cache_write: 0 };

// --- Per-session helpers ---------------------------------------------------- //

/** Collapsed headline for one session: "Codex · ~$0.62 · 128k/200k". The
 * provider prefix tells you at a glance exactly who is serving this window. */
export function usageHeadline(info: Instance): string {
  const cost = "~" + fmtUsd(info.tokens_cost || 0);
  const win = info.tokens_ctx_window || 0;
  const body = win ? cost + " · " + fmtTokens(info.tokens_ctx || 0) + "/" + fmtTokens(win) : cost;
  const p = provLabel(info.provider);
  return p ? p + " · " + body : body;
}

/** [label, value] rows for the expanded dropdown of one session. `plan` is
 * isPlanMode(usage) — on a subscription plan the per-session dollars aren't
 * billed spend, just the API-equivalent yardstick (still useful — it's how
 * you spot which session eats the window). */
export function usageRows(info: Instance, plan = false): UsageRowData[] {
  const rows: UsageRowData[] = [
    [plan ? "≈ API-equiv. cost" : "Est. cost", "~" + fmtUsd(info.tokens_cost || 0)],
  ];
  const win = info.tokens_ctx_window || 0;
  if (win) {
    const used = info.tokens_ctx || 0;
    rows.push([
      "Context",
      fmtTokens(used) + " / " + fmtTokens(win) + " (" + Math.round((100 * used) / win) + "%)",
    ]);
  }
  rows.push(["Input", (info.tokens_in || 0).toLocaleString()]);
  rows.push(["Output", (info.tokens || 0).toLocaleString()]);
  rows.push(["Cache read", (info.tokens_cache_read || 0).toLocaleString()]);
  rows.push(["Cache write", (info.tokens_cache_write || 0).toLocaleString()]);
  if (info.tokens_model) rows.push(["Model", info.tokens_model]);
  return rows;
}

// --- Plan-window helpers ----------------------------------------------------- //

/** "2h 11m" / "~7m" until an epoch-seconds deadline; "now" once past. */
export function fmtResetIn(endEpochSec: number): string {
  const ms = endEpochSec * 1000 - Date.now();
  return ms <= 0 ? "now" : fmtDurationShort(ms);
}

/** True when any reported provider runs on a subscription plan (windowed). */
export function isPlanMode(usage: UsageWindows | null | undefined): boolean {
  return usageProviders(usage).some((p) => p.mode === "windowed");
}

/** Per-provider usage entries from /api/usage ({name,label,mode,window,
 * window_note}). */
export function usageProviders(usage: UsageWindows | null | undefined): UsageProviderEntry[] {
  return usage && Array.isArray(usage.providers) ? usage.providers : [];
}

/** [label, value] rows describing ONE plan window (used %, reset, weekly cap,
 * billed extra-usage) — shared by the per-provider tab and, compactly, the
 * combined view. `w` is a provider's `.window` (may be null = fresh/idle). */
export function planStripRows(w: PlanWindow | null | undefined): UsageRowData[] {
  const rows: UsageRowData[] = [];
  if (w && w.end) {
    const live = w.source === "live"; // provider's own meter vs our estimate
    if (w.groups && w.groups.length) {
      // Per-model-group quotas (Antigravity): one row per group beats a
      // single ambiguous "plan window" number.
      w.groups.forEach((g) => {
        if (g.percent_used == null && !g.end) return;
        let v = g.percent_used != null ? g.percent_used.toFixed(0) + "% used" : "active";
        if (g.end) v += " · resets " + fmtResetIn(g.end);
        rows.push([g.label || "Quota", v]);
      });
    } else if (w.percent_used != null) {
      rows.push(["Plan window used", w.percent_used.toFixed(0) + "%" + (live ? "" : " (est.)")]);
    }
    if (!(w.groups && w.groups.length)) {
      const at = new Date(w.end * 1000).toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
      });
      rows.push(["Window resets", at + " (in " + fmtResetIn(w.end) + ")"]);
    }
    if (w.cost != null) rows.push(["Window cost (≈API)", "~" + fmtUsd(w.cost)]);
    if (w.weekly && w.weekly.percent_used != null) {
      let v = w.weekly.percent_used.toFixed(0) + "% used";
      if (w.weekly.end) {
        v +=
          " · resets " +
          new Date(w.weekly.end * 1000).toLocaleDateString([], { weekday: "short" });
      }
      rows.push(["Weekly cap", v]);
    }
    if (w.extra && w.extra.limit) {
      rows.push([
        "Extra usage (billed)",
        "$" + w.extra.used.toFixed(0) + " / $" + w.extra.limit.toFixed(0) + " this month",
      ]);
    }
  } else {
    rows.push(["Plan window", "fresh — your next message starts a new one", "wrap"]);
  }
  return rows;
}

// --- Overall breakdown helpers ---------------------------------------------- //

/** Token/cost rows for one period. `sessionAgg` covers the live "session"
 * period (summed from current panes); `periods` is the {day,week,month,year}
 * history object (combined top-level, or one provider's own `.periods`).
 * `plan` picks the cost label (API-equivalent yardstick on a subscription). */
export function periodRows(
  sessionAgg: UsageAgg | null | undefined,
  periods: PeriodTotals | null | undefined,
  period: UsagePeriodKey,
  plan: boolean,
): UsageRowData[] {
  const t = period === "session" ? sessionAgg : periods && periods[period];
  if (!t) return [["—", "no data yet"]];
  return [
    [plan ? "≈ API-equiv. cost" : "Est. cost", "~" + fmtUsd(t.cost || 0)],
    ["Input", (t.in || 0).toLocaleString()],
    ["Output", (t.out || 0).toLocaleString()],
    ["Cache read", (t.cache_read || 0).toLocaleString()],
    ["Cache write", (t.cache_write || 0).toLocaleString()],
  ];
}

/** One compact "remaining" line for a provider in the combined view. */
export function providerSummaryRow(p: UsageProviderEntry): UsageRowData {
  if (p.mode === "windowed") {
    const w = p.window;
    if (w && w.end) {
      const left =
        w.percent_used != null ? Math.max(0, 100 - w.percent_used).toFixed(0) + "% left" : "active";
      return [p.label, left + " · resets " + fmtResetIn(w.end)];
    }
    return [p.label, "fresh window"];
  }
  return [p.label, "metered (own API key)"];
}

// --- localStorage persistence (same keys as the vanilla app) --------------- //

export function loadUsagePeriod(): UsagePeriodKey {
  try {
    const raw = localStorage.getItem("mindflock.usagePeriod") || "session";
    return USAGE_PERIODS.some(([k]) => k === raw) ? (raw as UsagePeriodKey) : "session";
  } catch {
    return "session";
  }
}

export function saveUsagePeriod(key: UsagePeriodKey): void {
  try {
    localStorage.setItem("mindflock.usagePeriod", key);
  } catch {
    /* ignore */
  }
}

/** Selected tab in the overall-usage popover: "combined" or "p:<provider>". */
export function loadUsageTab(): string {
  try {
    return localStorage.getItem("mindflock.usageTab") || "combined";
  } catch {
    return "combined";
  }
}

export function saveUsageTab(key: string): void {
  try {
    localStorage.setItem("mindflock.usageTab", key);
  } catch {
    /* ignore */
  }
}
