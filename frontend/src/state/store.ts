/** Client-side UI state (Zustand). Server state lives in TanStack Query
 * (state/queries.ts) — this store only holds what the browser owns: focus,
 * layout, filters, selections, and which overlays are open.
 *
 * localStorage keys are UNCHANGED from the vanilla app (cs_* / mf_*) so an
 * upgrade keeps every user's layout, order, aliases, and bindings. */

import { create } from "zustand";
import { api } from "../api/client";
import {
  BREAK_DEFAULT_MINUTES,
  clampBreakMinutes,
  clampIdleMinutes,
  IDLE_DEFAULT_MINUTES,
} from "../lib/breakTimer";
import { defaultHiddenBars } from "../components/sidebar/barDefs";

export type ViewMode = "auto" | "2" | "4" | "9";

/** Sidebar width bounds (px).
 *
 * The floor is the width the sidebar shipped at for its whole life, and it is a
 * floor rather than a suggestion because narrower genuinely breaks the row: the
 * intrinsic minimum of a row (number + dot + chevron + the title's 34px legible
 * floor + a 96px stage chip + ✕) is ~240px, so below that the chip and the ✕
 * spill past the sidebar's edge and the name collapses to two characters.
 * Dragging can only make the sidebar WIDER — which is the direction anyone
 * reaches for the handle to go anyway.
 *
 * The ceiling stops a drag from squeezing the agent panes it exists beside. */
export const SIDEBAR_MIN_W = 260;
export const SIDEBAR_MAX_W = 560;
export const SIDEBAR_DEFAULT_W = 260;

export function clampSidebarWidth(px: number): number {
  if (!isFinite(px)) return SIDEBAR_DEFAULT_W;
  return Math.round(Math.min(SIDEBAR_MAX_W, Math.max(SIDEBAR_MIN_W, px)));
}

function load<T>(key: string, fallback: T, parse = true): T {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return fallback;
    return parse ? (JSON.parse(raw) as T) : (raw as unknown as T);
  } catch {
    return fallback;
  }
}

/** True when the key has never been written — a genuinely fresh user, as
 * opposed to one who explicitly cleared a setting. Lets first-run defaults
 * (starter bars, hints) differ from an empty saved value. */
function firstRun(key: string): boolean {
  try {
    return localStorage.getItem(key) === null;
  } catch {
    return false;
  }
}

function save(key: string, value: unknown, stringify = true) {
  try {
    localStorage.setItem(key, stringify ? JSON.stringify(value) : String(value));
  } catch {
    /* storage unavailable */
  }
}

/** The three fixed special windows (one instance each; verify and extension
 * windows are per-instance lists of their own). */
export type SpecialKind = "logs" | "syslogs" | "chat";

/** The grid token for a window that is not a session — a "\u0000" prefix no
 * tmux session title can carry, so window keys and session titles share one
 * namespace (the MRU, the row layout, the view cap) without ever colliding.
 *
 * `verify` and `ext` carry a per-instance ref because, unlike the other three,
 * several can be open at once: one per plan being watched, one per extension
 * pane. Lives here rather than in the grid because OPENING a window has to
 * select it (below), and the store cannot import a component. */
export function windowKey(
  kind: SpecialKind | "verify" | "ext",
  ref = ""
): string {
  return kind === "logs"
    ? "\u0000mindflock-logs"
    : kind === "syslogs"
      ? "\u0000system-logs"
      : kind === "verify"
        ? "\u0000verify:" + ref
        : kind === "ext"
          ? "\u0000ext:" + ref
          : "\u0000assistant-chat";
}

export type DialogName =
  | "new-session"
  | "settings"
  | "intake"
  | "verify"
  | "commit"
  | "make-pr"
  | "rename"
  | "device"
  // "workspaces" (the disk manager) was folded into "recent" — one page.
  | "recent"
  | "prompts"
  | "setup"
  | "todo"
  | "assistant-agent"
  | "palette"
  | "shortcuts"
  // The one dialog every extension's dialog surfaces share; the extension id +
  // surface ride in dialogTarget ("<ext>:<surface>[:<ref>]").
  | "extension";

