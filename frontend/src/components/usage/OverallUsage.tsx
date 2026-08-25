/** Ports the overall usage summary: the sidebar-header pill from
 * renderUsagePill (092-sidebar-render.js, markup from partials/040-sidebar.html)
 * plus the expanded popover from renderOverall / _periodBar /
 * providerSummaryRow / periodRows / toggleOverallUsage (050-usage-cost.js).
 * /api/usage comes from useUsage() (60s poll, replacing ensureUsageWindows). */

import { useEffect, useMemo, useReducer, useRef, useState } from "react";
import { refreshUsage, useInstances, useUsage } from "../../state/queries";
import { useUi } from "../../state/store";
import { fmtUsd } from "../../lib/format";
import type { Instance } from "../../api/types";
import { UsagePopNote, UsagePopTable, UsagePopover } from "./UsagePopover";
import {
  USAGE_NOTE,
  USAGE_NOTE_PLAN,
  USAGE_PERIODS,
  USAGE_WINDOW_NOTE,
  ZERO_AGG,
  asUsageWindows,
  isPlanMode,
  fmtResetIn,
  loadUsagePeriod,
  loadUsageTab,
  periodRows,
  planStripRows,
  providerSummaryRow,
  saveUsagePeriod,
  saveUsageTab,
  usageProviders,
  type UsageAgg,
  type UsagePeriodKey,
  type UsageProviderEntry,
  type UsageWindows,
} from "./usageModel";

/** Live "session" aggregates summed from the current instances: the combined
 * total plus a per-provider map (feeds each provider tab's Session row). */
function aggregate(instances: Instance[]): {
  agg: UsageAgg;
  aggByProvider: Record<string, UsageAgg>;
} {
  const agg: UsageAgg = { cost: 0, in: 0, out: 0, cache_read: 0, cache_write: 0 };
  const aggByProvider: Record<string, UsageAgg> = {};
  instances.forEach((i) => {
    const k = i.provider || i.program || "generic";
    const a =
      aggByProvider[k] ||
      (aggByProvider[k] = { cost: 0, in: 0, out: 0, cache_read: 0, cache_write: 0 });
    const cost = i.tokens_cost || 0;
    const tin = i.tokens_in || 0;
    const tout = i.tokens || 0;
    const cr = i.tokens_cache_read || 0;
    const cw = i.tokens_cache_write || 0;
    a.cost += cost;
    a.in += tin;
    a.out += tout;
    a.cache_read += cr;
    a.cache_write += cw;
    agg.cost += cost;
    agg.in += tin;
    agg.out += tout;
    agg.cache_read += cr;
    agg.cache_write += cw;
  });
  return { agg, aggByProvider };
}

/** Collapsed pill text + tooltip (port of renderUsagePill's head/title logic).
 * Tracks the FOCUSED pane's provider, falling back to the default provider:
 *   plan, active window  ->  "Codex: 94% left · resets 3h 15m"
 *   plan, idle window    ->  "Claude: fresh window"
 *   metered              ->  "Aider ~$4.20"   (real marginal spend) */
function pillText(
  usage: UsageWindows | null,
  provs: UsageProviderEntry[],
  instances: Instance[],
  focused: string | null,
  agg: UsageAgg,
  aggByProvider: Record<string, UsageAgg>,
): { head: string; title: string } {
  let head = "Usage  ~" + fmtUsd(agg.cost);
  let title = "Total estimated cost & token usage across all sessions — click for the breakdown";
  const focusedInst = instances.find((i) => i.title === focused);
  const focusedProv = focusedInst ? focusedInst.provider || focusedInst.program : null;
  // Prefer the focused pane's provider; else the default provider.
  let target = focusedProv ? provs.find((p) => p.name === focusedProv) || null : null;
  if (!target && usage && usage.default) {
    target = provs.find((p) => p.name === usage.default) || null;
  }
  if (target) {
    const tag = provs.length > 1 ? target.label + ": " : "";
    if (target.mode === "windowed") {
      const w = target.window;
      if (w && w.end) {
        const left =
          w.percent_used != null
            ? Math.max(0, 100 - w.percent_used).toFixed(0) + "% left · "
            : "";
        head = tag + left + "resets " + fmtResetIn(w.end);
        // Window exhausted + extra-usage credits flowing: the one moment on
        // a plan where tokens ARE real billed dollars — say so.
        if (w.percent_used != null && w.percent_used >= 99.5 && w.extra && w.extra.limit) {
          head = tag + "on credits ($" + w.extra.used.toFixed(0) + ") · resets " + fmtResetIn(w.end);
        }
        title =
          target.label +
          " plan window" +
          (focusedProv ? " (focused session)" : " (default)") +
          " — click for the breakdown";
      } else {
        head = tag + "fresh window";
        title =
          "No active " + target.label + " window — your next message starts one. Click for the breakdown";
      }
    } else {
      // Metered: this provider's dollars ARE real spend — show them.
      const c = (aggByProvider[target.name] || ZERO_AGG).cost || 0;
      head = target.label + " ~" + fmtUsd(c);
      title =
        target.label + " runs on your own API key — real marginal spend. Click for the breakdown";
    }
  }
  return { head, title };
}

