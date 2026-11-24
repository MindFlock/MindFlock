/** Shared files (backend core/hub.py, docs/web-ui.md "Shared files"): one
 * shared place for files, pictures and snippets every session can use. An
 * ITEM has a CODE — `file:k7m2` — that works in any prompt sent through
 * MindFlock (the server expands it to the item's path, name and kind) and in
 * an agent's terminal (a paste is expanded here, through /api/hub/expand).
 * The first codes read `hub:k7m2`; they keep working everywhere.
 *
 * People read "Shared files"; the code keeps its first name, hub, wherever
 * it is stored or called (the API, hub.json, the MCP tool, these modules).
 *
 * This module is the panel's, the composers' and the rail's shared half: the
 * list query, the panel's open state and selection, every action (add, copy,
 * send, pin and remove — one item or several — with Undo) and the words they
 * say. */

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { create } from "zustand";
import { ApiError, api, apiRaw, instApi } from "../api/client";
import { queryClient } from "../state/queries";
import { displayName, useUi } from "../state/store";
import { copyText } from "./clipboard";
import { errMsg } from "./format";

export type HubKind = "image" | "text" | "file";

export interface HubItem {
  id: string;
  ref: string;
  name: string;
  kind: HubKind;
  mime: string;
  size: number;
  sha256: string;
  by: "you" | "phone" | "session" | "device";
  by_name: string;
  added: number;
  used: number;
  note: string;
  pinned: boolean;
  textual: boolean;
  preview: string;
  path: string;
  expires: number | null;
  /** Kept on this device only: never offered to the person's other devices. */
  local_only?: boolean;
  /** Fetched from another of the person's devices (a snapshot of it). */
  origin?: HubOrigin;
  /** An item ANOTHER device holds, shown in this list (read-only here; any
   * use fetches it first — bringHere). Its `id` is its device code. */
  remote?: HubRemote;
}

export interface HubOrigin {
  device: string;
  name: string;
  id: string;
}

export interface HubRemote {
  device: string;
  deviceName: string;
  /** Its id on that device. */
  id: string;
  /** Bigger than one shared file may be here. */
  tooBig: boolean;
  /** Its name looks like a secret: only the person can bring it here. */
  secret: boolean;
}

/** Why another device can't be asked right now (core/hub_remote.py). */
export type HubDeviceState = "ok" | "offline" | "old" | "off" | "private" | "not_linked" | "refused" | "error";

export interface HubDeviceRow {
  id: string;
  ref: string;
  name: string;
  kind: HubKind;
  mime: string;
  size: number;
  sha256: string;
  by: HubItem["by"];
  by_name: string;
  added: number;
  note: string;
  textual: boolean;
  preview: string;
  secret: boolean;
  too_big: boolean;
  /** The local copy's id, once fetched here. */
  copy: string | null;
}

export interface HubDevice {
  device: string;
  name: string;
  state: HubDeviceState;
  note: string;
  items: HubDeviceRow[];
  dropped: number;
  truncated: boolean;
}

export interface HubDevices {
  self: string;
  devices: HubDevice[];
}

export interface HubList {
  items: HubItem[];
  used: number;
  count: number;
  quota: number;
  max_items: number;
  item_bytes: number;
  keep_days: number;
  root: string;
}

export { HUB_MIME, REF_RE, CODE_PREFIXES, dragItems, setDragItems } from "./hubRef";
import { setDragItems } from "./hubRef";
/** How long Undo stays offered after a remove (the server keeps it 10 min). */
const UNDO_MS = 10000;

// --------------------------------------------------------------------------
// The list
// --------------------------------------------------------------------------
export function useHub(enabled = true) {
  useEffect(bridgeHubEvents, []);
  return useQuery({
    queryKey: ["hub"],
    queryFn: () => api<HubList>("/api/hub"),
    enabled,
    staleTime: 10_000,
    refetchInterval: 60_000,
  });
}

export function refreshHub(): void {
  void queryClient.invalidateQueries({ queryKey: ["hub"] });
  void queryClient.invalidateQueries({ queryKey: ["hub-devices"] });
}

/** What the person's OTHER devices offer right now (GET /api/hub/devices:
 * only the list — nothing is copied to draw it). A server without device
 * sharing answers 404: no devices. */
export function useHubDevices(enabled = true) {
  return useQuery({
    queryKey: ["hub-devices"],
    queryFn: async (): Promise<HubDevices> => {
      try {
        return await api<HubDevices>("/api/hub/devices");
      } catch (err) {
        if ((err as ApiError)?.status === 404) return { self: "", devices: [] };
        throw err;
      }
    },
    enabled,
    staleTime: 15_000,
    refetchInterval: 30_000,
    retry: false,
  });
}

/** `file@mac-mini:k7m2` — an item named with the device that holds it. */
export function deviceCode(device: string, id: string): string {
  return "file@" + device + ":" + id;
}

/** The other devices' items as rows of this list — one already fetched here
 * is left out (its local copy is the row, "from ‹device›"). */