interface UiState {
  /** Logically focused session title (keyboard target, MRU head). */
  focused: string | null;
  /** Grid view cap. */
  viewMode: ViewMode;
  /** Persisted row layout: array of arrays of titles. */
  gridRows: string[][];
  sidebarHidden: boolean;
  /** Sidebar column width in px (drag the right edge; SIDEBAR_MIN_W…MAX_W). */
  sidebarWidth: number;
  /** User drag order of sidebar rows (stable; selection never reorders).
   * Holds session titles AND window sentinels — one rail, one order. */
  order: string[];
  /** The rail's rows exactly as rendered and numbered (display order, with
   * device grouping, collapse and the filter applied) — session titles and
   * window sentinels. Published by the Sidebar after each render; the
   * keymap's Alt+N / Ctrl+Tab and the notification "[N]" prefixes read it,
   * so a number can never point at a row the badge doesn't show. Transient. */
  railOrder: string[];
  /** Most-recently-used order (selection updates it; fills fixed views). */
  mru: string[];
  /** Sidebar filter text, lowercased. */
  filter: string;
  /** Titles hidden from the grid. */
  hidden: Set<string>;
  /** Verify runs currently shown as a read-only pane, by session title.
   *
   * Its own list rather than a `hidden`/MRU entry because a verify run is not
   * a session you work in: it gets a WATCH window (read-only, closable, absent
   * from the sidebar), not a terminal you can type into. Deliberately not
   * persisted — a run you were watching yesterday is not something to reopen on
   * launch, and the Verify dialog can always reopen one that still exists. */
  verifyPanes: string[];
  /** Extension panes (grid windows an extension opened), by pane key
   * ("<ext>:<surface>[:<ref>]") with a live chrome title. Deliberately not
   * persisted, like verifyPanes: the keep-alive DOM behind them lives only in
   * this page, so a reload could only restore an empty shell — the extension
   * reopens them on demand. */
  extPanes: Array<{ key: string; title: string }>;
  /** The fixed special windows that are open (MindFlock logs / system logs /
   * assistant chat). In the store — not component state — because the sidebar
   * lists every open window with its ✕ while other components open them.
   * Not persisted, same reasoning as verifyPanes. */
  specialOpen: SpecialKind[];
  /** Bulk-selected titles (sidebar checkboxes). */
  bulkSelected: Set<string>;
  /** Display aliases: title -> custom label. */
  aliases: Record<string, string>;
  /** Collapsed device groups in the sidebar. */
  collapsedDevices: Set<string>;
  /** Sidebar bars hidden via the footer Customize menu (keys in BAR_KEYS). */
  hiddenBars: Set<string>;
  /** User drag order of the sidebar bars (keys; see barDefs.ts). Empty = default. */
  barOrder: string[];
  /** Reduce motion: while an agent is running, cover its terminal with a static
   * "running" panel instead of showing the live (flickering) output. Off by
   * default; the cover lifts on interaction and re-covers after a short idle. */
  reduceMotion: boolean;
  /** Take a break: pop a full-screen reminder every `breakEveryMin` minutes.
   * Off by default — nobody wants an app that interrupts them uninvited. */
  breakReminder: boolean;
  /** Minutes between break reminders (clamped, see lib/breakTimer). */
  breakEveryMin: number;
  /** The idle flock: birds over the grid once nobody has touched this window
   * for `idleFlockAfterMin` minutes. On by default — unlike the break card it
   * interrupts nothing, it only shows up in a room you have already left. */
  idleFlock: boolean;
  /** Minutes of no input before the idle flock appears (clamped). */
  idleFlockAfterMin: number;
  /** Master switch for the getting-started hints. New users start with them on. */
  hintsEnabled: boolean;
  /** Hint keys the user has dismissed (hidden even while hints are enabled). */
  dismissedHints: Set<string>;
  /** True once the user has finished/skipped the welcome walkthrough. */
  tourDone: boolean;
  /** The walkthrough overlay is currently open (transient, not persisted). */
  tourOpen: boolean;
  /** Per-session last active tab (agent | terminal | diff | queue). */
  lastTab: Record<string, string>;
  /** The open modal, if any (one at a time, like the vanilla app). */
  openDialog: DialogName | null;
  /** Payload for dialogs that target a session (commit/rename/device…).
   * Overloaded by the two multi-screen dialogs, which read it as the screen /
   * tab to open on (Settings screen key, Intake tab key). */
  dialogTarget: string | null;
  /** Last PR base branch chosen per repo (Make-PR dialog pre-fill). */
  prBaseByRepo: Record<string, string>;

