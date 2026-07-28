/** Server state via TanStack Query. The instances poll is the SPA's heartbeat
 * (4s visible / 30s hidden — the server only computes *_changed events while
 * something polls, so the cadence is a feature, not laziness). Each poll also
 * feeds window.mindflock.__setSessions so addons see the same snapshot. */

import { QueryClient, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Config, DevicesResponse, Instance, UsageResponse } from "../api/types";

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

/* --------------------------------------------------------------------------
 * Settings panels: assigned tickets, open PRs, open issues.
 *
 * Each one is an upstream fan-out (GitHub / the ticket sources) that the
 * server caches and serves stale-while-revalidate. Holding them here rather
 * than in per-screen state is what removes the wait: the settings dialog
 * unmounts on close and screens unmount when you switch, so component state
 * meant every visit started from an empty panel and a spinner.
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

/** A settings panel's list, cached across dialog opens.
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

/** Warm all three panels when the settings dialog opens, so navigating to one
 * finds it loaded. A no-op for panels whose data is still fresh. */
export function prefetchSettingsPanels() {
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