export function remoteItems(d: HubDevices | undefined): HubItem[] {
  const out: HubItem[] = [];
  for (const dev of d?.devices || []) {
    if (dev.state !== "ok") continue;
    for (const r of dev.items) {
      if (r.copy) continue;
      out.push({
        id: deviceCode(dev.device, r.id),
        ref: r.ref,
        name: r.name,
        kind: r.kind,
        mime: r.mime,
        size: r.size,
        sha256: r.sha256,
        by: r.by,
        by_name: r.by_name,
        added: r.added,
        used: r.added,
        note: r.note,
        pinned: false,
        textual: r.textual,
        preview: r.preview,
        path: "",
        expires: null,
        remote: { device: dev.device, deviceName: dev.name, id: r.id, tooBig: r.too_big, secret: r.secret },
      });
    }
  }
  return out;
}

/** The item a code in text means, for a chip: `file:k7m2` — this device's
 * k7m2, else the one copy fetched from another device's k7m2, else the one
 * other device offering it; `file@mac-mini:k7m2` — that device's k7m2 (its
 * copy here once fetched). Undefined when none (or several) match. */
export function findByRef(ref: string, local: HubItem[], remote: HubItem[]): HubItem | undefined {
  const m = /^(?:file|hub)(?:@([A-Za-z0-9-]+))?:([A-Za-z0-9]{4,8})$/i.exec(ref.trim());
  if (!m) return undefined;
  const dev = (m[1] || "").toLowerCase();
  const id = m[2].toLowerCase();
  if (dev) {
    return (
      local.find((i) => i.origin?.device === dev && i.origin.id === id) ||
      remote.find((i) => i.remote?.device === dev && i.remote.id === id)
    );
  }
  const own = local.find((i) => i.id === id);
  if (own) return own;
  const copies = local.filter((i) => i.origin?.id === id);
  if (copies.length) return new Set(copies.map((i) => i.origin!.device)).size === 1 ? copies[0] : undefined;
  const there = remote.filter((i) => i.remote?.id === id);
  return there.length === 1 ? there[0] : undefined;
}

/** The devices that can't be asked right now, with the sentence to show
 * ("Mac mini is offline — …", "… needs an update"). */
export function deviceNotes(d: HubDevices | undefined): { device: string; name: string; state: HubDeviceState; note: string }[] {
  return (d?.devices || []).filter((x) => x.state !== "ok").map((x) => ({ device: x.device, name: x.name, state: x.state, note: x.note }));
}

/** The largest picture of another device drawn as a thumbnail (it is its
 * own thumbnail: core/hub_remote.py THUMB_MAX). */
export const REMOTE_THUMB_MAX = 512 * 1024;

/** Whether the list can draw this picture (a remote one only when small —
 * a bigger one is never pulled just to draw the list). */
export function hasThumb(it: Pick<HubItem, "kind" | "size" | "remote">): boolean {
  return it.kind === "image" && (!it.remote || it.size <= REMOTE_THUMB_MAX);
}

/** Bring items of other devices HERE before using them (one POST
 * /api/hub/batch action=fetch, answered per item): each lands in this
 * device's shared files as a normal item that remembers where it came from;
 * one already here is found, not fetched again. → the items to use, local
 * ones as they were, fetched ones as their local copies, in the order given;
 * what couldn't be fetched is said in the drawer, item by item (a secret-
 * looking one with "Bring it anyway"). */
export async function bringHere(items: HubItem[], opts: { secretOk?: boolean } = {}): Promise<HubItem[]> {
  const rem = items.filter((i) => i.remote);
  if (!rem.length) return items;
  const from = [...new Set(rem.map((i) => i.remote!.deviceName))].join(" and ");
  say((rem.length === 1 ? "Fetching " + rem[0].name : "Fetching " + rem.length + " items") + " from " + from + "…", { ms: 60_000 });
  const got = new Map<string, HubItem>();
  try {
    if (opts.secretOk) {
      for (const it of rem) {
        const rec = await api<HubItem>("/api/hub/devices/fetch", { json: { id: it.id, secret_ok: true } });
        got.set(it.id, rec);
      }
    } else {
      const r = await api<BatchResult>("/api/hub/batch", { json: { action: "fetch", ids: rem.map((i) => i.id) } });
      for (const row of r.results) {
        const it = rem.find((i) => i.id === row.id);
        if (!it) continue;
        if (row.ok && row.item) got.set(it.id, row.item);
        else if (row.code === "secret")
          addProblem(
            it.name + " on " + it.remote!.deviceName + " looks like a secret (an env file, key or credential) — it wasn't brought here. Every session here can read Shared files.",
            undefined,
            { label: "Bring it anyway", run: () => void bringHere([it], { secretOk: true }).then((l) => l.length && say("Brought " + it.name + " here as " + l[0].ref)) }
          );
        else addProblem("Couldn't fetch " + it.name + " from " + it.remote!.deviceName + ": " + (row.error || "error"));
      }
    }
  } catch (err) {
    say("Couldn't fetch from " + from + ": " + errMsg(err), { error: true });
    refreshHub();
    return [];
  }
  refreshHub();
  if (got.size === rem.length) hush();
  else if (!got.size)
    say("Couldn't fetch " + (rem.length === 1 ? rem[0].name : rem.length + " items") + " from " + from + " — why is in Shared files", { error: true });
  else say("Fetched " + got.size + " of " + rem.length + " from " + from + " — why " + (rem.length - got.size) + " couldn't be is in Shared files");
  const out: HubItem[] = [];
  for (const i of items) {
    if (!i.remote) out.push(i);
    else if (got.has(i.id)) out.push(got.get(i.id)!);
  }
  return out;
}