  setFocused(title: string | null): void;
  touchMru(title: string): void;
  /** Replace the MRU wholesale (the capped-view swap-in-place demotion). */
  setOrderlessMru(mru: string[]): void;
  setViewMode(v: ViewMode): void;
  setGridRows(rows: string[][]): void;
  toggleSidebar(): void;
  /** Set the sidebar width (clamped + persisted). */
  setSidebarWidth(px: number): void;
  setOrder(order: string[]): void;
  /** Publish the rendered rail (no-op unless the rows actually changed). */
  setRailOrder(keys: string[]): void;
  moveInOrder(title: string, before: string | null): void;
  setFilter(f: string): void;
  setHidden(title: string, hidden: boolean): void;
  /** Show a verify run's read-only pane (idempotent), or close it. */
  openVerifyPane(title: string): void;
  closeVerifyPane(title: string): void;
  /** Open (idempotent by key; a same-key open just applies the new title) /
   * close / retitle an extension pane. The extension host (extensions/host.ts)
   * owns the pane BODIES; these only manage the grid slots. */
  openExtPane(key: string, title: string): void;
  closeExtPane(key: string): void;
  retitleExtPane(key: string, title: string): void;
  /** Toggle one of the fixed special windows (logs / system logs / chat). */
  toggleSpecial(kind: SpecialKind): void;
  toggleBulk(title: string): void;
  clearBulk(): void;
  setAlias(title: string, alias: string): void;
  toggleDeviceCollapsed(device: string): void;
  toggleBarHidden(key: string): void;
  setBarOrder(order: string[]): void;
  /** Toggle the reduce-motion terminal cover. */
  setReduceMotion(on: boolean): void;
  /** Turn the take-a-break reminder on/off. */
  setBreakReminder(on: boolean): void;
  /** Set the minutes between break reminders (clamped on the way in). */
  setBreakEveryMin(min: number): void;
  /** Turn the idle flock on/off. */
  setIdleFlock(on: boolean): void;
  /** Set the minutes of idleness before the flock appears (clamped). */
  setIdleFlockAfterMin(min: number): void;
  /** Turn hints on/off. Turning them back on re-arms every dismissed hint. */
  setHintsEnabled(on: boolean): void;
  /** Dismiss a single hint by key (persisted). */
  dismissHint(key: string): void;
  /** Open the welcome walkthrough (first run or a manual replay). */
  openTour(): void;
  /** Close the walkthrough and remember it's been seen. */
  finishTour(): void;
  setLastTab(title: string, tab: string): void;
  openDialogFor(name: DialogName, target?: string | null): void;
  closeDialog(): void;
  setPrBase(repo: string, base: string): void;
}

