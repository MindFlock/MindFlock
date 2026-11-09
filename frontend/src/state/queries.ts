/** Server state via TanStack Query. The instances poll is the SPA's heartbeat
 * (4s visible / 30s hidden — the server only computes *_changed events while
 * something polls, so the cadence is a feature, not laziness). Each poll also
 * feeds window.mindflock.__setSessions so addons see the same snapshot. */

import { noteServerHome } from "../lib/serverPaths";
import { useEffect } from "react";
import { normalizeFlightRows } from "../lib/flights";
import { QueryClient, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { splitLockedRows } from "../lib/guest";
import { nudgeUsage } from "../lib/usageRefresh";
import { toast } from "../lib/toast";
import { errorPop } from "../lib/errorPop";
import { isVerifySession } from "../components/dialogs/verifySession";
import { windowName } from "../lib/windowName";
import { rememberEnvironments, type VerifyEnvsResponse } from "../lib/verifyStop";
import type { EffortCap } from "../lib/effort";
import type { MemoryDetail, MemoryHistory } from "../components/usage/memoryModel";
import type { PeerStatus } from "../lib/peer";
import type { EpicsResponse } from "../components/intake/epics";
import type { Flight, FlightPlan, StepKindDef } from "../lib/flights";
import type { ExtensionInfo, ExtensionSpec } from "../extensions/types";
import type {
  AuthProfilesResponse,
  Config,
  Json,
  DevicesResponse,
  DoctorPayload,
  Instance,
  MemoryReading,
  SlotsReading,
  MindflockStatus,
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
  useEffect(bridgeSessionSetEvents, []);
  return useQuery({
    queryKey: ["instances"],
    queryFn: async () => {
      // A guest link's crossed-out rows go to lib/guest.ts for the rail to
      // draw; nothing else (the grid, the pickers, the bell) ever sees them.
      const fetched = normalizeFlightRows(splitLockedRows(await api<Instance[]>("/api/instances")));
      window.mindflock?.__setSessions?.(fetched);
      // A session being moved keeps its "moving…" mark until the move's
      // event (`withMoving`).
      const list = withMoving(fetched, Date.now());
      // A session being made is listed only once its snapshot catches up: its
      // optimistic row waits in the cache until then (`mergePending`).
      return mergePending(list, queryClient.getQueryData<Instance[]>(["instances"]), Date.now());
    },
    refetchInterval: pollInterval,
    refetchIntervalInBackground: true,
    placeholderData: (prev) => prev,
  });
}

export function useConfig() {
  return useQuery({
    queryKey: ["config"],
    queryFn: () =>
      api<Config>("/api/config").then((c) => {
        noteServerHome((c as { home?: string } | undefined)?.home);
        return c;
      }),
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

/** The enabled-or-not extensions in a /api/addons answer — useExtensions'
 * view, and the voice assistant's (read from the cache, no hook). */
export function extensionsOf(d: AddonsResponse | undefined): ExtensionInfo[] {
  return (d?.addons || [])
    .filter((a) => a && a.id && a.extension)
    .map((a) => ({
      id: String(a.id),
      label: a.label || String(a.id),
      // Absent on a pre-v3 server: an addon you can't disable is enabled.
      enabled: a.enabled !== false,
      origin: a.origin === "user" ? ("user" as const) : ("builtin" as const),
      extension: a.extension as ExtensionSpec,
    }));
}

/** useExtensions' cache, read now (empty before it loaded). */
export function extensionsNow(): ExtensionInfo[] {
  return extensionsOf(queryClient.getQueryData<AddonsResponse>(["addons"]));
}

export function useExtensions() {
  return useQuery({
    ...extensionsQuery(),
    select: (d: AddonsResponse): ExtensionInfo[] => extensionsOf(d),
  });
}

/** Re-read the manifests after an enable/disable toggle. */
export function refreshExtensions() {
  return queryClient.invalidateQueries({ queryKey: ["addons"] });
}

/** The Assistant's live agent state — the pill on its window.
 *
 * A plain poll, on the SPA's own visible/hidden cadence, and no `enabled`
 * flag: the only callers are the Assistant window's chip, so the query has
 * observers exactly while that window is open and stops the moment it closes.
 * The state is not on the session event bus — the bus speaks for sessions the
 * engine owns, and the Assistant is a window, not one of them.
 */
export function useAssistantActivity() {
  return useQuery({
    queryKey: ["assistant-state"],
    queryFn: () => api<{ activity: string }>("/api/assistant/state"),
    refetchInterval: pollInterval,
    placeholderData: (prev) => prev,
    retry: false,
  });
}

/** GET /api/doctor — one cached payload for every reader (the sidebar's ⚠
 * chip, the top bar's version, the state notice, the doctor list). The server
 * caches the probe itself; `reprobeDoctor()` forces a fresh one (?refresh=1)
 * and writes it here, so a re-probe from one screen updates them all.
 * `refetchInterval` lets a reader keep it warm (the 5-min background probe);
 * the interval pauses while the page is hidden. */
export function useDoctor(opts: { enabled?: boolean; refetchInterval?: number | false } = {}) {
  return useQuery({
    queryKey: ["doctor"],
    queryFn: () => api<DoctorPayload>("/api/doctor"),
    enabled: opts.enabled ?? true,
    refetchInterval: opts.refetchInterval ?? false,
    refetchIntervalInBackground: false,
  });
}

/** Fresh doctor probe (GET /api/doctor?refresh=1), shared with useDoctor's
 * readers. Rejects like api() does; the cache keeps its last good payload. */
export async function reprobeDoctor(refresh = true): Promise<DoctorPayload> {
  const d = await api<DoctorPayload>("/api/doctor" + (refresh ? "?refresh=1" : ""));
  queryClient.setQueryData(["doctor"], d);
  return d;
}

/** GET /api/mindflock/status — ticket ingestion + the PR / issue automation.
 * ONE definition on purpose: the sidebar's two bars and Intake's tabs share
 * this key, and a shared key with two different intervals lets whichever
 * component mounted first decide the poll rate. 4s matches the sessions poll —
 * the green window can be as short as one provisioning. */
export function useMindflockStatus() {
  return useQuery({
    queryKey: ["mindflock-status"],
    queryFn: () => api<MindflockStatus>("/api/mindflock/status"),
    refetchInterval: 4_000,
    retry: false,
  });
}

/** The machine's memory headroom (`GET /api/system/memory`). ONE key for
 * the memory notice and the rail's Restart-all strip; 15s is plenty for a
 * level the server itself only steps with hysteresis. */
export function useMemory() {
  return useQuery({
    queryKey: ["system-memory"],
    queryFn: () => api<MemoryReading>("/api/system/memory"),
    // Faster while the launch gate holds starts back, so the rail's
    // "new agents wait" line clears soon after the queue drains.
    refetchInterval: (q) => (q.state.data?.gate?.holding ? 5_000 : 15_000),
    retry: false,
  });
}

/** Per-session memory (`GET /api/memory`): the machine, every session's
 * process tree and the top processes outside MindFlock — the sidebar's
 * Memory row and its popover. Polled every 15 s (the sampler's own tick)
 * only while something showing it is visible (`enabled`). An older server
 * 404s: no retry, and the row falls back to useMemory()'s numbers. */
export function useMemoryDetail(enabled: boolean) {
  return useQuery({
    queryKey: ["memory-detail"],
    queryFn: () => api<MemoryDetail>("/api/memory"),
    refetchInterval: 15_000,
    refetchIntervalInBackground: false,
    retry: false,
    enabled,
  });
}

/** The last hour of `GET /api/memory/history` — the Memory popover's
 * sparkline. Fetched only while the popover is open; a line lands every
 * 30 s, so a minute's poll is plenty. */
export function useMemoryHistory(enabled: boolean) {
  return useQuery({
    queryKey: ["memory-history", 60],
    queryFn: () => api<MemoryHistory>("/api/memory/history?minutes=60"),
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
    retry: false,
    enabled,
  });
}

/** Heavy-job slots (`GET /api/system/slots`). Polled only while something
 * shows them (the memory notice), so an idle app never reads the lock dir. */
export function useSlots(enabled: boolean) {
  return useQuery({
    queryKey: ["system-slots"],
    queryFn: () => api<SlotsReading>("/api/system/slots"),
    refetchInterval: 15_000,
    retry: false,
    enabled,
  });
}

/** Pull fresh pipeline status now (after a start/stop/toggle). */
export function refreshMindflockStatus() {
  return queryClient.invalidateQueries({ queryKey: ["mindflock-status"] });
}

/** Peer links (GET /api/peer) for the rail's "Shared with you" rows: each
 * link's `their_share` says whether the person on the other end has a
 * session to watch. 10s matches how often the server re-asks them. The
 * People screen keeps its own fetch (it reloads on peer.* events). */
export function usePeerLinks() {
  return useQuery({
    queryKey: ["peer-links"],
    queryFn: () => api<PeerStatus>("/api/peer"),
    refetchInterval: 10_000,
    retry: false,
  });
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

/* --------------------------------------------------------------------------
 * New boxes at once (docs/flights.md "As built: round 11 line").
 *
 * GET /api/instances serves the server's tick snapshot, so a session made a
 * moment ago is missing from it for up to a tick or two. An optimistic row
 * (`pending_create`, from `addPendingSession` or a `session.created` event)
 * stands in for it — in the grid, the rail and the Sky alike — until the
 * snapshot lists it, under its own title or one the server numbered from it,
 * or `PENDING_TTL_MS` passes, or `session.create_failed` names it.
 * ------------------------------------------------------------------------ */

/** How long an optimistic row waits for the server to list its session. */
export const PENDING_TTL_MS = 20_000;

/** An optimistic row's client-only fields. `pending_base` is the title it was
 * numbered from (`foo` for a guess `foo-2`) while the server's own pick is
 * unknown; it goes once the real title is. */
export interface PendingFields {
  pending_create?: boolean;
  pending_at?: number;
  pending_base?: string;
}

const pend = (r: Instance) => r as Instance & PendingFields;
export const isPendingRow = (r: Instance): boolean => pend(r).pending_create === true;

/** `t` is `base` or one of the server's numberings of it (`base-2`, …). */
export function numberedFrom(t: string, base: string): boolean {
  return t === base || (t.startsWith(base + "-") && /^\d+$/.test(t.slice(base.length + 1)));
}

/** The optimistic rows the cache holds that `fetched` does not list yet, kept
 * after the fetched rows (where `addPendingSession` put them). A row goes once
 * the fetch lists its title, or — while its real title is unknown — a title
 * new in this fetch numbered from its base (each new title claims one row); or
 * once it is older than `ttl`. Nothing pending: `fetched`, as is. */
export function mergePending(fetched: Instance[], cached: readonly Instance[] | undefined, now: number, ttl = PENDING_TTL_MS): Instance[] {
  const waiting = (cached || []).filter(isPendingRow);
  if (!waiting.length) return fetched;
  const listed = new Set(fetched.map((r) => r.title));
  const known = new Set((cached || []).filter((r) => !isPendingRow(r)).map((r) => r.title));
  const fresh = fetched.map((r) => r.title).filter((t) => !known.has(t) && !waiting.some((w) => w.title === t));
  const claimed = new Set<string>();
  const keep = waiting.filter((r) => {
    if (listed.has(r.title)) return false;
    const at = pend(r).pending_at;
    if (typeof at !== "number" || now - at > ttl) return false;
    const base = pend(r).pending_base;
    const hit = base ? fresh.find((t) => !claimed.has(t) && numberedFrom(t, base)) : undefined;
    if (hit) {
      claimed.add(hit);
      return false;
    }
    return true;
  });
  return keep.length ? [...fetched, ...keep] : fetched;
}

/** The server named a session it made (`session.created`). Already a row:
 * unchanged (a guess that was right only forgets its base). An optimistic row still waiting for its real title, numbered
 * from the same base: that row takes the title (one box, not two). Else a new
 * optimistic row, so a session made anywhere — a flight, an agent, another
 * browser — shows at once. */
export function notePendingCreated(rows: readonly Instance[], title: string, data: Record<string, unknown>, now: number): Instance[] {
  if (!title) return rows as Instance[];
  const same = rows.findIndex((r) => r.title === title);
  if (same >= 0) {
    // The guess was right: its title is known now, so its base is spent.
    if (!isPendingRow(rows[same]) || !pend(rows[same]).pending_base) return rows as Instance[];
    const next = rows.slice();
    next[same] = { ...rows[same], pending_base: undefined } as Instance;
    return next;
  }
  const i = rows.findIndex((r) => isPendingRow(r) && !!pend(r).pending_base && numberedFrom(title, pend(r).pending_base!));
  if (i >= 0) {
    const next = rows.slice();
    next[i] = { ...rows[i], title, pending_base: undefined, pending_at: now } as Instance;
    return next;
  }
  const row: Record<string, unknown> = { title, status: "loading", pending_create: true, pending_at: now };
  if (typeof data.parent === "string" && data.parent) row.parent = data.parent;
  if (typeof data.program === "string" && data.program) row.program = data.program;
  return [...rows, row as unknown as Instance];
}

/** A create answered with its row: the optimistic row for `guess` (or the one
 * an event already moved to the real title) becomes that row — still waiting
 * for the snapshot, so a poll that misses it keeps it. When the snapshot
 * already lists the real title, the guess just goes. */
export function landPendingRow(rows: readonly Instance[], guess: string, real: Instance, now: number): Instance[] {
  const t = real.title;
  if (!t) return rows as Instance[];
  const listed = rows.some((r) => r.title === t && !isPendingRow(r));
  const out: Instance[] = [];
  let placed = listed;
  for (const r of rows) {
    if (!isPendingRow(r) || (r.title !== guess && r.title !== t)) {
      out.push(r);
      continue;
    }
    if (placed) continue;
    out.push({ ...r, ...real, pending_create: true, pending_at: now, pending_base: undefined } as Instance);
    placed = true;
  }
  return placed ? out : [...out, { ...real, pending_create: true, pending_at: now } as Instance];
}

/** The optimistic row for `title` goes (`session.create_failed`). */
export function dropPendingRow(rows: readonly Instance[], title: string): Instance[] {
  return rows.some((r) => isPendingRow(r) && r.title === title) ? rows.filter((r) => !(isPendingRow(r) && r.title === title)) : (rows as Instance[]);
}

/* --------------------------------------------------------------------------
 * Moving a session (docs/flights.md "Moving a session").
 *
 * `POST /api/instances/{title}/move` answers 202 and the move runs on; its row
 * says "moving…" (`moving: true`) from the 202 until `session.moved` or
 * `session.move_failed` names it, or `MOVE_TTL_MS` passes. The mark lives
 * here, not only in the cache, so a poll in between keeps it.
 * ------------------------------------------------------------------------ */

/** How long a row says "moving…" with no word from the server. */
export const MOVE_TTL_MS = 60_000;

/** A row's client-only move field. */
export interface MovingFields {
  moving?: boolean;
}

export const isMovingRow = (r: Instance): boolean => (r as Instance & MovingFields).moving === true;

/** title → when its move was asked for. */
const movingSince = new Map<string, number>();

/** `rows` with `moving: true` on every row whose move is still pending (and
 * the expired marks forgotten). Nothing pending: `rows`, as is. */
export function withMoving(rows: Instance[], now: number, since: Map<string, number> = movingSince, ttl = MOVE_TTL_MS): Instance[] {
  for (const [t, at] of since) if (now - at > ttl) since.delete(t);
  if (!since.size) return rows;
  return rows.map((r) => (since.has(r.title) && !isMovingRow(r) ? ({ ...r, moving: true } as Instance) : r));
}

/** The move was accepted (202): the row says "moving…" until the event, or
 * for `MOVE_TTL_MS` at most. */
export function markMoving(title: string, now = Date.now()) {
  movingSince.set(title, now);
  queryClient.setQueryData<Instance[]>(["instances"], (rows) => (rows ? withMoving(rows, now) : rows));
  setTimeout(() => {
    // Only this mark: a second move asked meanwhile keeps its own minute.
    if (movingSince.get(title) === now) clearMoving(title);
  }, MOVE_TTL_MS);
}

/** The move landed, failed, or timed out: the row stops saying "moving…". */
export function clearMoving(title: string) {
  movingSince.delete(title);
  queryClient.setQueryData<Instance[]>(["instances"], (rows) =>
    rows && rows.some((r) => r.title === title && isMovingRow(r))
      ? rows.map((r) => (r.title === title && isMovingRow(r) ? ({ ...r, moving: undefined } as Instance) : r))
      : rows
  );
}

/** "Moved “name” to /new/folder" — the toast `session.moved` raises. */
export function movedToastText(name: string, to: string): string {
  return to ? `Moved “${name}” to ${to}` : `Moved “${name}”`;
}

/** The session a move event names: the envelope's, else the payload's. */
function movedTitle(env: EventEnvelope): string {
  return String(env?.session || (env?.data as Record<string, unknown> | undefined)?.title || "");
}

/** `session.moved`: the row stops saying "moving…", the rows refetch (its
 * folder changed), and — live, not a replay — a toast says where it went. */
export function onSessionMoved(env: EventEnvelope, replay = false) {
  const title = movedTitle(env);
  if (!title) return;
  clearMoving(title);
  void refreshInstances();
  if (!replay) toast(movedToastText(windowName(title), String(env.data?.to || "")), { duration: 5000 });
}

/** `session.move_failed`: the row stops saying "moving…" and the reason goes
 * on an error card (it stays: the remedy is often the sentence's end). */
export function onSessionMoveFailed(env: EventEnvelope, replay = false) {
  const title = movedTitle(env);
  if (!title) return;
  clearMoving(title);
  void refreshInstances();
  if (!replay) errorPop(`Moving “${windowName(title)}” failed`, String(env.data?.error || "no reason recorded"));
}

let instancesTimer: ReturnType<typeof setTimeout> | null = null;
/** One refetch for a burst of session-set events. */
function refreshInstancesSoon() {
  if (instancesTimer) clearTimeout(instancesTimer);
  instancesTimer = setTimeout(() => {
    instancesTimer = null;
    void refreshInstances();
  }, 150);
}

let sessionSetBridged = false;
/** The events that change the session set (backend/web/core/events.py): made,
 * removed, renamed, moved, or a make or move that failed after its 202.
 * Installed once per page; replays (history after a reconnect) only refetch. */
export function bridgeSessionSetEvents() {
  const ev = window.mindflock?.events;
  if (sessionSetBridged || !ev) return; // events.js may not have loaded yet
  sessionSetBridged = true;
  const replay = (env: EventEnvelope) => typeof ev.isReplay === "function" && ev.isReplay(env);
  ev.subscribe("session.created", (env) => {
    const title = String(env?.session || "");
    // A verify run is a hidden helper: the event can't say so, the row will.
    if (title && !replay(env) && !isVerifySession(title))
      queryClient.setQueryData<Instance[]>(["instances"], (rows) => (rows ? notePendingCreated(rows, title, env.data || {}, Date.now()) : rows));
    refreshInstancesSoon();
  });
  ev.subscribe("session.create_failed", (env) => {
    const title = String(env?.session || "");
    if (title) queryClient.setQueryData<Instance[]>(["instances"], (rows) => (rows ? dropPendingRow(rows, title) : rows));
    refreshInstancesSoon();
  });
  ev.subscribe("session.moved", (env) => onSessionMoved(env, replay(env)));
  ev.subscribe("session.move_failed", (env) => onSessionMoveFailed(env, replay(env)));
  for (const name of ["session.deleted", "session.renamed"]) ev.subscribe(name, refreshInstancesSoon);
}

/** After anything that changes the recently-closed store: a reopen, a forget, a
 * delete, or the unused-worktree sweep (which forgets the entries whose
 * directory it removed). The Verify dialog caches this list to offer closed
 * sessions as checklist targets, so without the invalidate it keeps offering
 * work whose worktree and store entry are both gone. */
export function refreshRecentlyClosed() {
  return queryClient.invalidateQueries({ queryKey: ["recently-closed"] });
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
export function usePanelQuery<T extends { stale?: boolean }>(key: PanelKey, opts: { enabled?: boolean } = {}) {
  const q = useQuery<T>({
    queryKey: [key],
    queryFn: () => api<T>(PANELS[key]),
    // false = read the cache only (the top bar's Flights badge counts what the
    // lists already hold; it never fans out upstream itself).
    enabled: opts.enabled ?? true,
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

/** Re-sweep one panel now (`?fresh=1`), outside the component that shows it —
 * the add flow uses it after a repository joins a list, so the tab's new
 * heading fills with that repository's rows instead of waiting out the cache. */
export function refreshPanel(key: PanelKey) {
  return queryClient
    .fetchQuery({
      queryKey: [key],
      queryFn: () => api(PANELS[key] + "?fresh=1"),
      staleTime: 0,
      retry: false,
    })
    .catch(() => undefined);
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
 * Intake → Epics: every not-done epic of yours on each Shortcut source.
 *
 * Not a member of PANELS: the server caches it for 60 s on its own clock and
 * its Refresh is `?refresh=1`, not the panels' `?fresh=1`, and it has no
 * `stale` flag to re-poll on. One query key, read by both the tab and its
 * badge on the strip, so the two can never disagree.
 * ------------------------------------------------------------------------ */

/** Matches the server's cache: polling faster would only be told the same. */
const EPICS_POLL_MS = 60_000;

/** The Epics list. Polled every 60 s while the page is visible (TanStack
 * pauses `refetchInterval` in a hidden tab) and only while something that
 * shows it is mounted — the open Intake dialog's tab strip. `refresh()` is the
 * ↻ button: `?refresh=1` asks Shortcut again instead of the server's cache. */
export function useEpicsQuery() {
  const q = useQuery<EpicsResponse>({
    queryKey: ["epics"],
    queryFn: () => api<EpicsResponse>("/api/epics"),
    staleTime: 30_000,
    gcTime: PANEL_GC_MS,
    placeholderData: (prev) => prev,
    retry: false,
    refetchInterval: EPICS_POLL_MS,
  });
  const refresh = () =>
    queryClient
      .fetchQuery({
        queryKey: ["epics"],
        queryFn: () => api<EpicsResponse>("/api/epics?refresh=1"),
        staleTime: 0,
        retry: false,
      })
      .catch(() => undefined);
  return { ...q, refresh };
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
        (d.refreshed ? "Rewrote the behaviors for " : "New behaviors for ") +
        String(d.plan || "a session") +
        " — " +
        n +
        (n === 1 ? " behavior" : " behaviors")
      );
    })
  );
  // "Your turn": the work shipped and its checklist came due. Said where the
  // user is, not only as a number on the Verify badge.
  ev.subscribe("session.test_plan_due", (env) =>
    say(
      env,
      (d) =>
        String(d.title || d.plan || "A change") +
        " shipped to " +
        String(d.live_branch || "the live branch") +
        " — its behaviors are ready on its route in the Sky",
    ),
  );
  ev.subscribe("session.test_plan_failed", (env) =>
    say(
      env,
      (d) =>
        "Couldn't write the behaviors for " +
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
        String(d.title || d.plan || "these behaviors") +
        " — " +
        String(d.run_session || "the session") +
        " never wrote its answers. Press Check now to try again.",
    ),
  );
  ev.subscribe("session.test_plan_checked", (env) => say(env, checkedToastText));
}

/** The toast for `session.test_plan_checked`. A check run before the merge
 * (`before_merge: true`) ran on the branch, not on what shipped — it says so. */
export function checkedToastText(d: Record<string, unknown>): string {
  const failed = Number(d.failed) || 0;
  const mine = Number(d.needs_you) || 0;
  const bits = [
    failed ? failed + (failed === 1 ? " behavior failed" : " behaviors failed") : "",
    mine ? mine + (mine === 1 ? " behavior needs you" : " behaviors need you") : "",
  ].filter(Boolean);
  return (
    String(d.title || d.plan || "The behaviors") +
    (d.before_merge === true ? " checked on the branch" : " checked") +
    (bits.length ? " — " + bits.join(", ") : " — every behavior verified")
  );
}

/** Whether GitHub is connected here (`GET /api/github/status`), for forms that
 * need a token — Verify's repository list warns before you add one, rather
 * than after Test access. Read rarely: the answer changes when somebody signs
 * in, and the Connect dialog refreshes it then. */
export function useGithubConnected(enabled = true) {
  return useQuery({
    ...githubStatusQuery(enabled),
    select: (r: GithubStatusLite) => !!r.connected,
  });
}

/** The part of `GET /api/github/status` a status line needs: whether a token
 * resolves, and whose it is (`login`; "" when GitHub couldn't be asked —
 * `user_error` then says why). Never the token. */
export interface GithubStatusLite {
  connected?: boolean;
  login?: string;
  source?: string;
  user_error?: string;
}

function githubStatusQuery(enabled: boolean) {
  return {
    queryKey: ["github-status"],
    queryFn: async () => (await api<GithubStatusLite>("/api/github/status")) || {},
    enabled,
    staleTime: 60_000,
    retry: false,
  };
}

/** Whether GitHub is connected AND as whom — the "GitHub: connected as @you"
 * line Intake's GitHub tabs and the add flow show. Shares `useGithubConnected`'s
 * cache entry, so the two can never disagree. */
export function useGithubStatus(enabled = true) {
  return useQuery(githubStatusQuery(enabled));
}

/** Re-ask after the Connect GitHub dialog closes: a sign-in there is the one
 * thing that changes the answer. */
export function refreshGithubStatus() {
  return queryClient.invalidateQueries({ queryKey: ["github-status"] });
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

/** The flock-wide "branch that ships" as the server last resolved it (the
 * Verify list's `live_branch`), read from the cache the top bar keeps warm —
 * never a request of its own. "" before anything has loaded. */
export function useCachedLiveBranch(): string {
  const q = useQuery({
    queryKey: ["test-plans"],
    queryFn: () => api<TestPlansResponse>("/api/test-plans"),
    enabled: false,
  });
  return q.data?.live_branch || "";
}

/** Every watched repo's Verify ENVIRONMENTS (GET /api/test-plans/environments):
 * the Verify stop's "Where" picker lists their names. Bound to `queryClient`
 * itself (no provider needed: the stop form also renders in a bare SSR test).
 * `isError` / null data (an older server: 404) → the picker falls back to a
 * text field. Each answer is remembered for the stop's face ("once on
 * release", lib/verifyStop.ts). */
export function useVerifyEnvironments(enabled = true) {
  return useQuery(
    {
      queryKey: ["test-plan-environments"],
      queryFn: async () => {
        const r = await api<VerifyEnvsResponse>("/api/test-plans/environments");
        const ok = r && typeof r === "object" && Array.isArray(r.names) ? r : null;
        rememberEnvironments(ok);
        if (!ok) throw new Error("no environments list");
        return ok;
      },
      enabled,
      staleTime: 60_000,
      retry: false,
    },
    queryClient
  );
}

/** Imperative refresh after anything that changes a plan (start a run, record a
 * step result, regenerate, delete) — the routes answer with the state the server
 * just wrote, but the list is shared with the top bar's badge. */
export function refreshTestPlans() {
  return queryClient.invalidateQueries({ queryKey: ["test-plans"] });
}

/* --------------------------------------------------------------------------
 * Flights (docs/flights.md): the flights in the air, the plans and the step
 * registry. A server that predates the routes answers 404, read as "none"
 * (null) and asked rarely. `flight.changed | needs_you | finished` refetch,
 * the pattern of the run events (state/runs.ts).
 * ------------------------------------------------------------------------ */

/** While a flight is live: the driver's own 5 s cadence. */
export const FLIGHTS_LIVE_MS = 5_000;
/** Nothing live (or the tab hidden): enough to notice a new one. */
export const FLIGHTS_IDLE_MS = 30_000;
const FLIGHTS_MISSING_MS = 120_000;

const routeMissing = (e: unknown) =>
  !!e && typeof e === "object" && "status" in e && ((e as { status: number }).status === 404 || (e as { status: number }).status === 405);

async function orNull<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p;
  } catch (e) {
    if (routeMissing(e)) return null;
    throw e;
  }
}

let flightsBridged = false;
function bridgeFlightEvents() {
  const ev = window.mindflock?.events;
  if (flightsBridged || !ev) return; // events.js may not have loaded yet
  flightsBridged = true;
  const bump = () => {
    void queryClient.invalidateQueries({ queryKey: ["flights"] });
    void queryClient.invalidateQueries({ queryKey: ["instances"] });
  };
  for (const name of ["flight.changed", "flight.needs_you", "flight.finished"]) ev.subscribe(name, bump);
}

/** The flights (unfinished + the last 20 finished). `data` null = this server
 * has no flights route. */
export function useFlights(enabled = true) {
  useEffect(bridgeFlightEvents, []);
  return useQuery({
    queryKey: ["flights"],
    queryFn: () => orNull(api<{ flights?: Flight[] }>("/api/flights").then((d) => d?.flights || [])),
    enabled,
    refetchInterval: (q) =>
      q.state.data === null
        ? FLIGHTS_MISSING_MS
        : (q.state.data || []).some((f) => f.state === "running" || f.state === "waiting_you") && !document.hidden
          ? FLIGHTS_LIVE_MS
          : FLIGHTS_IDLE_MS,
    refetchIntervalInBackground: true,
    placeholderData: (prev) => prev,
    retry: false,
  });
}

/** The plans, built-ins first. null = no flights route. */
export function useFlightPlans(enabled = true) {
  useEffect(bridgeFlightEvents, []);
  return useQuery({
    queryKey: ["flight-plans"],
    queryFn: () => orNull(api<{ plans?: FlightPlan[] }>("/api/flights/plans").then((d) => d?.plans || [])),
    enabled,
    staleTime: 30_000,
    placeholderData: (prev) => prev,
    retry: false,
  });
}

/** The step registry (`STEP_KINDS`): the palette's blocks and their forms.
 * Fixed for the life of a server, so read once. */
export function useFlightKinds(enabled = true) {
  return useQuery({
    queryKey: ["flight-kinds"],
    queryFn: () => orNull(api<{ kinds?: StepKindDef[] }>("/api/flights/kinds").then((d) => d?.kinds || [])),
    enabled,
    staleTime: 10 * 60_000,
    retry: false,
  });
}
