/** Client-side UI state (Zustand). Server state lives in TanStack Query
 * (state/queries.ts) — this store only holds what the browser owns: focus,
 * layout, filters, selections, and which overlays are open.
 *
 * localStorage keys are UNCHANGED from the vanilla app (cs_* / mf_*) so an
 * upgrade keeps every user's layout, order, aliases, and bindings. */

import { create } from "zustand";
import { defaultHiddenBars } from "../components/sidebar/barDefs";

export type ViewMode = "auto" | "2" | "4" | "9";

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

export type DialogName =
  | "new-session"
  | "settings"
  | "commit"
  | "make-pr"
  | "rename"
  | "device"
  | "workspaces"
  | "recent"
  | "prompts"
  | "setup"
  | "todo"
  | "assistant-agent"
  | "palette"
  | "shortcuts";

interface UiState {
  /** Logically focused session title (keyboard target, MRU head). */
  focused: string | null;
  /** Grid view cap. */
  viewMode: ViewMode;
  /** Persisted row layout: array of arrays of titles. */
  gridRows: string[][];
  sidebarHidden: boolean;
  /** User drag order of sidebar rows (stable; selection never reorders). */
  order: string[];
  /** Most-recently-used order (selection updates it; fills fixed views). */
  mru: string[];
  /** Sidebar filter text, lowercased. */
  filter: string;
  /** Titles hidden from the grid. */
  hidden: Set<string>;
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
  /** Payload for dialogs that target a session (commit/rename/device…). */
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
  setOrder(order: string[]): void;
  moveInOrder(title: string, before: string | null): void;
  setFilter(f: string): void;
  setHidden(title: string, hidden: boolean): void;
  toggleBulk(title: string): void;
  clearBulk(): void;
  setAlias(title: string, alias: string): void;
  toggleDeviceCollapsed(device: string): void;
  toggleBarHidden(key: string): void;
  setBarOrder(order: string[]): void;
  /** Toggle the reduce-motion terminal cover. */
  setReduceMotion(on: boolean): void;
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
  order: load<string[]>("cs_order", []),
  mru: load<string[]>("cs_mru", []),
  filter: "",
  hidden: new Set(load<string[]>("mf_hidden", [])),
  bulkSelected: new Set<string>(),
  aliases: load<Record<string, string>>("mf_aliases", {}),
  collapsedDevices: new Set(load<string[]>("cs_devcollapse", [])),
  // Fresh users start with only the essentials (Usage + Assistant) so a first
  // run isn't overwhelming; the rest are one click away in Customize. Once the
  // user touches Customize the saved set wins, empty included.
  hiddenBars: new Set(
    firstRun("mf_hiddenbars") ? defaultHiddenBars() : load<string[]>("mf_hiddenbars", [])
  ),
  barOrder: load<string[]>("mf_barorder", []),
  reduceMotion: load<boolean>("mf_reduce_motion", false),
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
  setOrder: (order) => {
    save("cs_order", order);
    set({ order });
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