export const useUi = create<UiState>((set, get) => ({
  focused: null,
  viewMode: load<ViewMode>("cs_viewmode", "auto", false),
  gridRows: load<string[][]>("cs_gridrows", []),
  sidebarHidden: load<string>("cs_sidebar", "", false) === "hidden",
  sidebarWidth: clampSidebarWidth(load<number>("mf_sidebar_w", SIDEBAR_DEFAULT_W)),
  order: load<string[]>("cs_order", []),
  railOrder: [],
  mru: load<string[]>("cs_mru", []),
  filter: "",
  hidden: new Set(load<string[]>("mf_hidden", [])),
  verifyPanes: [],
  extPanes: [],
  specialOpen: [],
  bulkSelected: new Set<string>(),
  aliases: load<Record<string, string>>("mf_aliases", {}),
  collapsedDevices: new Set(load<string[]>("cs_devcollapse", [])),
  // Fresh users start with the essentials (Usage + Ticket Ingestion + Assistant)
  // so a first run isn't overwhelming; the rest are one click away in Customize.
  // Once the user touches Customize the saved set wins, empty included.
  hiddenBars: new Set(
    firstRun("mf_hiddenbars") ? defaultHiddenBars() : load<string[]>("mf_hiddenbars", [])
  ),
  barOrder: load<string[]>("mf_barorder", []),
  reduceMotion: load<boolean>("mf_reduce_motion", false),
  breakReminder: load<boolean>("mf_break_on", false),
  breakEveryMin: clampBreakMinutes(load<number>("mf_break_every", BREAK_DEFAULT_MINUTES)),
  idleFlock: load<boolean>("mf_idle_flock", true),
  idleFlockAfterMin: clampIdleMinutes(load<number>("mf_idle_after", IDLE_DEFAULT_MINUTES)),
  hintsEnabled: load<boolean>("mf_hints", true),
  dismissedHints: new Set(load<string[]>("mf_hints_seen", [])),
  tourDone: load<boolean>("mf_tour_done", false),
  tourOpen: false,
  lastTab: load<Record<string, string>>("cs_lasttab", {}),
  openDialog: null,
  dialogTarget: null,
  prBaseByRepo: load<Record<string, string>>("mf_prbase", {}),

  setFocused: (title) => set({ focused: title }),
  touchMru: (title) => {
    const mru = [title, ...get().mru.filter((t) => t !== title)].slice(0, 50);
    save("cs_mru", mru);
    set({ mru });
  },
  setOrderlessMru: (mru) => {
    save("cs_mru", mru);
    set({ mru });
  },
  setViewMode: (v) => {
    save("cs_viewmode", v, false);
    set({ viewMode: v });
  },
  setGridRows: (rows) => {
    save("cs_gridrows", rows);
    set({ gridRows: rows });
  },
  toggleSidebar: () => {
    const hidden = !get().sidebarHidden;
    save("cs_sidebar", hidden ? "hidden" : "", false);
    set({ sidebarHidden: hidden });
  },
  setSidebarWidth: (px) => {
    const w = clampSidebarWidth(px);
    if (w === get().sidebarWidth) return;
    save("mf_sidebar_w", w);
    set({ sidebarWidth: w });
  },
  setOrder: (order) => {
    save("cs_order", order);
    set({ order });
  },
  setRailOrder: (keys) => {
    const cur = get().railOrder;
    if (cur.length === keys.length && cur.every((k, i) => k === keys[i])) return;
    set({ railOrder: keys });
  },
  moveInOrder: (title, before) => {
    const order = get().order.filter((t) => t !== title);
    const i = before === null ? order.length : order.indexOf(before);
    order.splice(i < 0 ? order.length : i, 0, title);
    save("cs_order", order);
    set({ order });
  },
  setFilter: (filter) => set({ filter: filter.toLowerCase() }),
  setHidden: (title, hidden) => {
    const next = new Set(get().hidden);
    if (hidden) next.add(title);
    else next.delete(title);
    save("mf_hidden", [...next]);
    set({ hidden: next });
  },
  openVerifyPane: (title) => {
    if (!title) return;
    // Selected either way — an already-open pane behind a capped view has to
    // come forward, exactly as clicking its sidebar row would.
    get().touchMru(windowKey("verify", title));
    if (get().verifyPanes.includes(title)) return;
    set({ verifyPanes: [...get().verifyPanes, title] });
  },
  closeVerifyPane: (title) =>
    set({ verifyPanes: get().verifyPanes.filter((t) => t !== title) }),
  openExtPane: (key, title) => {
    if (!key) return;
    get().touchMru(windowKey("ext", key));
    const cur = get().extPanes;
    const existing = cur.find((p) => p.key === key);
    if (existing) {
      // Same-key open = reveal (the pane already holds its grid slot) +
      // retitle; the body is never touched.
      if (existing.title !== title)
        set({ extPanes: cur.map((p) => (p.key === key ? { ...p, title } : p)) });
      return;
    }
    set({ extPanes: [...cur, { key, title }] });
  },
  closeExtPane: (key) => set({ extPanes: get().extPanes.filter((p) => p.key !== key) }),
  retitleExtPane: (key, title) =>
    set({ extPanes: get().extPanes.map((p) => (p.key === key ? { ...p, title } : p)) }),
  toggleSpecial: (kind) => {
    const cur = get().specialOpen;
    // Opening one selects it: these windows compete for grid slots like any
    // session, so at "view: 1" an unselected open is an open you cannot see.
    if (!cur.includes(kind)) get().touchMru(windowKey(kind));
    set({
      specialOpen: cur.includes(kind) ? cur.filter((k) => k !== kind) : [...cur, kind],
    });
  },
  toggleBulk: (title) => {
    const next = new Set(get().bulkSelected);
    if (next.has(title)) next.delete(title);
    else next.add(title);
    set({ bulkSelected: next });
  },
  clearBulk: () => set({ bulkSelected: new Set() }),
  setAlias: (title, alias) => {
    const aliases = { ...get().aliases };
    if (alias) aliases[title] = alias;
    else delete aliases[title];
    save("mf_aliases", aliases);
    set({ aliases });
    // Mirror the rename to the server (fire-and-forget delta): the browser
    // stays the source of truth for rendering, but ntfy pushes are formatted
    // server-side and would otherwise name sessions by their raw titles.
    api("/api/aliases", { json: { title, alias: alias || "" } }).catch(() => {
      /* offline / older server: the push just keeps the raw title */
    });
  },
  toggleDeviceCollapsed: (device) => {
    const next = new Set(get().collapsedDevices);
    if (next.has(device)) next.delete(device);
    else next.add(device);
    save("cs_devcollapse", [...next]);
    set({ collapsedDevices: next });
  },
  toggleBarHidden: (key) => {
    const next = new Set(get().hiddenBars);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    save("mf_hiddenbars", [...next]);
    set({ hiddenBars: next });
  },
  setBarOrder: (order) => {
    save("mf_barorder", order);
    set({ barOrder: order });
  },
  setReduceMotion: (on) => {
    save("mf_reduce_motion", on);
    set({ reduceMotion: on });
  },
  setBreakReminder: (on) => {
    save("mf_break_on", on);
    set({ breakReminder: on });
  },
  setBreakEveryMin: (min) => {
    const every = clampBreakMinutes(min);
    save("mf_break_every", every);
    set({ breakEveryMin: every });
  },
  setIdleFlock: (on) => {
    save("mf_idle_flock", on);
    set({ idleFlock: on });
  },
  setIdleFlockAfterMin: (min) => {
    const after = clampIdleMinutes(min);
    save("mf_idle_after", after);
    set({ idleFlockAfterMin: after });
  },
  setHintsEnabled: (on) => {
    save("mf_hints", on);
    if (on) {
      // Re-arm: clear the dismissed set so the user sees every hint again.
      save("mf_hints_seen", []);
      set({ hintsEnabled: true, dismissedHints: new Set() });
    } else {
      set({ hintsEnabled: false });
    }
  },
  dismissHint: (key) => {
    const next = new Set(get().dismissedHints);
    next.add(key);
    save("mf_hints_seen", [...next]);
    set({ dismissedHints: next });
  },
  openTour: () => set({ tourOpen: true }),
  finishTour: () => {
    save("mf_tour_done", true);
    set({ tourOpen: false, tourDone: true });
  },
  setLastTab: (title, tab) => {
    const lastTab = { ...get().lastTab, [title]: tab };
    save("cs_lasttab", lastTab);
    set({ lastTab });
  },
  openDialogFor: (name, target = null) => set({ openDialog: name, dialogTarget: target }),
  closeDialog: () => set({ openDialog: null, dialogTarget: null }),
  setPrBase: (repo, base) => {
    if (!repo) return;
    const next = { ...get().prBaseByRepo };
    if (base) next[repo] = base;
    else delete next[repo];
    save("mf_prbase", next);
    set({ prBaseByRepo: next });
  },
}));

