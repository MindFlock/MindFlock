/** Server state for ship lanes: the runs behind the rail's group headers, and
 * `/api/outbox` behind the bell's waiting rows.
 *
 * Both routes are new (SPEC §5). A server that predates them answers 404; that
 * is read as "no runs" / "nothing waiting" — the rail stays flat and the bell
 * lists only the sessions' own items — and polled rarely, so an older server's log isn't a wall of 404s.
 * Run events invalidate both at once (`run.changed` is the "refetch" event),
 * and so does a fast-track change, which is what moves a session along its
 * lane. */

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import type { OutboxResponse, RunDTO, RunSummary } from "../api/types";
import { needsRunDetail, RUN_DONE_STATES, type RunInfo } from "../lib/runs";
import { queryClient, type EventEnvelope } from "./queries";
import { toast } from "../lib/toast";

/** While a group is live: the server driver's own cadence (RUN_INTERVAL_S). */
export const RUNS_LIVE_MS = 5_000;
/** Nothing live (or the tab hidden): just enough to notice a new group. */
export const RUNS_IDLE_MS = 30_000;
/** A server without the route: check back now and then (an upgrade). */
const ROUTE_MISSING_MS = 120_000;

const missing = (e: unknown) => e instanceof ApiError && (e.status === 404 || e.status === 405);

/** null = this server has no runs route. */
async function fetchRuns(): Promise<RunInfo[] | null> {
  let list: RunSummary[];
  try {
    list = (await api<{ runs?: RunSummary[] }>("/api/runs"))?.runs || [];
  } catch (e) {
    if (missing(e)) return null;
    throw e;
  }
  // Tasks only for groups still moving: a finished group's header needs its
  // counts, which the summary has, and nothing is queued in it any more —
  // except a recent one-for-all / split group, whose family rows keep reading
  // their merged-back pieces and the release from it (needsRunDetail).
  const nowS = Date.now() / 1000;
  const live = list.filter((r) => needsRunDetail(r, nowS));
  const details = await Promise.all(
    live.map((r) =>
      api<{ run?: RunDTO }>("/api/runs/" + encodeURIComponent(r.id)).then(
        (d) => d?.run || null,
        () => null
      )
    )
  );
  const byId = new Map(details.filter((d): d is RunDTO => !!d).map((d) => [d.id, d]));
  return list.map((r) => ({ ...r, ...(byId.get(r.id) || {}) }));
}

function liveRuns(data: RunInfo[] | null | undefined): boolean {
  return !!data && data.some((r) => !RUN_DONE_STATES.has(r.state));
}

/** Every run, with tasks for the live ones. `data` null = no runs route. */
export function useRuns() {
  useEffect(bridgeRunEvents, []);
  return useQuery({
    queryKey: ["runs"],
    queryFn: fetchRuns,
    refetchInterval: (q) =>
      q.state.data === null
        ? ROUTE_MISSING_MS
        : liveRuns(q.state.data) && !document.hidden
          ? RUNS_LIVE_MS
          : RUNS_IDLE_MS,
    refetchIntervalInBackground: true,
    placeholderData: (prev) => prev,
    retry: false,
  });
}

/** One group in full — the lead's Thread tab reads its plan, its pieces and
 * its release card from here. Refetched on every run event (the bridge
 * invalidates ["run", …] with the list) and every few seconds while live. */
export function useRun(id: string | null | undefined) {
  useEffect(bridgeRunEvents, []);
  return useQuery({
    queryKey: ["run", id || ""],
    queryFn: async () => {
      try {
        return (await api<{ run?: RunDTO }>("/api/runs/" + encodeURIComponent(String(id))))?.run || null;
      } catch (e) {
        if (missing(e)) return null;
        throw e;
      }
    },
    enabled: !!id,
    refetchInterval: (q) =>
      q.state.data && RUN_DONE_STATES.has(q.state.data.state) ? RUNS_IDLE_MS : document.hidden ? RUNS_IDLE_MS : RUNS_LIVE_MS,
    refetchIntervalInBackground: true,
    // Only the SAME group's last answer: another lead's plan must never flash.
    placeholderData: (prev, prevQuery) => (prevQuery?.queryKey[1] === (id || "") ? prev : undefined),
    retry: false,
  });
}