/** Save other devices' items on this one (the ⋯ menu's and the selection
 * bar's "Save to this device"). */
export async function saveHere(items: HubItem[]): Promise<void> {
  const rem = items.filter((i) => i.remote);
  if (!rem.length) return;
  const got = await bringHere(rem);
  if (got.length) say(got.length === 1 ? "Saved " + got[0].name + " on this device as " + got[0].ref : batchLine(got.length, rem.length, "Saved") + " on this device");
}

let bridged = false;
/** Refetch the list whenever the server says shared files changed (any device
 * tab, the CLI, an agent's put). */
export function bridgeHubEvents(): void {
  if (bridged) return;
  const ev = typeof window !== "undefined" ? window.mindflock?.events : undefined;
  if (!ev) return;
  bridged = true;
  ev.subscribe("hub.changed", () => refreshHub());
}

// --------------------------------------------------------------------------
// The panel, the Send dialog and the composer hand-off
// --------------------------------------------------------------------------
/** One line Shared files says, at the TOP of the window (never over a session's
 * prompt line, where the bottom toasts sit), with up to two actions. */
export interface HubNotice {
  text: string;
  error?: boolean;
  actions?: { label: string; run(): void }[];
  n: number;
  ms: number;
}

/** A problem shown IN the drawer (a refused add), until dismissed. */
export interface HubProblem {
  id: number;
  text: string;
  /** Too big for the per-item limit: the MB a one-click raise would set. */
  raiseTo?: number;
  /** A one-click way past it (a secret-looking item: "Bring it anyway"). */
  confirm?: { label: string; run(): void };
}

interface HubUi {
  notice: HubNotice | null;
  problems: HubProblem[];
  open: boolean;
  setOpen(v: boolean): void;
  toggle(): void;
  /** Set by openAdd() and cleared by the drawer as it opens its file picker
   * ("Add files…") — the sidebar row's Add. */
  wantAdd: boolean;
  /** Open the drawer on its list and straight into Add files… */
  openAdd(): void;
  /** The items "Send to session…" is open for: the drawer shows its send
   * step (with Back) instead of the list — one surface, never a dialog the
   * drawer could cover. */
  sending: HubItem[] | null;
  openSend(items: HubItem | HubItem[] | null): void;
  /** The ids ticked in the list (the selection bar acts on them). */
  selected: string[];
  setSelected(ids: string[]): void;
  /** Text to put into a session's Thread composer (`n` bumps per request). */
  insert: { title: string; text: string; n: number } | null;
  insertInto(title: string, text: string): void;
}

let _n = 0;
export const useHubUi = create<HubUi>((set, get) => ({
  notice: null,
  problems: [],
  open: false,
  // Closing forgets the send step and the selection: the drawer opens on
  // the list, nothing ticked, every time.
  setOpen: (v) => {
    if (v) markHubSeen();
    set(v ? { open: true } : { open: false, sending: null, selected: [], wantAdd: false });
  },
  toggle: () => get().setOpen(!get().open),
  wantAdd: false,
  openAdd: () => {
    if (!get().open) markHubSeen();
    set({ open: true, sending: null, wantAdd: true });
  },
  sending: null,
  // Opening Send to session… retires a "Put in X's prompt" notice: two
  // live ways to send the same item is how it gets sent twice. It opens the
  // drawer too (the step lives there).
  openSend: (items) => {
    const list = items ? (Array.isArray(items) ? items : [items]) : [];
    if (!list.length) return set({ sending: null });
    if (!get().open) markHubSeen();
    set({ sending: list, notice: null, open: true });
  },
  selected: [],
  setSelected: (ids) => set({ selected: ids }),
  insert: null,
  insertInto: (title, text) => set({ insert: { title, text, n: ++_n } }),
}));

/** Say one thing at the top of the window (replaces what was there). */
export function say(text: string, opts: { error?: boolean; actions?: HubNotice["actions"]; ms?: number } = {}): void {
  const ms = opts.ms ?? (opts.actions?.length ? 9000 : Math.max(2500, Math.min(7000, 1200 + text.length * 45)));
  useHubUi.setState({ notice: { text, error: !!opts.error, actions: opts.actions, n: ++_n, ms } });
}

export function hush(): void {
  useHubUi.setState({ notice: null });
}

let _p = 0;
export function addProblem(text: string, raiseTo?: number, confirm?: HubProblem["confirm"]): void {
  useHubUi.setState((s) => ({ problems: [...s.problems.slice(-3), { id: ++_p, text, raiseTo, confirm }] }));
}

export function dropProblem(id: number): void {
  useHubUi.setState((s) => ({ problems: s.problems.filter((p) => p.id !== id) }));
}

