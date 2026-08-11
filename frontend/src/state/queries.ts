/** Server state via TanStack Query. The instances poll is the SPA's heartbeat
 * (4s visible / 30s hidden — the server only computes *_changed events while
 * something polls, so the cadence is a feature, not laziness). Each poll also
 * feeds window.mindflock.__setSessions so addons see the same snapshot. */

import { useEffect } from "react";
import { QueryClient, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Config, DevicesResponse, Instance, TrafficResponse, UsageResponse } from "../api/types";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 2_000,
    },
  },
});

const POLL_VISIBLE_MS = 4_000;
const POLL_HIDDEN_MS = 30_000;

function pollInterval() {
  return document.hidden ? POLL_HIDDEN_MS : POLL_VISIBLE_MS;
}

declare global {
  interface Window {
    mindflock?: {
      __setSessions?: (arr: unknown[]) => void;
      toast?: (msg: string, opts?: { onClick?: () => void; duration?: number }) => void;
      events?: {
        subscribe(name: string, cb: (env: EventEnvelope) => void): () => void;
        onStatus(cb: (s: "connected" | "disconnected") => void): () => void;
        isReplay(env: EventEnvelope): boolean;
        connected: boolean;
        lastSeq: number;
      };
    };
    reloadProviderPicker?: () => void;
  }
}

export interface EventEnvelope {
  seq: number;
  event: string;
  session: string;
  old: string | null;
  new: string | null;
  ts: number;
  data: Record<string, unknown>;
}

export function useInstances() {
  return useQuery({
    queryKey: ["instances"],
    queryFn: async () => {
      const list = await api<Instance[]>("/api/instances");
      window.mindflock?.__setSessions?.(list);
      return list;
    },
    refetchInterval: pollInterval,
    refetchIntervalInBackground: true,
    placeholderData: (prev) => prev,
  });
}

export function useConfig() {
  return useQuery({
    queryKey: ["config"],
    queryFn: () => api<Config>("/api/config"),
    staleTime: 60_000,
  });
}

export function refreshConfig() {
  return queryClient.invalidateQueries({ queryKey: ["config"] });
}

export function useDevices() {
  return useQuery({
    queryKey: ["devices"],
    queryFn: () => api<DevicesResponse>("/api/devices"),
    refetchInterval: 30_000,
    placeholderData: (prev) => prev,
  });
}

export function useUsage(enabled = true) {
  return useQuery({
    queryKey: ["usage"],
    queryFn: () => api<UsageResponse>("/api/usage"),
    refetchInterval: 60_000,
    enabled,
    placeholderData: (prev) => prev,
  });
}

/** Imperative refresh after any action that changes the session set. */
export function refreshInstances() {
  return queryClient.invalidateQueries({ queryKey: ["instances"] });
}

/** Settings → Site traffic (dev shell only): stars/forks, per-release
 * download counts, and click totals for the /go/ tracked links. The backend
 * addon already caches this for 5 minutes against GitHub's rate limit, so
 * the query's own staleTime just avoids a redundant fetch on every dialog
 * reopen within that window. `enabled` keeps this from ever firing for a
 * user who can't see the screen — no point spending the addon's cache TTL on
 * requests nobody reads. */
export function useTraffic(enabled: boolean, days = 90) {
  return useQuery({
    queryKey: ["traffic", days],
    queryFn: () => api<TrafficResponse>("/api/traffic?days=" + days),
    enabled,
    staleTime: 60_000,
    placeholderData: (prev) => prev,
    retry: false,
  });
}

/** The Refresh button on Site traffic: bypass the addon's cache. */
export function refreshTraffic(days = 90) {
  return queryClient.fetchQuery({
    queryKey: ["traffic", days],
    queryFn: () => api<TrafficResponse>("/api/traffic?days=" + days + "&refresh=1"),
    staleTime: 0,
    retry: false,
  });
}

/** Merge new fields into ONE cached session row, with no round trip.
 *
 * The zero-latency half of the freshness story. `refreshInstances()` cannot make
 * a stage change appear sooner no matter when it is called: GET /api/instances
 * serves the server's published tick snapshot for up to 10s and never recomputes
 * probes inline, so an invalidate inside that window returns the identical row.
 * Patching writes what we already learned (from `/stage`, or from a
 * `session.stage_changed` event) straight into the cache the UI renders. */
export function patchInstance(title: string, patch: Partial<Instance>) {
  queryClient.setQueryData<Instance[]>(["instances"], (rows) =>
    rows?.map((r) => (r.title === title ? { ...r, ...patch } : r))
  );
}