/** null = this server has no `/api/outbox` route. */
async function fetchOutbox(): Promise<OutboxResponse | null> {
  try {
    return await api<OutboxResponse>("/api/outbox?group=all");
  } catch (e) {
    if (missing(e)) return null;
    throw e;
  }
}

/** `GET /api/outbox?group=all` — every group in one response: the bell's
 * waiting rows and the finished groups' summaries (a group header's "Copy
 * summary"). Nothing else in the UI reads it. */
export function useOutbox() {
  useEffect(bridgeRunEvents, []);
  return useQuery({
    queryKey: ["outbox"],
    queryFn: fetchOutbox,
    refetchInterval: (q) => (q.state.data === null ? ROUTE_MISSING_MS : document.hidden ? RUNS_IDLE_MS : RUNS_LIVE_MS),
    refetchIntervalInBackground: true,
    placeholderData: (prev) => prev,
    retry: false,
  });
}

// --- The rule switches, for the client's own channels -------------------------
//
// The bell is the THIRD channel (after ntfy/desktop and the shell hooks) and
// has no server-side gate, so it checks the same per-rule switches Settings →
// Notifications flips — and so do the toasts. One cached read of the notify
// addon's config; a rule it doesn't list (an older server, the addon off)
// counts as on, which is every run rule's default.

interface NotifyConfig {
  rules?: Array<{ id: string; enabled?: boolean }>;
}

export function useNotifyConfig() {
  return useQuery({
    queryKey: ["notify-config"],
    queryFn: () => api<NotifyConfig>("/api/notify/config").catch(() => ({ rules: [] })),
    staleTime: 60_000,
    refetchInterval: 120_000,
    retry: false,
  });
}

/** Is notify rule `id` switched on? "" (a bell-only row) always is. */
export function ruleOn(id: string): boolean {
  if (!id) return true;
  const rule = queryClient.getQueryData<NotifyConfig>(["notify-config"])?.rules?.find((r) => r.id === id);
  return !rule || rule.enabled !== false;
}

/** Name / lane lookups off the cached runs, for phrasing a run event. */
export const runLookups = {
  name: (id: string) => (queryClient.getQueryData<RunInfo[] | null>(["runs"]) || []).find((r) => r.id === id)?.name || "",
  lane: (id: string) =>
    (queryClient.getQueryData<RunInfo[] | null>(["runs"]) || []).find((r) => r.id === id)?.policy?.lane || "",
};

/** After anything that moves a run or a lane. */
export function refreshRuns() {
  void queryClient.invalidateQueries({ queryKey: ["runs"] });
  void queryClient.invalidateQueries({ queryKey: ["run"] });
  return queryClient.invalidateQueries({ queryKey: ["outbox"] });
}

let runsBridged = false;
/** Run events → refetch. Replayed history refetches too: it is cheap, and a
 * reconnect after a restart is exactly when the cached groups are stale. */
function bridgeRunEvents() {
  const ev = window.mindflock?.events;
  if (runsBridged || !ev) return; // events.js may not have loaded yet
  runsBridged = true;
  const bump = (_env: EventEnvelope) => void refreshRuns();
  for (const name of ["run.changed", "run.needs_you", "run.task_shipped", "run.finished", "session.autopilot_changed"])
    ev.subscribe(name, bump);
  // An auto-split whose lead decided not to split: its group vanishes from
  // the rail, so say where the work went. Never for replayed history.
  ev.subscribe("run.changed", (env: EventEnvelope) => {
    if (env.data?.state !== "dissolved") return;
    if (typeof ev.isReplay === "function" && ev.isReplay(env)) return;
    const lead = String(env.data?.lead || "");
    if (lead) toast(dissolvedText(lead, String(env.data?.why || "")), { duration: 7000 });
  });
}

/** The toast for an auto-split that stayed one session. */
export function dissolvedText(lead: string, why: string): string {
  const reason = why.trim().replace(/\s+/g, " ");
  return (
    `${lead} didn't split it — it's doing the task itself` +
    (reason ? `: ${reason.length > 120 ? reason.slice(0, 119) + "…" : reason}` : "")
  );
}