/** Display name helper: alias if set, else the raw title. */
export function displayName(title: string): string {
  return useUi.getState().aliases[title] || title;
}

export type TourDecision = "open" | "skip" | "wait";

/** Should the welcome walkthrough open itself on this load?
 *
 * `tourDone` alone used to answer this, which is how a returning user who
 * cleared their browser storage — or opened the desktop app on a second machine
 * — got the twelve slides replayed at them. The server already knows better
 * (`general.onboarded`, from /api/config: true once a session has ever
 * existed), so it gets the deciding vote.
 *
 * `onboarded` is undefined until that request resolves, and the answer then is
 * "wait", never "open": popping the tour on a guess is the exact bug. The local
 * flags are still consulted first, so someone who has finished the tour or
 * turned hints off is left alone without waiting on the network.
 *
 * A returning user has by definition created a session, so the server's flag
 * covers the case that matters. What it does not cover is someone who watched the
 * slides, started nothing, and then opened a second device — they see the tour
 * again. That is the accepted price of never reporting the tour itself: the
 * frontend used to POST general.onboarded when the slideshow closed, and since
 * that flag means "this user is running sessions", it silently retired the grid's
 * setup card and the dependency checklist for a user who had neither. */
export function tourDecision(opts: {
  tourDone: boolean;
  hintsEnabled: boolean;
  onboarded: boolean | undefined;
}): TourDecision {
  if (opts.tourDone || !opts.hintsEnabled) return "skip";
  if (opts.onboarded === undefined) return "wait";
  return opts.onboarded ? "skip" : "open";
}
