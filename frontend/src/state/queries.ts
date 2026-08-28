/** Server state via TanStack Query. The instances poll is the SPA's heartbeat
 * (4s visible / 30s hidden — the server only computes *_changed events while
 * something polls, so the cadence is a feature, not laziness). Each poll also
 * feeds window.mindflock.__setSessions so addons see the same snapshot. */

import { useEffect } from "react";
import { QueryClient, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { nudgeUsage } from "../lib/usageRefresh";
import { toast } from "../lib/toast";
import { errorPop } from "../lib/errorPop";
import { isVerifySession } from "../components/dialogs/verify";
import type { EffortCap } from "../lib/effort";
import type { ExtensionInfo, ExtensionSpec } from "../extensions/types";
import type {
  AuthProfilesResponse,
  Config,
  Json,
  DevicesResponse,
  Instance,
  TestPlansResponse,
  TicketingCatalogEntry,
  TicketingSource,
  TrafficResponse,
  UsageResponse,
} from "../api/types";

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

/** The installed coding CLIs, and which one runs when nothing else says.
 *
 * ONE query behind every provider question the app asks — the per-row Effort
 * ceiling, the per-source Agent picker — because they are all the same request,
 * and two query keys over one endpoint meant opening Intake fetched it twice.
 * Providers change only when one is added or edited, so it is warmed at startup
 * and read from cache thereafter. */
interface ProvidersResponse {
  providers?: Array<{ name?: string; effort?: EffortCap }>;
  default?: string;
}

const PROVIDERS_STALE_MS = 300_000;

function providersQuery() {
  return {
    queryKey: ["providers"],
    queryFn: () => api<ProvidersResponse>("/api/providers"),
    staleTime: PROVIDERS_STALE_MS,
  } as const;
}

/** Re-read the installed CLIs after one is added, edited or removed. */
export function refreshProviders() {
  return queryClient.invalidateQueries({ queryKey: ["providers"] });
}

/** Per-CLI thinking-effort capability, keyed by provider name.
 *
 * Shared through the query cache because it is read PER ROW (every work row's
 * Effort picker asks what the CLI it would launch can do) while being a property
 * of the install, not of the row — one fetch serves a whole list, and the rungs
 * only change when a provider is added or edited. */
export function useProviderEfforts() {
  return useQuery({
    ...providersQuery(),
    select: (d: ProvidersResponse) => {
      const out: Record<string, EffortCap> = {};
      for (const p of d?.providers || []) {
        if (p?.name && p.effort) out[p.name] = p.effort;
      }
      return out;
    },
  });
}

/** The CLI names a source's Agent picker offers, plus the app-wide default
 * (shown as the "unset" option's label, so the empty choice is never a
 * mystery). */
export function useAgentChoices() {
  return useQuery({
    ...providersQuery(),
    select: (d: ProvidersResponse) => ({
      names: (d?.providers || []).map((x) => x.name || "").filter(Boolean),
      fallback: d?.default || "",
    }),
  });
}

/** The extension manifests (Addon API v3), one row per addon whose manifest
 * carries a non-null `extension` — the sidebar bars, the palette entries, the
 * Settings screen and the runtime host all read this one cache. Same shape as
 * the providers query above: the manifest changes only on a server restart or
 * an enable/disable toggle (which calls refreshExtensions), so a long
 * staleTime keeps it from being refetched on every consumer mount. */
interface AddonsResponse {
  addons?: Array<{
    id?: string;
    label?: string;
    enabled?: boolean;
    origin?: string;
    extension?: ExtensionSpec | null;
  }>;
}

const EXTENSIONS_STALE_MS = 300_000;

function extensionsQuery() {
  return {
    queryKey: ["addons"],
    queryFn: () => api<AddonsResponse>("/api/addons"),
    staleTime: EXTENSIONS_STALE_MS,
  } as const;
}

export function useExtensions() {
  return useQuery({
    ...extensionsQuery(),
    select: (d: AddonsResponse): ExtensionInfo[] =>
      (d?.addons || [])
        .filter((a) => a && a.id && a.extension)
        .map((a) => ({
          id: String(a.id),
          label: a.label || String(a.id),
          // Absent on a pre-v3 server: an addon you can't disable is enabled.
          enabled: a.enabled !== false,
          origin: a.origin === "user" ? ("user" as const) : ("builtin" as const),
          extension: a.extension as ExtensionSpec,
        })),
  });
}

/** Re-read the manifests after an enable/disable toggle. */
export function refreshExtensions() {
  return queryClient.invalidateQueries({ queryKey: ["addons"] });
}

export function useDevices() {
  return useQuery({
    queryKey: ["devices"],
    queryFn: () => api<DevicesResponse>("/api/devices"),
    refetchInterval: 30_000,
    placeholderData: (prev) => prev,
  });
}

/** Imperative refresh of the usage pill's numbers. */
export function refreshUsage() {
  return queryClient.invalidateQueries({ queryKey: ["usage"] });
}

let usageBridged = false;
let usageRefreshedAt: number | null = null;
let usageTrailing: ReturnType<typeof setTimeout> | null = null;

function nudgeUsageNow() {
  const decision = nudgeUsage(Date.now(), usageRefreshedAt, usageTrailing != null);
  if (decision.action === "skip") return;
  if (decision.action === "refresh") {
    usageRefreshedAt = Date.now();
    refreshUsage();
    return;
  }
  usageTrailing = setTimeout(() => {
    usageTrailing = null;
    usageRefreshedAt = Date.now();
    refreshUsage();
  }, decision.delayMs);
}

/** Refresh the usage numbers when the server says usage changed, instead of
 * only when the poll happens to come round.
 *
 * The pill used to be a pure 60s poll with no focus refetch, so a turn that
 * burned through a chunk of the window could sit unreported for a minute — and
 * for as long as the window was hidden, since the interval is suspended then.
 * Usage changes for exactly one reason (an agent ran), and the event bus
 * already announces that, so the events drive the refresh:
 *
 * - ``session.activity_changed`` — a session started or finished working. The
 *   REPLAYED envelopes a reconnect delivers are ignored: they are history, not
 *   news, and ~100 of them would otherwise arrive at once.
 * - ``session.usage_restored`` — a limited session's window reopened, i.e. the
 *   percentage just dropped to zero. Showing an exhausted meter after the limit
 *   has lifted is the single most misleading state this pill can be in.
 * - Bus reconnect — while disconnected we missed every transition above, so the
 *   displayed number is of unknown age and gets replaced on principle.
 *
 * Installed once per page rather than per subscriber: the invalidation is
 * global, so a second listener would only duplicate requests. */
function bridgeUsageEvents() {
  if (usageBridged) return;
  const ev = window.mindflock?.events;
  if (!ev) return; // events.js hasn't loaded yet — a later mount installs it
  usageBridged = true;
  ev.subscribe("session.activity_changed", (env) => {
    if (typeof ev.isReplay === "function" && ev.isReplay(env)) return;
    nudgeUsageNow();
  });
  ev.subscribe("session.usage_restored", () => nudgeUsageNow());
  ev.onStatus((status) => {
    if (status === "connected") nudgeUsageNow();
  });
}

export function useUsage(enabled = true) {
  useEffect(bridgeUsageEvents, []);
  return useQuery({
    queryKey: ["usage"],
    queryFn: () => api<UsageResponse>("/api/usage"),
    // Halved from 60s, and now only the floor under the event-driven refreshes
    // above rather than the sole way the number ever moves.
    refetchInterval: 30_000,
    // Overrides the client-wide `false`. The interval does not run while the
    // window is hidden, so without this, coming back to a long-backgrounded app
    // showed a minutes-old percentage until the next tick — the worst case of
    // the delay this whole path is about.
    refetchOnWindowFocus: true,
    enabled,
    placeholderData: (prev) => prev,
  });
}

/** Auth profiles (Settings → Accounts) — the identity list the New dialog and
 * each pane's account chip render. Cheap (a settings read), so a modest
 * staleTime keeps the pickers fresh without polling. */
export function useAuthProfiles() {
  return useQuery({
    queryKey: ["auth-profiles"],
    queryFn: () => api<AuthProfilesResponse>("/api/settings/auth-profiles"),
    staleTime: 30_000,
    placeholderData: (prev) => prev,
  });
}

export function refreshAuthProfiles() {
  return queryClient.invalidateQueries({ queryKey: ["auth-profiles"] });
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

/* --------------------------------------------------------------------------
 * Intake metadata: the settings document and the ticketing source list.
 *
 * The panels above are the *rows*; these are the chrome those rows live in —
 * the source cards, the per-repo overrides, the Agent pickers. They used to be
 * component state fetched on mount, which meant every open of the dialog paid
 * for them again, in series, behind a "Loading…" that covered the whole tab.
 * Held here instead, warmed with the panels, so the first paint after a click
 * is the real one.
 * ------------------------------------------------------------------------ */

/** Nothing writes these but this app, and every writer pushes the server's echo
 * straight into the cache, so a re-read is only ever a guard against another
 * window (or the CLI) having changed something. */
const SETTINGS_STALE_MS = 30_000;

/** The whole settings document, shared by every dialog that edits it.
 *
 * `enabled` is the dialog's open state: closed dialogs must not poll, but the
 * cache they read is warmed from the shell, so opening one renders the fields
 * filled in rather than empty-then-populated. */
export function useSettingsDoc(enabled: boolean) {
  return useQuery({
    queryKey: ["settings"],
    queryFn: async () => (await api<{ settings?: Json }>("/api/settings"))?.settings || {},
    staleTime: SETTINGS_STALE_MS,
    enabled,
    placeholderData: (prev) => prev,
  });
}

/** Apply the server's echo from a settings write, with no round trip. */
export function putSettingsDoc(settings: Json) {
  queryClient.setQueryData<Json>(["settings"], settings);
}

/** Re-read settings, ignoring the stale window (the model's `reload()`). */
export function fetchSettingsDoc() {
  return queryClient.fetchQuery({
    queryKey: ["settings"],
    queryFn: async () => (await api<{ settings?: Json }>("/api/settings"))?.settings || {},
    staleTime: 0,
  });
}

/** The ticketing provider catalog: which platforms can be connected and what
 * each one asks for. A property of the build, not of the install — it cannot
 * change while the app is running, so it is fetched once and kept. */
export function useTicketingCatalog(enabled: boolean) {
  return useQuery({
    queryKey: ["ticketing-catalog"],
    queryFn: async () =>
      (await api<{ providers?: TicketingCatalogEntry[] }>(
        "/api/settings/providers/ticketing"
      ))?.providers || [],
    staleTime: Infinity,
    enabled,
  });
}

/** The connected ticketing sources — the cards on Intake → Tickets. */
export function useTicketingSources(enabled: boolean) {
  return useQuery({
    queryKey: ["ticketing-sources"],
    queryFn: async () =>
      (await api<{ sources?: TicketingSource[] }>("/api/settings/ticketing/sources"))
        ?.sources || [],
    staleTime: SETTINGS_STALE_MS,
    enabled,
    placeholderData: (prev) => prev,
  });
}

/** Apply a saved source list to the cache, so a reopen shows what you saved
 * rather than re-fetching to be told the same thing. */
export function putTicketingSources(sources: TicketingSource[]) {
  queryClient.setQueryData<TicketingSource[]>(["ticketing-sources"], sources);
}

/** Warm everything the Intake dialog renders *around* its rows.
 *
 * Separate from `prefetchIntakePanels` because it is on a different clock: the
 * panels go stale in seconds (upstream lists move), while these are settings
 * that only change when someone edits them here. Warmed once at startup, and
 * again on the way into the dialog for anything that has gone stale since.
 * Prefetch, not fetch: a warm cache costs nothing. */
export function prefetchIntakeMeta() {
  void queryClient.prefetchQuery({
    queryKey: ["settings"],
    queryFn: async () => (await api<{ settings?: Json }>("/api/settings"))?.settings || {},
    staleTime: SETTINGS_STALE_MS,
  });
  void queryClient.prefetchQuery({
    queryKey: ["ticketing-sources"],
    queryFn: async () =>
      (await api<{ sources?: TicketingSource[] }>("/api/settings/ticketing/sources"))
        ?.sources || [],
    staleTime: SETTINGS_STALE_MS,
  });
  void queryClient.prefetchQuery({
    queryKey: ["ticketing-catalog"],
    queryFn: async () =>
      (await api<{ providers?: TicketingCatalogEntry[] }>(
        "/api/settings/providers/ticketing"
      ))?.providers || [],
    staleTime: Infinity,
  });
  void queryClient.prefetchQuery(providersQuery());
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
  // The chrome the panels render into — settings, source cards, agent names —
  // is warmed once and unconditionally, unlike the panels themselves. It is
  // three cheap local reads rather than an upstream fan-out, and it is what
  // the dialog needs FIRST: with no source connected yet, the panels have
  // nothing to say and the source cards are the whole screen.
  useEffect(() => {
    prefetchIntakeMeta();
  }, []);

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

/* --------------------------------------------------------------------------
 * Verify: test plans for work that has gone live.
 *
 * Deliberately NOT a member of PANELS, even though it feeds a dialog that looks
 * like Intake. Everything PANELS carries — the `?fresh=1` Refresh escape hatch,
 * the `stale` flag and the quick re-poll that follows it, the hour-long gcTime —
 * exists because those three lists are upstream fan-outs (GitHub, the ticket
 * providers) that the server serves stale-while-revalidate. /api/test-plans is a
 * local JSON file: there is no upstream to be stale against, nothing for a
 * "fresh" sweep to do differently, and a re-read costs nothing. Bolting it onto
 * PANELS would have meant a Refresh button that means less than it says and a
 * `stale` field the server never sets.
 * ------------------------------------------------------------------------ */

/** Fast enough that a plan generated in the background, or a run finishing,
 * appears while you are still looking at the dialog; slow enough that the badge
 * on the top bar costs nothing to keep honest all day. */
const TEST_PLANS_POLL_MS = 10_000;

let testPlansBridged = false;

/** Refresh the plans the moment the server says the set changed, rather than up
 * to a poll later.
 *
 * Both interesting transitions are announced on the bus and are ones a user is
 * plausibly staring at when they happen: `session.test_plan_ready` (the headless
 * generation finished, so a plan that read "generating…" now has steps) and
 * `session.test_plan_due` (the due loop found the commit on the live branch —
 * the event the whole feature exists to deliver). Modelled on
 * bridgeUsageEvents() above, including its replay guard: a reconnect replays
 * history, and replaying a hundred old envelopes must not fire a hundred
 * invalidations. Installed once per page — the invalidation is global, so a
 * second subscriber would only duplicate the request. */
function bridgeTestPlanEvents() {
  const ev = window.mindflock?.events;
  if (testPlansBridged || !ev) return; // events.js may not have loaded yet
  testPlansBridged = true;
  const bump = (env: EventEnvelope) => {
    if (typeof ev.isReplay === "function" && ev.isReplay(env)) return;
    refreshTestPlans();
  };
  ev.subscribe("session.test_plan_ready", bump);
  ev.subscribe("session.test_plan_due", bump);

  /** ...and SAY so, for the two endings that were silent.
   *
   * Both are things the user asked for and then walked away from: a rewrite
   * promises "up to three minutes", a run takes minutes of an agent. The row
   * changes either way, but only if you happen to be looking at it — and the
   * whole premise of this feature is that the moment it matters is the moment
   * nobody is. Toasts rather than pushes: a push is for something that happened
   * while the app was closed, and both of these happened while it was open. */
  const say = (env: EventEnvelope, text: (d: Record<string, unknown>) => string) => {
    if (typeof ev.isReplay === "function" && ev.isReplay(env)) return;
    refreshTestPlans();
    const data = (env?.data || {}) as Record<string, unknown>;
    toast(text(data));
  };
  ev.subscribe("session.test_plan_ready", (env) =>
    say(env, (d) => {
      const n = Number(d.steps) || 0;
      return (
        (d.refreshed ? "Rewrote the checklist for " : "New checklist for ") +
        String(d.plan || "a session") +
        " — " +
        n +
        (n === 1 ? " step" : " steps")
      );
    })
  );
  ev.subscribe("session.test_plan_failed", (env) =>
    say(
      env,
      (d) =>
        "Couldn't write the checklist for " +
        String(d.plan || "a session") +
        ": " +
        String(d.error || "no reason recorded")
    )
  );
  /** THE FAILURE THAT ARRIVES AFTER THE PROMISE. `POST /run` answers 202 as
   * soon as the session is registered and the dialog says an agent is starting;
   * the worktree, the branch and tmux all happen afterwards on a background
   * task, and when THAT fails the only thing the app had ever said about this
   * run was the promise. `fail_run` does land the reason on the plan, but the
   * user sees it on the next 10s poll and only if they are still looking at the
   * dialog. This retracts it where they are, and it stays until dismissed —
   * because the sentence is a paragraph of git or tmux output whose remedy is
   * its last clause. */
  ev.subscribe("session.create_failed", (env) => {
    if (typeof ev.isReplay === "function" && ev.isReplay(env)) return;
    const session = String(env?.session || "");
    // BOTH of this feature's sessions. `isVerifySession` matches `verify-` only,
    // and "Fix what failed" opens a `fix-<plan>` one from the same dialog with
    // the same optimistic toast — whose failure (a leftover worktree, a repo
    // that moved) is exactly the kind that arrives minutes later on the
    // background start. Anything else is an ordinary session and not ours.
    if (!isVerifySession(session) && !session.startsWith("fix-")) return;
    refreshTestPlans();
    const data = (env?.data || {}) as Record<string, unknown>;
    errorPop(
      isVerifySession(session)
        ? "The verify session couldn't start"
        : "The session to fix it couldn't start",
      String(data.error || "no reason recorded"),
    );
  });
  ev.subscribe("session.test_plan_gave_up", (env) =>
    say(
      env,
      (d) =>
        "Gave up on the verify run for " +
        String(d.title || d.plan || "a checklist") +
        " — " +
        String(d.run_session || "the session") +
        " never wrote its answers. Run it again.",
    ),
  );
  ev.subscribe("session.test_plan_checked", (env) =>
    say(env, (d) => {
      const failed = Number(d.failed) || 0;
      const mine = Number(d.needs_you) || 0;
      const bits = [
        failed ? failed + (failed === 1 ? " step failed" : " steps failed") : "",
        mine ? mine + (mine === 1 ? " step needs you" : " steps need you") : "",
      ].filter(Boolean);
      return (
        String(d.title || d.plan || "A checklist") +
        " checked" +
        (bits.length ? " — " + bits.join(", ") : " — everything passed")
      );
    })
  );
}

/** The Verify dialog's list, and the top bar's due count.
 *
 * `enabled` is what keeps the poll scoped: a disabled query never schedules its
 * interval, so passing `false` (a closed dialog) leaves only whichever other
 * caller is still mounted driving the cadence. The top bar keeps it on, because
 * a badge that only updates while its own dialog is open is a badge that never
 * tells you anything you didn't already know. `placeholderData` holds the last
 * list on screen across a reopen, and `retry: false` matches the other local
 * endpoints — a server too old to know this route will 404 every time, and
 * asking twice per poll just doubles the noise. */
export function useTestPlans(enabled = true) {
  useEffect(bridgeTestPlanEvents, []);
  return useQuery({
    queryKey: ["test-plans"],
    queryFn: () => api<TestPlansResponse>("/api/test-plans"),
    enabled,
    refetchInterval: TEST_PLANS_POLL_MS,
    placeholderData: (prev) => prev,
    retry: false,
  });
}

/** Imperative refresh after anything that changes a plan (start a run, record a
 * step result, regenerate, delete) — the routes answer with the state the server
 * just wrote, but the list is shared with the top bar's badge. */
export function refreshTestPlans() {
  return queryClient.invalidateQueries({ queryKey: ["test-plans"] });
}
