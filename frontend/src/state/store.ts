/** Client-side UI state (Zustand). Server state lives in TanStack Query
 * (state/queries.ts) — this store only holds what the browser owns: focus,
 * layout, filters, selections, and which overlays are open.
 *
 * localStorage keys are UNCHANGED from the vanilla app (cs_* / mf_*) so an
 * upgrade keeps every user's layout, order, aliases, and bindings. */

import { create } from "zustand";
import { api, ApiError } from "../api/client";
import {
  BREAK_DEFAULT_MINUTES,
  clampBreakMinutes,
  clampIdleMinutes,
  IDLE_DEFAULT_MINUTES,
} from "../lib/breakTimer";
import { defaultHiddenBars } from "../components/sidebar/barDefs";
import { notePrefWrite } from "../lib/prefs";
import { asPaneHeaderPref, type PaneHeaderPref } from "../lib/paneHeader";
import { toast } from "../lib/toast";
import type { TourId } from "../lib/tours";

export type ViewMode = "auto" | "2" | "4" | "9";

/** Sidebar width bounds (px).
 *
 * The floor is the width the sidebar shipped at for its whole life, and it is a
 * floor rather than a suggestion because narrower genuinely breaks the row: the
 * intrinsic minimum of a row (number + dot + the title's 34px legible floor +
 * a 96px stage chip + the ⋮ + ✕) is ~240px, so below that the chip and the ✕
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

/** The synced `tours_seen` list as this browser has it (strings only). */
function loadToursSeen(): string[] {
  const v = load<unknown>("mf_tours_seen", []);
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
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

/** Persist one key. A key that is also a person-wide pref (bars, break and
 * idle reminders, hints — see lib/prefs.ts PREF_MAP) is mirrored to the
 * server too, so it follows you to your other devices. */
function save(key: string, value: unknown, stringify = true) {
  try {
    localStorage.setItem(key, stringify ? JSON.stringify(value) : String(value));
  } catch {
    /* storage unavailable */
  }
  notePrefWrite(key);
}

/** Forget one key — "back to the default"; a synced pref is cleared on the
 * server too (an absent key is sent as null). */
function drop(key: string) {
  try {
    localStorage.removeItem(key);
  } catch {
    /* storage unavailable */
  }
  notePrefWrite(key);
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
  // Customize (CustomizeDialog): "customize" opens its Sidebar tab, and
  // "prompts" below opens it on that tab.
  | "customize"
  | "verify"
  | "commit"
  | "make-pr"
  | "rename"
  // "workspaces" (the disk manager) was folded into "recent" — one page.
  | "recent"
  // Customize → Prompts: the saved-prompt library.
  | "prompts"
  | "setup"
  | "todo"
  | "assistant-agent"
  | "palette"
  | "shortcuts"
  // Repo-level red zones + Plan-first, managed outside any one session.
  | "red-zones"
  // Schedule (TemplatesDialog): the top bar's Schedule (Alt+S), New's
  // templates strip, the palette's "Schedule…" / "Templates…". dialogTarget
  // "new" = reopen New on close (lib/templates' readTemplatesTarget).
  | "schedule"
  // The surface's name before it was Schedule. Still accepted by
  // openDialogFor (old call sites, a saved #templates link) and opened as
  // "schedule" — the store never holds it.
  | "templates"
  // The one dialog every extension's dialog surfaces share; the extension id +
  // surface ride in dialogTarget ("<ext>:<surface>[:<ref>]").
  | "extension"
  // A peer's shared session, watched from here (PeerSessionViewer); the
  // link id rides in dialogTarget.
  | "peer-session";

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
  /** Select mode: a plain click on a rail row picks it instead of focusing it.
   * Entered by Ctrl/⌘-click, Shift-click or a row menu's "Select"; the bulk
   * bar at the top of the list shows while it is on. */
  bulkMode: boolean;
  /** Picked titles — highlighted on the rail rows themselves. */
  bulkSelected: Set<string>;
  /** The last row picked: where a Shift-click range starts. */
  bulkAnchor: string | null;
  /** Display aliases: title -> custom label. */
  aliases: Record<string, string>;
  /** Collapsed device groups in the sidebar. */
  collapsedDevices: Set<string>;
  /** Folded run groups (sessions started together) in the sidebar, by run id.
   * Same contract as collapsedDevices: a folded group's rows leave railOrder,
   * so Alt+N numbering skips them exactly as it skips a folded device. */
  collapsedRuns: Set<string>;
  /** Orchestrators whose sub-sessions are folded away in the sidebar, by the
   * parent's title (lib/familyFold). Same contract as collapsedRuns: folded
   * rows leave railOrder, so Alt+N numbering skips them. */
  collapsedFamilies: Set<string>;
  /** Sidebar bars hidden via the footer Customize menu (keys in BAR_KEYS). */
  hiddenBars: Set<string>;
  /** The person chose `hiddenBars` (Customize, here or on another device);
   * false = they are the defaults, which the calm first hour may extend
   * (lib/calm.ts, barDefs.setCalmBarDefaults). */
  barsExplicit: boolean;
  /** Re-derive `hiddenBars` from the defaults — only while not explicit. */
  applyDefaultBars(): void;
  /** The window is phone-narrow (App watches it): the sidebar becomes an
   * overlay that starts closed, and "Auto" view shows one pane. */
  narrow: boolean;
  setNarrow(on: boolean): void;
  /** Customize → "Filter box": show the session filter above the list. Off,
   * "/" still opens it for one search (filterPeek). */
  showFilter: boolean;
  /** The filter box was summoned by "/" while Customize hides it. */
  filterPeek: boolean;
  /** User drag order of the sidebar bars (keys; see barDefs.ts). Empty = default. */
  barOrder: string[];
  /** Customize → Sidebar's "Reset to default": the starter bars, the default
   * order and the filter box, as on a fresh install (the calm first hour
   * may extend the defaults again). */
  resetSidebarBars(): void;
  /** What a session pane's header shows, in what order (lib/paneHeader,
   * synced pref `pane_header`); null = the defaults. */
  paneHeader: PaneHeaderPref | null;
  /** Save a pane-header choice; null = back to the defaults. */
  setPaneHeader(pref: PaneHeaderPref | null): void;
  /** Customize's open page (this browser's own convenience, not a pref). */
  customizePage: "sidebar" | "pane";
  setCustomizePage(page: "sidebar" | "pane"): void;
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
  /** The walkthrough overlay is currently open (transient, not persisted).
   * The palette's "Tour", the ? sheet and Settings → General's Replay open
   * it; it opens by itself once, on a first launch (onboarding/WelcomeTour's
   * useFirstLaunchTour), until `welcomeTourSeen`. */
  tourOpen: boolean;
  /** The welcome tour has been shown (synced pref `welcome_tour`). */
  welcomeTourSeen: boolean;
  /** Light mode is on (the <html> `light` class; synced pref `theme`). Change
   * it with lib/theme.ts `setTheme`, never by toggling the class directly. */
  themeLight: boolean;
  /** Remember the welcome tour as shown — on every device. */
  markWelcomeTourSeen(): void;
  /** The Map tab's tour has been seen (synced pref `map_tour`): it opens itself on a first Map only until then. */
  mapTourSeen: boolean;
  /** Remember the Map tour as seen (Done / Skip / Esc). */
  markMapTourSeen(): void;
  /** The coach-mark tour on screen (components/onboarding/CoachTour.tsx), and
   * its step; null = none. Transient: a reload never resumes one. */
  coachTour: { id: TourId; step: number } | null;
  /** Tours seen or dismissed, on any device (synced pref `tours_seen`): a
   * tour opens by itself (useAutoTour) only while its id is not in here. */
  toursSeen: string[];
  /** Show tour `id` from its first step (a "?" button, a Replay, or
   * useAutoTour's one automatic opening). */
  openCoachTour(id: TourId): void;
  /** Move the open coach tour to `step`. */
  setCoachStep(step: number): void;
  /** Close the coach tour, however it ends (Done, Skip, Esc, its dialog
   * closing) — and remember it as seen, on every device. */
  closeCoachTour(): void;
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
  /** Who the Thread composer should be addressed to, set by `threadOpen`:
   * `title` is the session whose Thread tab was opened, `to` the member the
   * message goes to. `seq` bumps on every open, so a second Ctrl+K S at the
   * same target still moves the caret into the composer. Transient. */
  threadComposeTarget: ThreadComposeTarget | null;
  /** Where the Map tab should land, set by `openMap`: the session, then an
   * optional file to fly to and open the card of, and/or a part of the Map to
   * show (a Session-panel section, or the ⚠ Could break layer). `seq` bumps on
   * every open so the same focus twice still re-flies. Transient; the Map tab
   * consumes it when it is active and has the tree. */
  mapFocus: MapFocus | null;
  /** The file the Diff tab should select, set by `openDiff`. Transient. */
  diffFocus: DiffFocus | null;
  /** When this browser last looked at each session's Thread tab (epoch ms) —
   * the tab's badge counts results newer than this. Persisted. */
  threadLastSeen: Record<string, number>;
  /** The pane whose ⏩ fast-track picker is open. Transient. */
  fastTrackMenu: FastTrackMenuState | null;
  /** Text the New dialog's describe box opens with, once — Intake's "Start
   * together…" hands its ticked tickets over this way. Transient. */
  newPrefill: string;

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
  /** Open / close the bulk bar; closing also drops the selection. */
  setBulkMode(on: boolean): void;
  toggleBulk(title: string): void;
  setBulk(titles: string[], on: boolean): void;
  /** Pick a row from the rail: Ctrl/⌘ toggles it, Shift picks the range from
   * the last pick (rail order). Either way select mode turns on. */
  pickRow(title: string, range: boolean): void;
  clearBulk(): void;
  setShowFilter(on: boolean): void;
  setFilterPeek(on: boolean): void;
  setAlias(title: string, alias: string): void;
  toggleDeviceCollapsed(device: string): void;
  toggleRunCollapsed(runId: string): void;
  toggleFamilyCollapsed(parent: string): void;
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
  /** Open the welcome walkthrough (a manual replay — never automatic). */
  openTour(): void;
  /** Close the walkthrough. */
  finishTour(): void;
  setLastTab(title: string, tab: string): void;
  openDialogFor(name: DialogName, target?: string | null): void;
  closeDialog(): void;
  setPrBase(repo: string, base: string): void;
  /** Bring a session's pane forward on its Thread tab, with the composer
   * addressed to `composeTo` (default: the session itself). */
  threadOpen(title: string, opts?: { composeTo?: string }): void;
  /** Bring a session's pane forward on its Map tab, optionally flying to a
   * file and/or opening a part of the Map (see `mapFocus`). Every "Open map"
   * in the app goes through here, so it lands where the user was looking. */
  openMap(title: string, focus?: { path?: string; section?: MapSection }): void;
  /** Bring a session's pane forward on its Diff tab, optionally on one file. */
  openDiff(title: string, path?: string): void;
  /** Record that the Thread tab of `title` was looked at (now, by default). */
  setThreadLastSeen(title: string, ts?: number): void;
  /** Open the ⏩ fast-track picker on a session's pane, or close it (null). */
  setFastTrackMenu(menu: FastTrackMenuState | null): void;
  /** Open New with `text` already in its describe box (list mode when it is
   * a list or ticket IDs). */
  openNewWith(text: string): void;
  /** The prefill for this opening of New, cleared as it is read. */
  takeNewPrefill(): string;
}

export interface ThreadComposeTarget {
  title: string;
  to: string;
  seq: number;
}

/** A part of the Map `openMap` can open: a Session-panel section, or the
 * bird index's ⚠ Could break layer ("risk"). */
export type MapSection = "breaches" | "scope" | "plan" | "blast" | "zones" | "changed" | "others" | "risk";

export interface MapFocus {
  title: string;
  path?: string;
  section?: MapSection;
  seq: number;
}

export interface DiffFocus {
  title: string;
  path?: string;
  seq: number;
}

export interface FastTrackMenuState {
  title: string;
}

/** How the store brings a session forward. The real one is
 * lib/sessionActions' selectSession — the one selection authority, with the
 * capped-view demotion — which registers itself on import. The store cannot
 * import it directly (sessionActions imports the store), and the fallback is
 * only what a store-only caller (a unit test) needs. */
type SessionSelector = (title: string, opts?: { noKeyboard?: boolean }) => void;
let _selectSession: SessionSelector | null = null;
export function setSessionSelector(fn: SessionSelector | null): void {
  _selectSession = fn;
}
let _threadSeq = 0;
let _focusSeq = 0;

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
  bulkMode: false,
  bulkSelected: new Set<string>(),
  bulkAnchor: null,
  aliases: load<Record<string, string>>("mf_aliases", {}),
  collapsedDevices: new Set(load<string[]>("cs_devcollapse", [])),
  collapsedRuns: new Set(load<string[]>("mf_runcollapse", [])),
  collapsedFamilies: new Set(load<string[]>("mf_familycollapse", [])),
  // Fresh users start with the essentials (Usage + Tickets + Assistant)
  // so a first run isn't overwhelming; the rest are one click away in Customize.
  // Once the user touches Customize the saved set wins, empty included.
  barsExplicit: !firstRun("mf_hiddenbars"),
  narrow: false,
  hiddenBars: new Set(
    firstRun("mf_hiddenbars") ? defaultHiddenBars() : load<string[]>("mf_hiddenbars", [])
  ),
  barOrder: load<string[]>("mf_barorder", []),
  paneHeader: asPaneHeaderPref(load<unknown>("mf_paneheader", null)),
  customizePage: load<string>("mf_customize_page", "sidebar") === "pane" ? "pane" : "sidebar",
  showFilter: load<boolean>("mf_show_filter", true),
  filterPeek: false,
  reduceMotion: load<boolean>("mf_reduce_motion", false),
  breakReminder: load<boolean>("mf_break_on", false),
  breakEveryMin: clampBreakMinutes(load<number>("mf_break_every", BREAK_DEFAULT_MINUTES)),
  idleFlock: load<boolean>("mf_idle_flock", true),
  idleFlockAfterMin: clampIdleMinutes(load<number>("mf_idle_after", IDLE_DEFAULT_MINUTES)),
  hintsEnabled: load<boolean>("mf_hints", true),
  dismissedHints: new Set(load<string[]>("mf_hints_seen", [])),
  tourOpen: false,
  mapTourSeen: load<boolean>("mf_map_tour", false) === true,
  welcomeTourSeen: load<boolean>("mf_welcome_tour", false) === true,
  coachTour: null,
  toursSeen: loadToursSeen(),
  // index.html's pre-paint script has already decided (saved choice, else the
  // OS colour scheme), so the class is the answer.
  themeLight: typeof document !== "undefined" && document.documentElement.classList.contains("light"),
  lastTab: load<Record<string, string>>("cs_lasttab", {}),
  openDialog: null,
  dialogTarget: null,
  prBaseByRepo: load<Record<string, string>>("mf_prbase", {}),
  threadComposeTarget: null,
  mapFocus: null,
  diffFocus: null,
  threadLastSeen: load<Record<string, number>>("mf_thread_seen", {}),
  fastTrackMenu: null,
  newPrefill: "",

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
    // A narrow window's overlay opens and closes without touching the saved
    // wide-window choice.
    if (!get().narrow) save("cs_sidebar", hidden ? "hidden" : "", false);
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
  setBulkMode: (on) =>
    set(on ? { bulkMode: true } : { bulkMode: false, bulkSelected: new Set(), bulkAnchor: null }),
  toggleBulk: (title) => {
    const next = new Set(get().bulkSelected);
    if (next.has(title)) next.delete(title);
    else next.add(title);
    set({ bulkSelected: next, bulkAnchor: title, bulkMode: true });
  },
  pickRow: (title, range) => {
    const { bulkAnchor, railOrder, bulkSelected } = get();
    const from = range && bulkAnchor ? railOrder.indexOf(bulkAnchor) : -1;
    const to = railOrder.indexOf(title);
    if (from < 0 || to < 0) {
      get().toggleBulk(title);
      return;
    }
    // A range adds; it never un-picks what was picked outside it. Window
    // rows ride in railOrder as sentinels — only sessions are picked.
    const next = new Set(bulkSelected);
    const [a, b] = from < to ? [from, to] : [to, from];
    for (const k of railOrder.slice(a, b + 1)) if (!k.startsWith("\u0000")) next.add(k);
    set({ bulkSelected: next, bulkAnchor: title, bulkMode: true });
  },
  setBulk: (titles, on) => {
    const next = new Set(get().bulkSelected);
    for (const t of titles) {
      if (on) next.add(t);
      else next.delete(t);
    }
    set({ bulkSelected: next });
  },
  clearBulk: () => set({ bulkSelected: new Set(), bulkAnchor: null }),
  setShowFilter: (on) => {
    save("mf_show_filter", on);
    set({ showFilter: on, filterPeek: false });
  },
  setFilterPeek: (on) => set({ filterPeek: on }),
  setAlias: (title, alias) => {
    const prev = get().aliases[title] || "";
    const write = (value: string) => {
      const aliases = { ...get().aliases };
      if (value) aliases[title] = value;
      else delete aliases[title];
      save("mf_aliases", aliases);
      set({ aliases });
    };
    write(alias);
    // Mirror the rename to the server: it serves the name to the phone and to
    // pushes, and it refuses a name another session already has (409) — the
    // rename is then undone here too, so no two windows share a name.
    api("/api/aliases", { json: { title, alias: alias || "" } }).catch((err) => {
      if (!(err instanceof ApiError) || err.status !== 409) return; // offline: keep it
      if ((get().aliases[title] || "") === alias) write(prev);
      toast(err.message || `Another session is already named “${alias}”`);
    });
  },
  toggleDeviceCollapsed: (device) => {
    const next = new Set(get().collapsedDevices);
    if (next.has(device)) next.delete(device);
    else next.add(device);
    save("cs_devcollapse", [...next]);
    set({ collapsedDevices: next });
  },
  toggleRunCollapsed: (runId) => {
    const next = new Set(get().collapsedRuns);
    if (next.has(runId)) next.delete(runId);
    else next.add(runId);
    save("mf_runcollapse", [...next]);
    set({ collapsedRuns: next });
  },
  toggleFamilyCollapsed: (parent) => {
    const next = new Set(get().collapsedFamilies);
    if (next.has(parent)) next.delete(parent);
    else next.add(parent);
    save("mf_familycollapse", [...next]);
    set({ collapsedFamilies: next });
  },
  toggleBarHidden: (key) => {
    const next = new Set(get().hiddenBars);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    save("mf_hiddenbars", [...next]);
    set({ hiddenBars: next, barsExplicit: true });
  },
  applyDefaultBars: () => {
    if (get().barsExplicit) return;
    const next = new Set(defaultHiddenBars());
    const cur = get().hiddenBars;
    if (next.size === cur.size && [...next].every((k) => cur.has(k))) return;
    set({ hiddenBars: next });
  },
  setNarrow: (on) => {
    if (on === get().narrow) return;
    // Narrow: the sidebar starts closed (an overlay you open with ▯), and
    // nothing is saved — widen the window and your own choice is back.
    if (on) set({ narrow: true, sidebarHidden: true });
    else set({ narrow: false, sidebarHidden: load<string>("cs_sidebar", "", false) === "hidden" });
  },
  setBarOrder: (order) => {
    save("mf_barorder", order);
    set({ barOrder: order });
  },
  resetSidebarBars: () => {
    drop("mf_hiddenbars");
    drop("mf_barorder");
    save("mf_show_filter", true);
    set({ hiddenBars: new Set(defaultHiddenBars()), barsExplicit: false, barOrder: [], showFilter: true });
  },
  setPaneHeader: (pref) => {
    if (pref) save("mf_paneheader", pref);
    else drop("mf_paneheader");
    set({ paneHeader: pref });
  },
  setCustomizePage: (page) => {
    save("mf_customize_page", page);
    set({ customizePage: page });
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
  markMapTourSeen: () => {
    if (get().mapTourSeen) return;
    save("mf_map_tour", true);
    set({ mapTourSeen: true });
  },
  markWelcomeTourSeen: () => {
    if (get().welcomeTourSeen) return;
    save("mf_welcome_tour", true);
    set({ welcomeTourSeen: true });
  },
  openCoachTour: (id) => set({ coachTour: { id, step: 0 } }),
  setCoachStep: (step) => {
    const t = get().coachTour;
    if (t && t.step !== step) set({ coachTour: { ...t, step } });
  },
  closeCoachTour: () => {
    const t = get().coachTour;
    if (!t) return;
    const seen = get().toursSeen;
    if (seen.includes(t.id)) set({ coachTour: null });
    else {
      const next = [...seen, t.id];
      save("mf_tours_seen", next);
      set({ coachTour: null, toursSeen: next });
    }
  },
  openTour: () => set({ tourOpen: true }),
  finishTour: () => set({ tourOpen: false }),
  setLastTab: (title, tab) => {
    const lastTab = { ...get().lastTab, [title]: tab };
    save("cs_lasttab", lastTab);
    set({ lastTab });
  },
  openDialogFor: (name, target = null) =>
    set({ openDialog: name === "templates" ? "schedule" : name, dialogTarget: target }),
  closeDialog: () => set({ openDialog: null, dialogTarget: null }),
  setPrBase: (repo, base) => {
    if (!repo) return;
    const next = { ...get().prBaseByRepo };
    if (base) next[repo] = base;
    else delete next[repo];
    save("mf_prbase", next);
    set({ prBaseByRepo: next });
  },
  threadOpen: (title, opts) => {
    if (!title) return;
    // Keyboard-less: the Thread tab's composer takes the caret, not the
    // terminal — selecting with the keyboard would hand it to the agent pane
    // and a message typed next would land in the agent's input box.
    if (_selectSession) _selectSession(title, { noKeyboard: true });
    else {
      get().setHidden(title, false);
      get().touchMru(title);
      get().setFocused(title);
    }
    get().setLastTab(title, "thread");
    set({
      fastTrackMenu: null,
      threadComposeTarget: { title, to: opts?.composeTo || title, seq: ++_threadSeq },
    });
  },
  openMap: (title, focus) => {
    if (!title) return;
    if (_selectSession) _selectSession(title, { noKeyboard: true });
    else {
      get().setHidden(title, false);
      get().touchMru(title);
      get().setFocused(title);
    }
    get().setLastTab(title, "map");
    set({ mapFocus: { title, path: focus?.path || undefined, section: focus?.section, seq: ++_focusSeq } });
  },
  openDiff: (title, path) => {
    if (!title) return;
    if (_selectSession) _selectSession(title, { noKeyboard: true });
    else {
      get().setHidden(title, false);
      get().touchMru(title);
      get().setFocused(title);
    }
    get().setLastTab(title, "diff");
    set({ diffFocus: { title, path: path || undefined, seq: ++_focusSeq } });
  },
  setThreadLastSeen: (title, ts) => {
    if (!title) return;
    const threadLastSeen = { ...get().threadLastSeen, [title]: ts ?? Date.now() };
    save("mf_thread_seen", threadLastSeen);
    set({ threadLastSeen });
  },
  setFastTrackMenu: (menu) => set({ fastTrackMenu: menu }),
  openNewWith: (text) => set({ newPrefill: text, openDialog: "new-session", dialogTarget: null }),
  takeNewPrefill: () => {
    const t = get().newPrefill;
    if (t) set({ newPrefill: "" });
    return t;
  },
}));