/* --------------------------------------------------------------------------
 * Intake panels: assigned tickets, open PRs, open issues.
 *
 * Each one is an upstream fan-out (GitHub / the ticket sources) that the
 * server caches and serves stale-while-revalidate. Holding them here rather
 * than in per-tab state is what removes the wait: the Intake dialog unmounts on
 * close and tabs unmount when you switch, so component state meant every visit
 * started from an empty panel and a spinner. It is also what lets the tab strip
 * show a count without a second request.
 * ------------------------------------------------------------------------ */

/** Matches the server's own fresh window, so a mount inside it is answered
 * from this cache instead of making a round trip to be told the same thing. */
const PANEL_STALE_MS = 20_000;
/** How soon to pull the fresh copy after the server hands us a stale one. */
const PANEL_STALE_RETRY_MS = 2_000;
/** Keep panels well past the default 5min: "cached across dialog opens" has to
 * survive lunch, or the wait comes back exactly when it feels worst. */
const PANEL_GC_MS = 60 * 60_000;

export const PANELS = {
  tickets: "/api/tickets",
  "github-prs": "/api/github/prs",
  "github-issues": "/api/github/issues",
} as const;

export type PanelKey = keyof typeof PANELS;

/** A work panel's list, cached across dialog opens.
 *
 * `placeholderData` keeps the previous rows on screen while the refetch runs,
 * so reopening a screen shows the last list immediately instead of blanking.
 * `refresh()` is the Refresh button: `?fresh=1` tells the server to skip its
 * cache and actually sweep, so the click means what it says. */
export function usePanelQuery<T extends { stale?: boolean }>(key: PanelKey) {
  const q = useQuery<T>({
    queryKey: [key],
    queryFn: () => api<T>(PANELS[key]),
    staleTime: PANEL_STALE_MS,
    gcTime: PANEL_GC_MS,
    placeholderData: (prev) => prev,
    // An unconfigured integration answers 502; retrying it just doubles the
    // requests to say the same thing, and the panel has a Refresh button.
    retry: false,
    // Served something the server is already replacing → come back for it once.
    refetchInterval: (query) =>
      query.state.data?.stale ? PANEL_STALE_RETRY_MS : false,
  });
  /** The Refresh button: skip both caches and wait for a real upstream sweep.
   * The rejection is swallowed because the failure is already in query state,
   * which is what renders the error banner. */
  const refresh = () =>
    queryClient
      .fetchQuery({
        queryKey: [key],
        queryFn: () => api<T>(PANELS[key] + "?fresh=1"),
        staleTime: 0,
        retry: false,
      })
      .catch(() => undefined);
  return { ...q, refresh };
}

/** Warm all three panels, so opening Intake — or switching to a tab inside it —
 * finds the list already there, and so the tab strip's counts are filled in
 * before you get there, which is the point of putting them on the strip. A
 * no-op for panels whose data is still fresh. */
export function prefetchIntakePanels() {
  for (const key of Object.keys(PANELS) as PanelKey[]) {
    void queryClient.prefetchQuery({
      queryKey: [key],
      queryFn: () => api(PANELS[key]),
      staleTime: PANEL_STALE_MS,
      gcTime: PANEL_GC_MS,
      retry: false,
    });
  }
}

/** How often to re-warm the panels in the background.
 *
 * Under the server's 300s stale window, so there is always a cached payload for
 * it to hand back instantly while it revalidates — past that window the server
 * awaits a real upstream sweep, which is the slow open this loop exists to
 * prevent. Also well under the 1h client gcTime, so the cache never empties out
 * from under a page that has been sitting open. */
const PANEL_WARM_MS = 4 * 60_000;

/** Keep the Intake panels warm from app startup, not from dialog open.
 *
 * Prefetching when the dialog opens is already too late: the fan-out starts on
 * the same tick the panel mounts, so the first thing you see is a spinner over
 * an empty list. Warming from the shell means the rows are in the cache before
 * there is anything to render them into, and `placeholderData` in
 * `usePanelQuery` puts the last known list on screen immediately on every open
 * after that.
 *
 * Skipped while the page is hidden (a background tab must not spend the ticket
 * provider's rate limit on a list nobody is about to look at), and re-warmed on
 * the way back so returning to the window doesn't show minutes-old counts.
 * `enabled` is what keeps this off entirely for a user with no ticketing source
 * connected — every panel 502s for them, forever, on a four-minute timer. */
export function useIntakeWarm(enabled: boolean) {
  useEffect(() => {
    if (!enabled) return;
    const warm = () => {
      if (!document.hidden) prefetchIntakePanels();
    };
    warm();
    const timer = window.setInterval(warm, PANEL_WARM_MS);
    document.addEventListener("visibilitychange", warm);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", warm);
    };
  }, [enabled]);
}