/** Briefly ring the session a shared file just went to — its pane and its rail
 * row — so you see where it landed. */
export function flashTarget(title: string): void {
  if (typeof document === "undefined") return;
  const esc = typeof CSS !== "undefined" && CSS.escape ? CSS.escape(title) : title.replace(/["\\]/g, "\\$&");
  const sel = '[data-title="' + esc + '"]';
  for (const el of Array.from(document.querySelectorAll<HTMLElement>(sel))) {
    el.classList.remove("hub-flash");
    void el.offsetWidth;
    el.classList.add("hub-flash");
    setTimeout(() => el.classList.remove("hub-flash"), 1800);
  }
}

// --------------------------------------------------------------------------
// What's new since you last looked (the sidebar row's count)
// --------------------------------------------------------------------------
const SEEN_KEY = "mf_hub_seen";

export function hubSeen(): number {
  try {
    return Number(localStorage.getItem(SEEN_KEY) || 0) || 0;
  } catch {
    return 0;
  }
}

export function markHubSeen(now = Date.now() / 1000): void {
  try {
    localStorage.setItem(SEEN_KEY, String(now));
  } catch {
    /* private mode: the count just stays */
  }
}

/** Items an AGENT or your PHONE added since you last opened Shared files — the
 * ones you haven't seen. What you added yourself is never "new". */
export function newCount(items: Pick<HubItem, "by" | "added">[], seen = hubSeen()): number {
  return items.filter((i) => i.by !== "you" && i.added > seen).length;
}

/** A session's state in a word, for the Send dialog's list. */
export function stateWord(activity: string | undefined): string {
  const a = String(activity || "");
  if (a === "working") return "Working";
  if (a === "clarify" || a === "ask" || a === "question") return "Question";
  if (a === "limit") return "Waiting on limit";
  if (a === "offline") return "Offline";
  if (a === "starting" || a === "loading") return "Starting";
  return "Idle";
}

// --------------------------------------------------------------------------
// Words
// --------------------------------------------------------------------------
export function humanSize(n: number): string {
  if (!(n > 0)) return "0 bytes";
  if (n < 1024) return n + (n === 1 ? " byte" : " bytes");
  if (n < 1024 * 1024) return (n / 1024).toFixed(n < 10240 ? 1 : 0) + " KB";
  if (n < 1024 ** 3) return (n / 1024 ** 2).toFixed(1) + " MB";
  return (n / 1024 ** 3).toFixed(1) + " GB";
}

/** Who added it, as the panel says it: "you", "your phone", "session api". */
export function whoLabel(it: Pick<HubItem, "by" | "by_name"> & { origin?: HubOrigin; remote?: HubRemote }): string {
  if (it.origin) return "from " + (it.origin.name || it.origin.device);
  if (it.by === "session" && it.remote) return "session " + (it.by_name || "?");
  if (it.by === "session") return "session " + displayName(it.by_name || "?");
  if (it.by === "device") return "device " + (it.by_name || "?");
  if (it.by === "phone") return "your phone";
  return "you";
}

export function kindLabel(it: Pick<HubItem, "kind" | "name">): string {
  if (it.kind === "image") return "Picture";
  if (it.kind === "text") return "Snippet";
  const ext = (it.name.match(/\.([A-Za-z0-9]{1,6})$/) || [])[1];
  return ext ? ext.toUpperCase() + " file" : "File";
}

/** "expires in 9 days" / "pinned" — when an unpinned item goes. */
export function expiryLabel(it: Pick<HubItem, "pinned" | "expires">, now = Date.now() / 1000): string {
  if (it.pinned || !it.expires) return "Pinned — kept until you remove it";
  const d = Math.max(0, (it.expires - now) / 86400);
  if (d < 1) return "Deleted automatically today unless used — Pin to keep";
  return "Deleted automatically in " + Math.ceil(d) + (Math.ceil(d) === 1 ? " day" : " days") + " unless used — Pin to keep";
}

export function rawUrl(it: Pick<HubItem, "id"> & { remote?: HubRemote }, download = false): string {
  if (it.remote)
    // Another device's picture: only its thumbnail (checked here) — never
    // its whole file just to draw it.
    return "/api/hub/devices/" + encodeURIComponent(it.remote.device) + "/items/" + encodeURIComponent(it.remote.id) + "/thumb";
  return "/api/hub/items/" + encodeURIComponent(it.id) + "/raw" + (download ? "?download=1" : "");
}

/** The free space line: "3 items · 1.2 MB of 1.0 GB". */
export function usageLine(l: Pick<HubList, "count" | "used" | "quota">): string {
  return (
    l.count + (l.count === 1 ? " item" : " items") + " · " + humanSize(l.used) + " of " + humanSize(l.quota)
  );
}

/** The code prefix the composer picker answers — `file:` (or the first
 * codes' `hub:`) — at the token the caret is in, or null. The prefix alone
 * (or with the first letters) counts; "file: " with a space after it is
 * prose, not a code, so the picker closes. → where the token starts, the
 * prefix as typed and what follows it. */
export function refTokenAt(text: string, caret: number): { start: number; prefix: string; query: string } | null {
  const before = text.slice(0, caret);
  const m = before.match(/(^|[\s([{"'`])(file:|hub:)((?:[A-Za-z0-9._-][A-Za-z0-9._ -]{0,39})?)$/i);
  if (!m) return null;
  const prefix = m[2];
  const query = m[3];
  if (/\s\s/.test(query) || query.length > 40) return null;
  return { start: caret - query.length - prefix.length, prefix, query };
}

/** Items matching what was typed after the prefix — by id prefix first, then
 * by name. */
export function matchItems(items: HubItem[], query: string, most = 8): HubItem[] {
  const q = query.trim().toLowerCase();
  if (!q) return items.slice(0, most);
  const byId = items.filter((i) => i.id.startsWith(q));
  const byName = items.filter((i) => !byId.includes(i) && i.name.toLowerCase().includes(q));
  return [...byId, ...byName].slice(0, most);
}

// --------------------------------------------------------------------------
// Adding
// --------------------------------------------------------------------------
export interface AddResult {
  added: HubItem[];
  /** Refused files, each with the server's sentence; `secret` ones may be
   * added anyway (the owner confirms in the panel). */
  refused: { file: File; error: string; secret: boolean; raiseTo?: number }[];
}

export async function addFile(file: File, opts: { source?: "you" | "phone"; secretOk?: boolean; note?: string } = {}): Promise<HubItem> {
  const q = new URLSearchParams({ name: file.name || "pasted", source: opts.source || "you" });
  if (opts.secretOk) q.set("secret_ok", "1");
  if (opts.note) q.set("note", opts.note);
  const r = await apiRaw("/api/hub/items?" + q.toString(), {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: file,
  });
  const body = (await r.json().catch(() => null)) as (HubItem & { error?: string; code?: string; can_confirm?: boolean }) | null;
  if (!r.ok) {
    throw new ApiError(r.status, (body && body.error) || "Couldn't add " + file.name, body);
  }
  return body as HubItem;
}

/** Upload files one by one; never throws. A pasted screenshot has no useful
 * name ("image.png"): it is named by the time it was pasted. */
export async function addFiles(files: File[], opts: { source?: "you" | "phone"; secretOk?: boolean } = {}): Promise<AddResult> {
  const out: AddResult = { added: [], refused: [] };
  for (const raw of files) {
    const f = renamePasted(raw);
    try {
      out.added.push(await addFile(f, opts));
    } catch (err) {
      const body = (err as ApiError)?.body as { code?: string } | null;
      // Too big for the per-item limit: offer the next round size up (MB).
      const raiseTo =
        body?.code === "too_large" && f.size > 0 ? Math.min(65536, Math.ceil(f.size / (1024 * 1024) / 50) * 50 + 50) : undefined;
      out.refused.push({ file: f, error: errMsg(err), secret: body?.code === "secret", raiseTo });
    }
  }
  if (out.added.length) refreshHub();
  return out;
}

/** "image.png" from a clipboard → "screenshot-14-32-05.png". */
export function renamePasted(f: File, now = new Date()): File {
  if (!/^image\.(png|jpe?g|gif|webp)$/i.test(f.name || "") && f.name) return f;
  const ext = (f.type.split("/")[1] || "png").replace("jpeg", "jpg");
  const hh = (n: number) => String(n).padStart(2, "0");
  // No spaces: the path an agent is handed needs no quoting.
  const name =
    "screenshot-" + hh(now.getHours()) + "-" + hh(now.getMinutes()) + "-" + hh(now.getSeconds()) + "." + ext;
  try {
    return new File([f], name, { type: f.type });
  } catch {
    return f;
  }
}

export async function addText(text: string, name = ""): Promise<HubItem> {
  const it = await api<HubItem>("/api/hub/text", { json: { text, name } });
  refreshHub();
  return it;
}

/** What the panel says after an add: one item names its code (copied to
 * the clipboard) and, when a session is focused, offers to put it straight
 * into that session's prompt — paste a screenshot, one click, done. A
 * refused add is shown IN the drawer (addProblem), never only in passing. */
export function addedToast(r: AddResult): void {
  if (r.added.length === 1) {
    const it = r.added[0];
    void copyText(it.ref);
    const focused = useUi.getState().focused;
    const here = focused && !focused.includes("::") ? focused : "";
    say(
      "Added " + it.name + " — code " + it.ref + " copied to clipboard",
      here
        ? { actions: [{ label: "Put in " + displayName(here) + "'s prompt", run: () => void putInPrompt(here, it, "", { focus: true }) }] }
        : {}
    );
  } else if (r.added.length > 1) say("Added " + r.added.length + " items to Shared files");
  for (const x of r.refused.filter((x) => !x.secret)) {
    addProblem("Couldn't add " + x.file.name + ": " + x.error, x.raiseTo);
  }
}

/** Raise the per-item limit (the owner's one click on a too-big refusal). */
export async function raiseItemLimit(mb: number): Promise<boolean> {
  try {
    await api("/api/settings", { json: { hub: { item_mb: mb } } });
    refreshHub();
    say("Each item can now be up to " + mb + " MB — add the file again");
    return true;
  } catch (err) {
    say("Couldn't raise the limit: " + errMsg(err), { error: true });
    return false;
  }
}

/** Rebuild the list of shared files from the folders on disk (hub_broken). */
export async function rebuildHub(): Promise<void> {
  try {
    const r = await api<{ items: number; restored: number; read_again: number }>("/api/hub/rebuild", { method: "POST" });
    refreshHub();
    say(
      "Rebuilt the list: " + r.items + (r.items === 1 ? " item" : " items") + " — nothing was lost" +
        (r.read_again
          ? ". " + r.read_again + " read again " + (r.read_again === 1 ? "is" : "are") + " kept on this device only — ⋯ → Show on my other devices to share one again"
          : "")
    );
  } catch (err) {
    say("Couldn't rebuild the list: " + errMsg(err), { error: true });
  }
}

// --------------------------------------------------------------------------
// Using
// --------------------------------------------------------------------------
export async function copyRef(it: HubItem): Promise<void> {
  const ok = await copyText(it.ref);
  say(ok ? "Code copied to clipboard — paste " + it.ref + " into any prompt or terminal" : "Couldn't copy — the code is " + it.ref, {
    error: !ok,
  });
}

export async function copyPath(it: HubItem): Promise<void> {
  const ok = await copyText(it.path);
  say(ok ? "Path of " + it.name + " copied to clipboard" : "Couldn't copy — the path is " + it.path, { error: !ok });
}

export async function copyItemText(it: HubItem): Promise<void> {
  try {
    const r = await api<{ text: string; truncated: boolean }>(
      "/api/hub/items/" + encodeURIComponent(it.id) + "/text"
    );
    const ok = await copyText(r.text);
    say(ok ? (r.truncated ? "The first 1 MB of " : "The text of ") + it.name + " copied to clipboard" : "Couldn't copy the text", {
      error: !ok,
    });
  } catch (err) {
    say("Couldn't copy the text: " + errMsg(err), { error: true });
  }
}

/** Whether this browser can put an image on the clipboard. */
export function canCopyImage(): boolean {
  return (
    typeof navigator !== "undefined" &&
    !!navigator.clipboard?.write &&
    typeof (globalThis as { ClipboardItem?: unknown }).ClipboardItem === "function"
  );
}

export async function copyImage(it: HubItem): Promise<void> {
  try {
    const r = await apiRaw(rawUrl(it));
    if (!r.ok) throw new Error("the image could not be read");
    let blob = await r.blob();
    if (blob.type !== "image/png") blob = await toPng(blob);
    const Item = (globalThis as unknown as { ClipboardItem: new (x: Record<string, Blob>) => ClipboardItem }).ClipboardItem;
    await navigator.clipboard.write([new Item({ "image/png": blob })]);
    say("The image " + it.name + " copied to clipboard");
  } catch (err) {
    say("Couldn't copy the image here (" + errMsg(err) + ") — use Download, or Copy code", { error: true });
  }
}

async function toPng(blob: Blob): Promise<Blob> {
  const bmp = await createImageBitmap(blob);
  const c = document.createElement("canvas");
  c.width = bmp.width;
  c.height = bmp.height;
  c.getContext("2d")!.drawImage(bmp, 0, 0);
  return await new Promise<Blob>((res, rej) => c.toBlob((b) => (b ? res(b) : rej(new Error("no PNG"))), "image/png"));
}

export function download(it: HubItem): void {
  const a = document.createElement("a");
  a.href = rawUrl(it, true);
  a.download = it.name;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** Open a session's Queue tab (a queued send's "Open queue"). */
export async function openQueue(title: string): Promise<void> {
  useUi.getState().setLastTab(title, "queue");
  const { selectSession } = await import("./sessionActions");
  selectSession(title);
}

/** The codes of several items, as one line ("file:k7m2 file:zz11"). */
export function codesOf(items: Pick<HubItem, "ref">[]): string {
  return items.map((i) => i.ref).join(" ");
}

/** What a sentence calls one item or several: its name, or "3 items". */
export function itemsWord(items: Pick<HubItem, "name">[]): string {
  return items.length === 1 ? items[0].name : items.length + " items";
}

/** Point a session's agent at one item or several: their codes are typed
 * into the agent's prompt WITHOUT pressing Enter (the server expands each to
 * its path, name and kind) — the person adds their words there. With
 * `message`, ONE message is sent: "<message> <codes>" (queued when the
 * session is busy: "Open queue"). A session on a dialog gets it in its
 * Thread box instead. `focus`: close the drawer and bring that session
 * forward (the person types next). */
export async function putInPrompt(
  title: string,
  its: Pick<HubItem, "ref" | "name"> | Pick<HubItem, "ref" | "name">[],
  message = "",
  opts: { focus?: boolean } = {}
): Promise<boolean> {
  const list = Array.isArray(its) ? its : [its];
  if (!list.length) return false;
  const codes = codesOf(list);
  const msg = message.trim();
  const text = msg ? msg + " " + codes : codes + " ";
  const name = displayName(title);
  const what = list.length === 1 ? list[0].ref : list.length + " codes";
  try {
    const r = await instApi<{ queued?: boolean; hub_note?: string }>(title, "/send", {
      json: msg ? { text, dialog_safe: true } : { text, submit: false, dialog_safe: true },
    });
    const note = r?.hub_note ? " (" + r.hub_note + ")" : "";
    if (msg && r?.queued)
      say(name + " is busy — your message with " + what + " is queued; it gets it when it's free" + note, {
        actions: [{ label: "Open queue", run: () => void openQueue(title) }],
      });
    else if (msg) say("Sent to " + name + " with " + what + note);
    else say(what + (list.length === 1 ? " is" : " are") + " in " + name + "'s prompt — add your words there and press Enter" + note);
    if (opts.focus) {
      // The person types next, in that session: the drawer would cover it.
      useHubUi.getState().setOpen(false);
      const { selectSession } = await import("./sessionActions");
      selectSession(title);
    }
    flashTarget(title);
    return true;
  } catch (err) {
    if ((err as ApiError)?.status === 409) {
      useHubUi.getState().insertInto(title, text);
      useUi.getState().threadOpen(title);
      if (opts.focus) useHubUi.getState().setOpen(false);
      flashTarget(title);
      say(name + " is answering a question — " + what + (list.length === 1 ? " is" : " are") + " in its Thread box; send it from there");
      return true;
    }
    say("Couldn't send " + what + " to " + name + ": " + errMsg(err), { error: true });
    return false;
  }
}

export interface TakeResult {
  path: string;
  abs: string;
  replaced: boolean;
}

/** Save a copy of an item in a session's project folder (the owner's copy:
 * any path there except files agents follow or that run by themselves).
 * `tell`: then tell that session's agent where it is. */
export async function copyInto(
  title: string,
  it: HubItem,
  path: string,
  replace: boolean,
  tell = false
): Promise<TakeResult> {
  const r = await api<TakeResult>("/api/hub/items/" + encodeURIComponent(it.id) + "/copy-to", {
    json: { session: title, path, replace },
  });
  const name = displayName(title);
  let told = false;
  if (tell) {
    try {
      await instApi(title, "/send", {
        json: {
          text: "I saved " + it.name + " in your project folder at " + r.path + " (from MindFlock's Shared files; you can read it there).",
          dialog_safe: true,
        },
      });
      told = true;
    } catch {
      told = false;
    }
  }
  flashTarget(title);
  say(
    (r.replaced ? "Replaced " + r.path + " in " + name + "'s folder" : "Saved a copy at " + r.path + " in " + name + "'s folder") +
      (tell ? (told ? " — and told " + name : " — couldn't tell " + name + " (it may be offline)") : "")
  );
  return r;
}

export async function freePath(title: string, it: HubItem, path = ""): Promise<{ path: string; wanted: string; taken: boolean }> {
  return api(
    "/api/hub/items/" +
      encodeURIComponent(it.id) +
      "/free?session=" +
      encodeURIComponent(title) +
      (path ? "&path=" + encodeURIComponent(path) : "")
  );
}

export async function updateItem(it: HubItem, patch: Partial<Pick<HubItem, "name" | "note" | "pinned" | "local_only">>): Promise<HubItem | null> {
  try {
    const r = await api<HubItem>("/api/hub/items/" + encodeURIComponent(it.id), { method: "PATCH", json: patch });
    refreshHub();
    return r;
  } catch (err) {
    say("Couldn't change " + it.name + ": " + errMsg(err), { error: true });
    return null;
  }
}

export async function removeItem(it: HubItem): Promise<void> {
  try {
    await api("/api/hub/items/" + encodeURIComponent(it.id), { method: "DELETE" });
  } catch (err) {
    say("Couldn't remove " + it.name + ": " + errMsg(err), { error: true });
    return;
  }
  refreshHub();
  say("Removed " + it.name + " from Shared files", {
    actions: [
      {
        label: "Undo",
        run: () => {
          void api("/api/hub/items/" + encodeURIComponent(it.id) + "/restore", { method: "POST" })
            .then(() => {
              refreshHub();
              say("Put " + it.name + " back");
            })
            .catch((err) => say("Couldn't put it back: " + errMsg(err), { error: true }));
        },
      },
    ],
    ms: UNDO_MS,
  });
}

// --------------------------------------------------------------------------
// Several at once (the selection bar): one request, answered per item
// --------------------------------------------------------------------------
export type BatchAction = "pin" | "unpin" | "remove" | "restore" | "copy" | "fetch";

export interface BatchRow {
  id: string;
  ref?: string;
  ok: boolean;
  error?: string;
  code?: string;
  path?: string;
  wanted?: string;
  item?: HubItem;
}

export interface BatchResult {
  action: BatchAction;
  results: BatchRow[];
  done: number;
  failed: number;
}

/** POST /api/hub/batch: the server checks and does each item on its own and
 * answers per item — never all-or-nothing, never a silent skip. */
export async function batch(action: BatchAction, items: Pick<HubItem, "id">[], session?: string): Promise<BatchResult> {
  const r = await api<BatchResult>("/api/hub/batch", {
    json: { action, ids: items.map((i) => i.id), ...(session ? { session } : {}) },
  });
  refreshHub();
  return r;
}

/** The items a batch could not do, said IN the drawer (one problem per item,
 * with the server's reason) — a partial batch is never only a passing line. */
export function batchProblems(r: BatchResult, items: Pick<HubItem, "id" | "name">[], verb: string): string[] {
  const out: string[] = [];
  for (const row of r.results.filter((x) => !x.ok)) {
    const it = items.find((i) => i.id === row.id);
    const line = "Couldn't " + verb + " " + (it ? it.name : row.ref || row.id) + ": " + (row.error || "error");
    out.push(line);
    addProblem(line);
  }
  return out;
}

/** "Pinned 3 items" / "Pinned 2 of 3 — why 1 couldn't be is in Shared files". */
export function batchLine(done: number, asked: number, past: string): string {
  if (done === asked) return past + " " + (asked === 1 ? "1 item" : asked + " items");
  return past + " " + done + " of " + asked + " — why " + (asked - done) + " couldn't be is in Shared files";
}

/** Pin or unpin several. */
export async function pinItems(items: HubItem[], pinned: boolean): Promise<BatchResult | null> {
  if (!items.length) return null;
  try {
    const r = await batch(pinned ? "pin" : "unpin", items);
    batchProblems(r, items, pinned ? "pin" : "unpin");
    say(batchLine(r.done, items.length, pinned ? "Pinned" : "Unpinned"), { error: r.failed > 0 && r.done === 0 });
    return r;
  } catch (err) {
    say("Couldn't " + (pinned ? "pin" : "unpin") + " them: " + errMsg(err), { error: true });
    return null;
  }
}

/** Remove several, with one Undo that puts every one of them back. */
export async function removeItems(items: HubItem[]): Promise<BatchResult | null> {
  if (!items.length) return null;
  let r: BatchResult;
  try {
    r = await batch("remove", items);
  } catch (err) {
    say("Couldn't remove them: " + errMsg(err), { error: true });
    return null;
  }
  batchProblems(r, items, "remove");
  const gone = items.filter((i) => r.results.some((x) => x.id === i.id && x.ok));
  if (!gone.length) {
    say("Nothing was removed", { error: true });
    return r;
  }
  say(batchLine(gone.length, items.length, "Removed") + " from Shared files", {
    actions: [
      {
        label: "Undo",
        run: () => {
          void batch("restore", gone)
            .then((back) => {
              batchProblems(back, gone, "put back");
              say(batchLine(back.done, gone.length, "Put back"), { error: back.done === 0 });
            })
            .catch((err) => say("Couldn't put them back: " + errMsg(err), { error: true }));
        },
      },
    ],
    ms: UNDO_MS,
  });
  return r;
}

/** Copy the codes of several items, one line ("file:k7m2 file:zz11"). */
export async function copyRefs(items: HubItem[]): Promise<void> {
  if (items.length === 1) return copyRef(items[0]);
  const codes = codesOf(items);
  const ok = await copyText(codes);
  say(ok ? items.length + " codes copied to clipboard — paste them into any prompt or terminal" : "Couldn't copy — the codes are " + codes, {
    error: !ok,
  });
}

/** Save a copy of several items in a session's project folder, each at
 * hub/<name> or the next free path beside it (never over a file), then —
 * with `tell` — ONE line telling that session's agent where they all are. */
export async function copyIntoMany(title: string, items: HubItem[], tell: boolean): Promise<BatchResult> {
  const r = await batch("copy", items, title);
  const name = displayName(title);
  batchProblems(r, items, "copy");
  const saved = r.results.filter((x) => x.ok && x.path);
  let told = false;
  if (tell && saved.length) {
    const where = saved
      .map((x) => {
        const it = items.find((i) => i.id === x.id);
        return (it ? it.name + " at " : "") + x.path;
      })
      .join(", ");
    try {
      await instApi(title, "/send", {
        json: {
          text: "I saved " + (saved.length === 1 ? "a file" : saved.length + " files") + " in your project folder: " + where + " (from MindFlock's Shared files; you can read them there).",
          dialog_safe: true,
        },
      });
      told = true;
    } catch {
      told = false;
    }
  }
  if (saved.length) flashTarget(title);
  say(
    batchLine(saved.length, items.length, "Saved copies of") +
      " in " + name + "'s folder" +
      (tell && saved.length ? (told ? " — and told " + name : " — couldn't tell " + name + " (it may be offline)") : ""),
    { error: !saved.length }
  );
  return r;
}

/** A drag of one card: the whole selection when that card is in it. */
export function dragSet(it: HubItem, all: HubItem[], selected: string[]): HubItem[] {
  if (!selected.includes(it.id)) return [it];
  const sel = all.filter((i) => selected.includes(i.id));
  return sel.length ? sel : [it];
}

/** Put a drag's items on the dataTransfer (see hubRef.setDragItems). */
export function startDrag(dt: DataTransfer, items: HubItem[]): void {
  setDragItems(dt, items);
}
