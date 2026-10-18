/** What a session pane's header shows, and in what order — Customize → Pane
 * header (components/customize/PaneHeaderPicker.tsx), read by grid/Pane.tsx.
 *
 * Two kinds of item, each ordered among its own kind: TABS (Agent, Terminal,
 * Diff, Queue, Map, Thread) sit after the session's name, BUTTONS (⏩
 * Fast-track, Copy entire history, History, the Commit…/next-step button,
 * the cost chip, the diff-stat, Hide −) after them. ⋯ is not an item: it is
 * always the far-right element, and everything hidden here stays reachable
 * from it, so hiding never loses a feature.
 *
 * The default (the owner's list): Agent · Terminal · Diff · Queue · Map,
 * then ⏩ and Copy entire history. Everything else starts off and can be
 * switched on. (Map joined the default in 0.10: one switch away, nobody
 * found it. A saved choice that hid it keeps it hidden.)
 * Agent can't be hidden. A hidden tab you are ON still shows while you are on
 * it (opened from ⋯, a shortcut or a link), so the active tab always has a
 * place in its own header.
 *
 * Stored as the synced pref `pane_header` (lib/prefs.ts, localStorage
 * `mf_paneheader`): `{order, hidden}`, both lists of item ids, `null` = never
 * customized. An id the saved `order` never mentions is one added after the
 * save: it takes its default visibility and its default place, so a new item
 * neither vanishes for nor ambushes someone with an older choice.
 *
 * The calm first hour (lib/calm.ts) folds Queue, Map and ⏩ while the header
 * is still the default — a newcomer's header is name · Agent · Terminal ·
 * Diff · Copy — until each has state worth seeing: Map comes back as soon as
 * the session changed a file or has any zone state. Once the person has
 * customized it, calm leaves their choice alone: what they switched on is
 * what they see.
 *
 * Also here: `fitDecision`, the text → icons switch (see there). Pure. */

export type PaneTabId = "agent" | "shell" | "diff" | "queue" | "map" | "thread";
export type PaneButtonId = "fasttrack" | "copy" | "history" | "next" | "cost" | "diffstat" | "hide";
export type PaneItemId = PaneTabId | PaneButtonId;

export interface PaneItemDef {
  id: PaneItemId;
  kind: "tab" | "button";
  label: string;
  /** One line for Customize: what it is / when it shows. */
  note: string;
  /** Shown out of the box. */
  on: boolean;
  /** Can't be switched off (Agent). */
  locked?: boolean;
}

export const PANE_TABS: PaneItemDef[] = [
  { id: "agent", kind: "tab", label: "Agent", note: "The session's agent — always shown", on: true, locked: true },
  { id: "shell", kind: "tab", label: "Terminal", note: "A shell in the session's folder (not for a shared folder)", on: true },
  { id: "diff", kind: "tab", label: "Diff", note: "What the session changed (git sessions)", on: true },
  { id: "queue", kind: "tab", label: "Queue", note: "Send a message now or queue prompts to run when idle", on: true },
  { id: "map", kind: "tab", label: "Map", note: "The worktree as a tree: agents, zones, blast radius (git sessions)", on: true },
  { id: "thread", kind: "tab", label: "Thread", note: "Workers and what passed between them — only once a session has some", on: false },
];

export const PANE_BUTTONS: PaneItemDef[] = [
  { id: "fasttrack", kind: "button", label: "Fast-track ⏩", note: "Where the session goes when its turn ends: commit, push, PR…", on: true },
  { id: "copy", kind: "button", label: "Copy entire history", note: "The open terminal tab's whole scrollback to the clipboard", on: true },
  { id: "next", kind: "button", label: "Next step (Commit…)", note: "The guided button: Commit…, then Push, then Open PR", on: false },
  { id: "history", kind: "button", label: "History", note: "Scroll and select the full history like a page", on: false },
  { id: "cost", kind: "button", label: "Cost", note: "What the session has cost, click for its tokens", on: false },
  { id: "diffstat", kind: "button", label: "Diff stats", note: "+added −removed · files, against the base branch", on: false },
  { id: "hide", kind: "button", label: "Hide −", note: "Hide the window — the session keeps running", on: false },
];

