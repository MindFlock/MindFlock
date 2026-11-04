/** Renaming a session renames the SESSION: its title, on the server
 * (`POST /api/instances/{title}/rename {to}`). There is no hidden second
 * name any more — what the rail shows is what the session is called, in the
 * API, on the phone, in a push, to an agent.
 *
 * The browser keys a lot by title (the rail order, the grid rows, the MRU,
 * the Sky's layout, a pane's last tab, …), so a rename has to carry all of it
 * to the new title, or the renamed session would come back as a stranger at
 * the bottom of the rail with none of its layout. `moveSessionTitle` is the
 * one place that does that. It runs after this browser's own rename answers,
 * on a `session.renamed` event from another browser (lib/aliasSync), and for
 * each session the boot alias migration renamed — so it is idempotent: the
 * renaming browser gets its own answer AND the event, and the second call
 * finds nothing left under the old title.
 *
 * Old renames (aliases, `mf_aliases`) still show through `displayName` until
 * the boot seed migrates them (lib/aliasSync); one the server could not
 * migrate (the name is taken, or not a valid title) stays a label. */

import { ApiError, instApi } from "../api/client";
import type { Instance } from "../api/types";
import { queryClient, refreshInstances } from "../state/queries";
import { useUi } from "../state/store";
import { renameCodemapSeen } from "./codemapSeen";
import { errorPop } from "./errorPop";
import { errMsg } from "./format";
import { loadSkyLayout, renameSkyLine, saveSkyLayout, SKY_POS_KEY } from "./skyLayout";
import { renameNodePos } from "./skyNodePos";
import { toast } from "./toast";

/** The title-keyed localStorage this moves (key → shape). Listed for a
 * reviewer (the inventory test pins it): a new title-keyed key belongs here
 * AND in moveSessionTitle. */
export const TITLE_KEYED_STORAGE: Readonly<Record<string, string>> = {
  cs_order: "list of titles (+ window sentinels): the rail's drag order",
  cs_mru: "list of titles (+ sentinels): most recently used",
  cs_gridrows: "rows of titles: the grid layout",
  mf_hidden: "list of titles hidden from the grid",
  mf_familycollapse: "list of parent titles whose sub-sessions are folded",
  mf_aliases: "{title: label} — the old renames: the renamed title's is dropped",
  cs_lasttab: "{title: tab} — a pane's last tab",
  mf_thread_seen: "{title: ms} — when its Thread tab was last looked at",
  mf_codemap_seen: "{title: s} — when its Map was last looked at (lib/codemapSeen)",
  mf_sky_layout: "{title: line layout} — the Sky (lib/skyLayout)",
  mf_sky_pos: "{title: {x, y}} — the Sky's older positions record",
  mf_sky_node_pos: "{chain key = title: {node: pos}} — a chain's free nodes (lib/skyNodePos)",
};

/** Transient store fields that name a session, moved too. */
export const TITLE_KEYED_STATE = [
  "focused",
  "railOrder",
  "verifyPanes",
  "bulkSelected",
  "bulkAnchor",
  "dialogTarget",
  "threadComposeTarget",
  "mapFocus",
  "diffFocus",
  "fastTrackMenu",
] as const;

/** A window token for a verify run's pane (store windowKey("verify", t)). */
const VERIFY = "\u0000verify:";

type Listener = (from: string, to: string) => void;
const listeners = new Set<Listener>();

/** For state a module keeps by title outside the store (a Thread draft, the
 * last commit message, the Sky's layout in component state): called on every
 * move, after the store's. Returns the unsubscribe. */
export function onSessionRenamed(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** A Map keyed by title: `from`'s entry moves to `to` (kept when `to` already
 * has one). With `values`, a value naming `from` (an addressee) follows too. */
export function renameMapKey<V>(m: Map<string, V>, from: string, to: string, opts: { values?: boolean } = {}): void {
  if (m.has(from)) {
    if (!m.has(to)) m.set(to, m.get(from) as V);
    m.delete(from);
  }
  if (opts.values) for (const [k, v] of m) if ((v as unknown) === from) m.set(k, to as unknown as V);
}

function persist(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable: the move holds for this page */
  }
}

