/** The hub (backend core/hub.py, docs/web-ui.md "Hub"): one shared place
 * for files, pictures and snippets every session can use. An ITEM has a
 * REFERENCE — `hub:k7m2` — that works in any prompt sent through MindFlock
 * (the server expands it to the item's path, name and kind) and in an agent's
 * terminal (a paste is expanded here, through /api/hub/expand).
 *
 * This module is the panel's, the composers' and the rail's shared half: the
 * list query, the panel's open state, every action (add, copy, send,
 * remove with Undo) and the words they say. */

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

export { HUB_MIME, REF_RE } from "./hubRef";
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
}

let bridged = false;
/** Refetch the list whenever the server says the hub changed (any device
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
/** One line the hub says, at the TOP of the window (never over a session's
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
}

interface HubUi {
  notice: HubNotice | null;
  problems: HubProblem[];
  open: boolean;
  setOpen(v: boolean): void;
  toggle(): void;
  /** The item "Send to session…" is open for. */
  sending: HubItem | null;
  openSend(item: HubItem | null): void;
  /** Text to put into a session's Thread composer (`n` bumps per request). */
  insert: { title: string; text: string; n: number } | null;
  insertInto(title: string, text: string): void;
}

let _n = 0;
export const useHubUi = create<HubUi>((set, get) => ({
  notice: null,
  problems: [],
  open: false,
  setOpen: (v) => {
    if (v) markHubSeen();
    set({ open: v });
  },
  toggle: () => {
    if (!get().open) markHubSeen();
    set({ open: !get().open });
  },
  sending: null,
  // Opening Send to session… retires a "Put in X's prompt" notice: two
  // live ways to send the same item is how it gets sent twice.
  openSend: (item) => set(item ? { sending: item, notice: null } : { sending: null }),
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
export function addProblem(text: string, raiseTo?: number): void {
  useHubUi.setState((s) => ({ problems: [...s.problems.slice(-3), { id: ++_p, text, raiseTo }] }));
}

export function dropProblem(id: number): void {
  useHubUi.setState((s) => ({ problems: s.problems.filter((p) => p.id !== id) }));
}

/** Briefly ring the session a hub item just went to — its pane and its rail
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
// What's new since you last looked (the top-bar count)
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

/** Items an AGENT or your PHONE added since you last opened the hub — the
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
export function whoLabel(it: Pick<HubItem, "by" | "by_name">): string {
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

export function rawUrl(it: Pick<HubItem, "id">, download = false): string {
  return "/api/hub/items/" + encodeURIComponent(it.id) + "/raw" + (download ? "?download=1" : "");
}

/** The free space line: "3 items · 1.2 MB of 1.0 GB". */
export function usageLine(l: Pick<HubList, "count" | "used" | "quota">): string {
  return (
    l.count + (l.count === 1 ? " item" : " items") + " · " + humanSize(l.used) + " of " + humanSize(l.quota)
  );
}

/** The hub: prefix the composer picker answers: the token the caret is in,
 * or null. `hub:` alone (or with the first letters) counts. */
export function refTokenAt(text: string, caret: number): { start: number; query: string } | null {
  const before = text.slice(0, caret);
  const m = before.match(/(^|[\s([{"'`])hub:([A-Za-z0-9._ -]{0,40})$/i);
  if (!m) return null;
  const query = m[2];
  if (/\s\s/.test(query) || query.length > 40) return null;
  return { start: caret - query.length - 4, query };
}

/** Items matching what was typed after `hub:` — by id prefix first, then by
 * name. */
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
  } else if (r.added.length > 1) say("Added " + r.added.length + " items to the hub");
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

/** Rebuild the hub's list from the folders on disk (hub_broken). */
export async function rebuildHub(): Promise<void> {
  try {
    const r = await api<{ items: number; restored: number; read_again: number }>("/api/hub/rebuild", { method: "POST" });
    refreshHub();
    say("Rebuilt the hub: " + r.items + (r.items === 1 ? " item" : " items") + " — nothing was lost");
  } catch (err) {
    say("Couldn't rebuild the hub: " + errMsg(err), { error: true });
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

/** Point a session's agent at an item: its code is typed into the agent's
 * prompt WITHOUT pressing Enter (the server expands it to the path, name and
 * kind) — the person adds their words there. With `message`, it is sent:
 * "<message> <code>" (queued when the session is busy: "Open queue"). A
 * session on a dialog gets it in its Thread box instead. `focus`: close the
 * drawer and bring that session forward (the person types next). */
export async function putInPrompt(
  title: string,
  it: Pick<HubItem, "ref" | "name">,
  message = "",
  opts: { focus?: boolean } = {}
): Promise<boolean> {
  const msg = message.trim();
  const text = msg ? msg + " " + it.ref : it.ref + " ";
  const name = displayName(title);
  try {
    const r = await instApi<{ queued?: boolean }>(title, "/send", {
      json: msg ? { text, dialog_safe: true } : { text, submit: false, dialog_safe: true },
    });
    if (msg && r?.queued)
      say(name + " is busy — your message with " + it.ref + " is queued; it gets it when it's free", {
        actions: [{ label: "Open queue", run: () => void openQueue(title) }],
      });
    else if (msg) say("Sent to " + name + " with " + it.ref);
    else say(it.ref + " is in " + name + "'s prompt — add your words there and press Enter");
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
      say(name + " is answering a question — " + it.ref + " is in its Thread box; send it from there");
      return true;
    }
    say("Couldn't send " + it.ref + " to " + name + ": " + errMsg(err), { error: true });
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
          text: "I saved " + it.name + " in your project folder at " + r.path + " (from the MindFlock hub; you can read it there).",
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

export async function updateItem(it: HubItem, patch: Partial<Pick<HubItem, "name" | "note" | "pinned">>): Promise<HubItem | null> {
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
  say("Removed " + it.name + " from the hub", {
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