export const PANE_ITEMS: PaneItemDef[] = [...PANE_TABS, ...PANE_BUTTONS];
const BY_ID = new Map(PANE_ITEMS.map((d) => [d.id, d]));

export function paneItemDef(id: PaneItemId): PaneItemDef {
  return BY_ID.get(id)!;
}

/** The stored choice (`pane_header`); null = the defaults. */
export interface PaneHeaderPref {
  order: string[];
  hidden: string[];
}

/** A stored value of the right shape, or null (anything else reads as unset). */
export function asPaneHeaderPref(v: unknown): PaneHeaderPref | null {
  if (!v || typeof v !== "object" || Array.isArray(v)) return null;
  const o = v as { order?: unknown; hidden?: unknown };
  const strs = (x: unknown) => (Array.isArray(x) ? x.filter((s): s is string => typeof s === "string") : null);
  const order = strs(o.order);
  const hidden = strs(o.hidden);
  if (!order || !hidden) return null;
  return { order, hidden };
}

/** One kind's ids in display order: the saved order first (unknown ids
 * dropped), then any the save never mentioned, each at its default place. */
function orderedKind(defs: PaneItemDef[], saved: string[]): PaneItemId[] {
  const known = new Set(defs.map((d) => d.id as string));
  const out: PaneItemId[] = [];
  for (const id of saved) if (known.has(id) && !out.includes(id as PaneItemId)) out.push(id as PaneItemId);
  defs.forEach((d, i) => {
    if (out.includes(d.id)) return;
    // Just after the default item before it that is placed, else first.
    let at = 0;
    for (let j = i - 1; j >= 0; j--) {
      const k = out.indexOf(defs[j].id);
      if (k >= 0) {
        at = k + 1;
        break;
      }
    }
    out.splice(at, 0, d.id);
  });
  return out;
}

export interface ResolvedHeader {
  tabs: PaneItemId[];
  buttons: PaneItemId[];
  /** Switched on (Agent always). */
  shown: Set<PaneItemId>;
}

/** Every item, ordered, and which are on — what Customize lists. */
export function resolvePaneHeader(pref: PaneHeaderPref | null): ResolvedHeader {
  const order = pref?.order ?? [];
  const mentioned = new Set(order);
  const hidden = new Set(pref?.hidden ?? []);
  const shown = new Set<PaneItemId>();
  for (const d of PANE_ITEMS) {
    const on = d.locked || (mentioned.has(d.id) ? !hidden.has(d.id) : d.on);
    if (on) shown.add(d.id);
  }
  return { tabs: orderedKind(PANE_TABS, order), buttons: orderedKind(PANE_BUTTONS, order), shown };
}

/** Back to a stored value from a resolved one. */
function toPref(r: ResolvedHeader): PaneHeaderPref {
  const order = [...r.tabs, ...r.buttons];
  return { order, hidden: order.filter((id) => !r.shown.has(id)) };
}

/** Switch one item on/off (Agent stays on). */
export function togglePaneItem(pref: PaneHeaderPref | null, id: PaneItemId): PaneHeaderPref {
  const r = resolvePaneHeader(pref);
  if (!paneItemDef(id)?.locked) {
    if (r.shown.has(id)) r.shown.delete(id);
    else r.shown.add(id);
  }
  return toPref(r);
}

/** Move one item up (-1) or down (+1) among its own kind. */
export function movePaneItem(pref: PaneHeaderPref | null, id: PaneItemId, dir: -1 | 1): PaneHeaderPref {
  const r = resolvePaneHeader(pref);
  const list = paneItemDef(id).kind === "tab" ? r.tabs : r.buttons;
  const i = list.indexOf(id);
  const j = i + dir;
  if (i >= 0 && j >= 0 && j < list.length) [list[i], list[j]] = [list[j], list[i]];
  return toPref(r);
}

/** Put `id` where `before` is (drag and drop), within its own kind; `before`
 * null = the end. */