function readObj(key: string): Record<string, unknown> | null {
  try {
    const v = JSON.parse(localStorage.getItem(key) || "null");
    return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** Move every piece of this browser's per-session state from title `from` to
 * title `to`. Idempotent; a no-op for an empty or unchanged pair. */
export function moveSessionTitle(from: string, to: string): void {
  if (!from || !to || from === to) return;
  const tok = (t: string) => (t === from ? to : t === VERIFY + from ? VERIFY + to : t);
  /** The list with `from` swapped for `to` — the same array when nothing
   * changed; a `to` already present keeps its first place. */
  const swap = (list: readonly string[]): string[] => {
    if (!list.some((t) => tok(t) !== t)) return list as string[];
    const seen = new Set<string>();
    const out: string[] = [];
    for (const t of list.map(tok)) if (!seen.has(t)) (seen.add(t), out.push(t));
    return out;
  };
  /** The record with `from`'s entry under `to` (a `to` entry already there
   * wins), or the same record. */
  const rekey = <V>(rec: Record<string, V>): Record<string, V> => {
    if (!(from in rec)) return rec;
    const next: Record<string, V> = { ...rec };
    if (!(to in next)) next[to] = next[from];
    delete next[from];
    return next;
  };

  const s = useUi.getState();
  const patch: Record<string, unknown> = {};

  const order = swap(s.order);
  if (order !== s.order) (patch.order = order), persist("cs_order", order);
  const mru = swap(s.mru);
  if (mru !== s.mru) (patch.mru = mru), persist("cs_mru", mru);
  const rows = s.gridRows.map(swap);
  if (rows.some((r, i) => r !== s.gridRows[i])) (patch.gridRows = rows), persist("cs_gridrows", rows);
  const rail = swap(s.railOrder);
  if (rail !== s.railOrder) patch.railOrder = rail;
  const verify = swap(s.verifyPanes);
  if (verify !== s.verifyPanes) patch.verifyPanes = verify;

  if (s.hidden.has(from)) {
    const hidden = new Set(swap([...s.hidden]));
    patch.hidden = hidden;
    persist("mf_hidden", [...hidden]);
  }
  if (s.collapsedFamilies.has(from)) {
    const fam = new Set(swap([...s.collapsedFamilies]));
    patch.collapsedFamilies = fam;
    persist("mf_familycollapse", [...fam]);
  }
  if (s.bulkSelected.has(from)) patch.bulkSelected = new Set(swap([...s.bulkSelected]));
  if (s.bulkAnchor === from) patch.bulkAnchor = to;

  // The old rename was a label over the title; the title IS the name now.
  // A label still kept for the new title is one from a dead session.
  if (from in s.aliases || to in s.aliases) {
    const aliases = { ...s.aliases };
    delete aliases[from];
    delete aliases[to];
    patch.aliases = aliases;
    persist("mf_aliases", aliases);
  }

  const lastTab = rekey(s.lastTab);
  if (lastTab !== s.lastTab) (patch.lastTab = lastTab), persist("cs_lasttab", lastTab);
  const seen = rekey(s.threadLastSeen);
  if (seen !== s.threadLastSeen) (patch.threadLastSeen = seen), persist("mf_thread_seen", seen);

  if (s.focused === from) patch.focused = to;
  if (s.dialogTarget === from) patch.dialogTarget = to;
  const ct = s.threadComposeTarget;
  if (ct && (ct.title === from || ct.to === from))
    patch.threadComposeTarget = { ...ct, title: tok(ct.title), to: tok(ct.to) };
  if (s.mapFocus?.title === from) patch.mapFocus = { ...s.mapFocus, title: to };
  if (s.diffFocus?.title === from) patch.diffFocus = { ...s.diffFocus, title: to };
  if (s.fastTrackMenu?.title === from) patch.fastTrackMenu = { ...s.fastTrackMenu, title: to };

  if (Object.keys(patch).length) useUi.setState(patch as never);

  // Outside the store.
  renameCodemapSeen(from, to);
  renameNodePos(from, to);
  const layout = loadSkyLayout();
  const moved = renameSkyLine(layout, from, to);
  if (moved !== layout) saveSkyLayout(moved);
  const oldPos = readObj(SKY_POS_KEY);
  if (oldPos && from in oldPos) persist(SKY_POS_KEY, rekey(oldPos));

  // The snapshot's row: under its new name now, rather than a beat of the
  // old one until the refetch.
  queryClient.setQueryData<Instance[]>(["instances"], (prev) => {
    if (!prev || !prev.some((r) => r.title === from || r.parent === from)) return prev;
    const hasTo = prev.some((r) => r.title === to);
    return prev
      .filter((r) => !(hasTo && r.title === from))
      .map((r) =>
        r.title === from || r.parent === from
          ? { ...r, title: r.title === from ? to : r.title, parent: r.parent === from ? to : r.parent }
          : r
      );
  });

  for (const fn of listeners) {
    try {
      fn(from, to);
    } catch {
      /* one holder's trouble is not the rename's */
    }
  }
}

/** The answer of POST /api/instances/{title}/rename. */
export interface RenameAnswer {
  title: string;
  from: string;
  unchanged?: boolean;
}

/** Rename session `title` to `name` (trimmed) — every rename surface's door
 * (the Rename dialog, the rail row's in-place edit, the Sky frame's bar).
 *
 * `plain` is what the session shows with no old rename over it (its sidebar
 * label, else its title): typing that back, or the title, while an old
 * rename (alias) is showing just drops the alias. A blank or unchanged name
 * does nothing. A refusal (400 invalid name, 404 gone, 409 taken / not
 * yours to rename) is the server's own sentence, in a toast. Resolves to the
 * session's title afterwards ("" when nothing was renamed). */
export async function renameSession(title: string, name: string, opts: { plain?: string } = {}): Promise<string> {
  const next = String(name || "").trim();
  if (!title || !next) return "";
  const ui = useUi.getState();
  if (ui.aliases[title] && (next === title || next === opts.plain)) {
    ui.setAlias(title, "");
    toast(`Renamed to “${next}”`);
    return title;
  }
  if (next === title) return "";
  try {
    const r = await instApi<RenameAnswer>(title, "/rename", { json: { to: next } });
    const now = (r && typeof r.title === "string" && r.title) || next;
    if (r?.unchanged || now === title) return "";
    moveSessionTitle(title, now);
    toast(`Renamed to “${now}”`);
    void refreshInstances();
    return now;
  } catch (err) {
    if (err instanceof ApiError && err.status >= 400 && err.status < 500) toast(err.message || `Couldn't rename it to “${next}”`);
    else errorPop("Rename failed", errMsg(err));
    return "";
  }
}