/** Display name helper: alias if set, else the raw title. */
/** The view the grid actually uses: "Auto" means one pane in a narrow
 * window (two terminals side by side at phone width are a word per line). */
export function effectiveViewMode(s: { viewMode: ViewMode; narrow: boolean }): ViewMode {
  return s.narrow && s.viewMode === "auto" ? ("1" as ViewMode) : s.viewMode;
}

export function displayName(title: string): string {
  return useUi.getState().aliases[title] || title;
}

// --- New session: the last-used choice, per repo ---------------------------- //

/** What Name → Enter reuses: the template, agent and account the last
 * session in that folder was started with. Per browser (this is a hand's
 * habit, not a setting), keyed by the folder's path; `folder` is the last
 * folder used anywhere, for the moment before the server's own suggestion
 * lands. */
export interface NewSessionChoice {
  template: string;
  program: string;
  profile_id: string;
}

export const LAST_NEW_SESSION_KEY = "mf_new_session_last";

interface LastNewSession {
  folder: string;
  byRepo: Record<string, NewSessionChoice>;
}

type KV = { getItem(k: string): string | null; setItem(k: string, v: string): void };

function readLastNew(kv: KV | null): LastNewSession {
  try {
    const raw = kv ? JSON.parse(kv.getItem(LAST_NEW_SESSION_KEY) || "null") : null;
    if (raw && typeof raw === "object")
      return {
        folder: typeof raw.folder === "string" ? raw.folder : "",
        byRepo: raw.byRepo && typeof raw.byRepo === "object" ? raw.byRepo : {},
      };
  } catch {
    /* unreadable = never set */
  }
  return { folder: "", byRepo: {} };
}