export function OverallUsage() {
  const { data: instancesData } = useInstances();
  const instances = useMemo(() => instancesData ?? [], [instancesData]);
  const { data: usageData } = useUsage(instances.length > 0);
  const usage = asUsageWindows(usageData);
  const focused = useUi((s) => s.focused);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const [open, setOpen] = useState(false);
  const [period, setPeriodState] = useState<UsagePeriodKey>(loadUsagePeriod);
  const [tab, setTabState] = useState<string>(loadUsageTab);

  // Vanilla repainted the pill every 4s poll so the reset countdown ticks even
  // when the payload itself is unchanged (query structural sharing would
  // otherwise freeze it). A 30s tick matches the label's minute granularity.
  const [, tick] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    const id = setInterval(tick, 30_000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (!instances.length) setOpen(false);
  }, [instances.length]);

  const { agg, aggByProvider } = useMemo(() => aggregate(instances), [instances]);
  const provs = usageProviders(usage);
  const { head, title } = pillText(usage, provs, instances, focused, agg, aggByProvider);

  const setPeriod = (key: UsagePeriodKey) => {
    saveUsagePeriod(key);
    setPeriodState(key);
  };
  const setTab = (key: string) => {
    saveUsageTab(key);
    setTabState(key);
  };

  // Resolve the active tab to a specific provider entry, or null = combined.
  let active: UsageProviderEntry | null = null;
  if (tab.indexOf("p:") === 0) {
    const nm = tab.slice(2);
    active = provs.find((p) => p.name === nm) || null;
  }

  // Whether ANY provider in play is on a subscription plan (combined caveat).
  const plan = isPlanMode(usage);

  return (
    <>
      <button
        id="overall-usage"
        ref={btnRef}
        className={"overall-usage" + (instances.length ? "" : " hidden")}
        type="button"
        title={title}
        onClick={(ev) => {
          ev.stopPropagation();
          setOpen((o) => {
            // Opening the breakdown is the one moment the user is deliberately
            // reading these numbers, so it refetches rather than showing
            // whatever the last poll left behind.
            if (!o) refreshUsage();
            return !o;
          });
        }}
      >
        <span className="usage-head">{head}</span>
        <span className="caret">▾</span>
      </button>
      {open && btnRef.current && (
        <UsagePopover anchor={btnRef.current} onClose={() => setOpen(false)}>
          {/* Provider / Combined tab bar. Only shown once we know the
              providers (the pre-fetch first paint degrades to the plain
              view). */}
          {provs.length > 0 && (
            <div className="usage-tabs">
              <button
                type="button"
                className={"usage-tab" + (tab === "combined" ? " active" : "")}
                onClick={(ev) => {
                  ev.stopPropagation();
                  setTab("combined");
                }}
              >
                Combined
              </button>
              {provs.map((p) => (
                <button
                  key={p.name}
                  type="button"
                  className={"usage-tab" + (tab === "p:" + p.name ? " active" : "")}
                  onClick={(ev) => {
                    ev.stopPropagation();
                    setTab("p:" + p.name);
                  }}
                >
                  {p.label}
                </button>
              ))}
            </div>
          )}
          {/* The period toggle sits directly under the provider tabs in EVERY
              view, so the two button rows always read as one stacked menu. */}
          <div className="usage-periods">
            {USAGE_PERIODS.map(([key, label]) => (
              <button
                key={key}
                type="button"
                className={"usage-period" + (key === period ? " active" : "")}
                onClick={(ev) => {
                  ev.stopPropagation();
                  setPeriod(key);
                }}
              >
                {label}
              </button>
            ))}
          </div>
          {active ? (
            // --- Single-provider view: plan window (if any) + THIS provider's
            // own Session/Day/Week/… token+cost breakdown.
            <>
              <div className="usage-pop-head">
                {active.label + (active.mode === "windowed" ? " · plan window" : " · metered")}
              </div>
              {active.mode === "windowed" && (
                <UsagePopTable rows={planStripRows(active.window)} />
              )}
              <UsagePopTable
                rows={periodRows(
                  aggByProvider[active.name] || ZERO_AGG,
                  active.periods,
                  period,
                  active.mode === "windowed",
                )}
              />
              <UsagePopNote text={period === "session" ? USAGE_NOTE : USAGE_WINDOW_NOTE[period]} />
              {active.mode === "windowed" ? (
                <>
                  {active.window_note ? <UsagePopNote text={active.window_note} /> : null}
                  <UsagePopNote text={USAGE_NOTE_PLAN} />
                </>
              ) : (
                <UsagePopNote
                  text={
                    active.window_note ||
                    "Runs on your own API key — the model API's rate limits apply; " +
                      "no MindFlock-managed window."
                  }
                />
              )}
            </>
          ) : (
            // --- Combined view: per-provider remaining + combined totals.
            <>
              {provs.length > 0 ? (
                <UsagePopTable rows={provs.map(providerSummaryRow)} />
              ) : null}
              <UsagePopTable rows={periodRows(agg, usage, period, plan)} />
              <UsagePopNote text={period === "session" ? USAGE_NOTE : USAGE_WINDOW_NOTE[period]} />
              {/* The combined dollars span every provider's sessions; note the
                  plan caveat whenever any provider is on a subscription plan. */}
              {plan && <UsagePopNote text={USAGE_NOTE_PLAN} />}
            </>
          )}
        </UsagePopover>
      )}
    </>
  );
}