export function placePaneItem(pref: PaneHeaderPref | null, id: PaneItemId, before: PaneItemId | null): PaneHeaderPref {
  const r = resolvePaneHeader(pref);
  const list = paneItemDef(id).kind === "tab" ? r.tabs : r.buttons;
  if (before && paneItemDef(before).kind !== paneItemDef(id).kind) return toPref(r);
  const from = list.indexOf(id);
  if (from < 0 || id === before) return toPref(r);
  list.splice(from, 1);
  const to = before ? list.indexOf(before) : list.length;
  list.splice(to < 0 ? list.length : to, 0, id);
  return toPref(r);
}

/** What one pane can draw, and its live state. */
export interface PaneHeaderCtx {
  /** The tab the pane is on. */
  active: string;
  /** A peer-shared folder: no Terminal (its shell would run outside the sandbox). */
  sharedFolder: boolean;
  /** A git session: Diff and Map exist. */
  git: boolean;
  /** The session has a Thread (workers, a parent, a lead, or opened on purpose). */
  thread: boolean;
  /** The calm first hour (lib/calm.ts). */
  calm: boolean;
  /** Queue state worth seeing: prompts queued or held at a usage limit. */
  queueBusy: boolean;
  /** Fast-track state worth seeing: a target set, running, halted, or its picker open. */
  ftBusy: boolean;
  /** Map state worth seeing: changed files or any zone state (lib/mapEntry
   * mapHasState). Absent = none. */
  mapBusy?: boolean;
}

/** The items this pane's header draws, in order: the person's choice, less
 * what this session can't have, plus the tab it is on. */
export function headerItems(pref: PaneHeaderPref | null, ctx: PaneHeaderCtx): { tabs: PaneTabId[]; buttons: PaneButtonId[] } {
  const r = resolvePaneHeader(pref);
  const possible = (id: PaneItemId) =>
    !(id === "shell" && ctx.sharedFolder) && !((id === "diff" || id === "map") && !ctx.git) && !(id === "thread" && !ctx.thread);
  // Calm folds Queue, Map and ⏩ — only while the header is the default,
  // and never what is state rather than an option.
  const calmFolds = (id: PaneItemId) =>
    ctx.calm &&
    pref === null &&
    ((id === "queue" && !ctx.queueBusy) || (id === "map" && !ctx.mapBusy) || (id === "fasttrack" && !ctx.ftBusy));
  const tabs = r.tabs.filter(
    (id) => possible(id) && (id === ctx.active || (r.shown.has(id) && !calmFolds(id)))
  ) as PaneTabId[];
  const buttons = r.buttons.filter((id) => r.shown.has(id) && !calmFolds(id)) as PaneButtonId[];
  return { tabs, buttons };
}

// --- Text until it doesn't fit, then icons ------------------------------------

/** Room the header must still leave the session's name before it gives up
 * its words: a name squeezed below this reads as nothing, so the words go
 * first. A shorter name needs only its own width. */
export const TITLE_ROOM_PX = 110;

/** Extra room needed before the words come BACK: without it a header right
 * at the edge would flip between words and icons on every pixel of a drag. */
export const FIT_HYSTERESIS_PX = 24;

export interface FitState {
  icons: boolean;
  /** What the header needed with words, measured the last time it showed
   * them (null = not measured since its contents changed). */
  textNeed: number | null;
}

/** One step of the words ↔ icons switch.
 *
 * `avail` is the header's width; `need` the width its items take as they are
 * drawn now (words or icons), name at its reserved room. With words, `need`
 * is remembered, and more than `avail` turns icons on. With icons, the words
 * come back only once `avail` holds the remembered need plus the hysteresis —
 * or, when that need is unknown (the contents changed), to measure it: the
 * caller does that before paint, so it never shows. */
export function fitDecision(cur: FitState, avail: number, need: number, hyst = FIT_HYSTERESIS_PX): FitState {
  if (avail <= 0) return cur;
  if (!cur.icons) return { icons: need > avail, textNeed: need };
  if (cur.textNeed === null) return { icons: false, textNeed: null };
  return { icons: avail < cur.textNeed + hyst, textNeed: cur.textNeed };
}