function lsKV(): KV | null {
  try {
    return typeof localStorage !== "undefined" ? localStorage : null;
  } catch {
    return null;
  }
}

/** The remembered choice for `repo` (else for the last folder used, else
 * null), and the last folder. */
export function lastNewSessionChoice(
  repo: string,
  kv: KV | null = lsKV()
): { folder: string; choice: NewSessionChoice | null } {
  const last = readLastNew(kv);
  const key = (repo || "").trim() || last.folder;
  return { folder: last.folder, choice: (key && last.byRepo[key]) || null };
}

/** Forget the remembered account for `repo` (resolved the way
 * lastNewSessionChoice reads it) — it was removed, and pre-filling it would
 * post an id the server refuses. The rest of the entry stays. */
export function forgetNewSessionProfile(repo: string, kv: KV | null = lsKV()): void {
  if (!kv) return;
  const last = readLastNew(kv);
  const key = (repo || "").trim() || last.folder;
  const choice = key ? last.byRepo[key] : null;
  if (!choice || !choice.profile_id) return;
  const byRepo = { ...last.byRepo, [key]: { ...choice, profile_id: "" } };
  try {
    kv.setItem(LAST_NEW_SESSION_KEY, JSON.stringify({ folder: last.folder, byRepo }));
  } catch {
    /* storage unavailable */
  }
}

/** Record what a session was just started with. Only 32 folders are kept:
 * a habit per repo, not a log. */
export function rememberNewSessionChoice(
  repo: string,
  choice: NewSessionChoice,
  kv: KV | null = lsKV()
): void {
  const folder = (repo || "").trim();
  if (!folder || !kv) return;
  const last = readLastNew(kv);
  const byRepo: Record<string, NewSessionChoice> = { ...last.byRepo };
  delete byRepo[folder];
  const entries = Object.entries(byRepo).slice(-31);
  entries.push([folder, { template: choice.template || "", program: choice.program || "", profile_id: choice.profile_id || "" }]);
  try {
    kv.setItem(LAST_NEW_SESSION_KEY, JSON.stringify({ folder, byRepo: Object.fromEntries(entries) }));
  } catch {
    /* storage unavailable */
  }
}
