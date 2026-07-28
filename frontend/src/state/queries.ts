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
