/** The hub (backend core/hub.py, docs/web-ui.md "Hub"): one shared place
 * for files, images and text snippets every session can use. An ITEM has a
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
import { toast } from "./toast";

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
interface HubUi {
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
  open: false,
  setOpen: (v) => set({ open: v }),
  toggle: () => set({ open: !get().open }),
  sending: null,
  openSend: (item) => set({ sending: item }),
  insert: null,
  insertInto: (title, text) => set({ insert: { title, text, n: ++_n } }),
}));

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
  if (it.kind === "image") return "Image";
  if (it.kind === "text") return "Text";
  const ext = (it.name.match(/\.([A-Za-z0-9]{1,6})$/) || [])[1];
  return ext ? ext.toUpperCase() + " file" : "File";
}

/** "expires in 9 days" / "pinned" — when an unpinned item goes. */
export function expiryLabel(it: Pick<HubItem, "pinned" | "expires">, now = Date.now() / 1000): string {
  if (it.pinned || !it.expires) return "pinned — kept";
  const d = Math.max(0, (it.expires - now) / 86400);
  if (d < 1) return "goes today unless used";
  return "goes in " + Math.ceil(d) + (Math.ceil(d) === 1 ? " day" : " days") + " unless used";
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
  refused: { file: File; error: string; secret: boolean }[];
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
      out.refused.push({ file: f, error: errMsg(err), secret: body?.code === "secret" });
    }
  }
  if (out.added.length) refreshHub();
  return out;
}

/** "image.png" from a clipboard → "screenshot 14-32-05.png". */
export function renamePasted(f: File, now = new Date()): File {
  if (!/^image\.(png|jpe?g|gif|webp)$/i.test(f.name || "") && f.name) return f;
  const ext = (f.type.split("/")[1] || "png").replace("jpeg", "jpg");
  const hh = (n: number) => String(n).padStart(2, "0");
  const name =
    "screenshot " + hh(now.getHours()) + "-" + hh(now.getMinutes()) + "-" + hh(now.getSeconds()) + "." + ext;
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

/** What the panel says after an add: one item names its reference. */
export function addedToast(r: AddResult): void {
  if (r.added.length === 1) {
    toast("Added " + r.added[0].name + " — reference " + r.added[0].ref + " (copied)");
    void copyText(r.added[0].ref);
  } else if (r.added.length > 1) toast("Added " + r.added.length + " items to the hub");
  const plain = r.refused.filter((x) => !x.secret);
  if (plain.length) toast("Couldn't add " + plain.map((x) => x.file.name + ": " + x.error).join("; "), { error: true });
}

// --------------------------------------------------------------------------
// Using
// --------------------------------------------------------------------------
export async function copyRef(it: HubItem): Promise<void> {
  const ok = await copyText(it.ref);
  toast(ok ? "Copied " + it.ref + " — paste it into any prompt or terminal" : "Couldn't copy — the reference is " + it.ref);
}

export async function copyPath(it: HubItem): Promise<void> {
  const ok = await copyText(it.path);
  toast(ok ? "Copied the path of " + it.name : "Couldn't copy — the path is " + it.path);
}

export async function copyItemText(it: HubItem): Promise<void> {
  try {
    const r = await api<{ text: string; truncated: boolean }>(
      "/api/hub/items/" + encodeURIComponent(it.id) + "/text"
    );
    const ok = await copyText(r.text);
    toast(ok ? (r.truncated ? "Copied the first 1 MB of " : "Copied the text of ") + it.name : "Couldn't copy the text");
  } catch (err) {
    toast("Couldn't copy the text: " + errMsg(err));
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
    toast("Copied the image " + it.name);
  } catch (err) {
    toast("Couldn't copy the image here (" + errMsg(err) + ") — use Download, or Copy reference");
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

/** Type the reference into the session's agent prompt WITHOUT pressing
 * Enter (the server expands it to the path, name and kind): the person adds
 * their words there. With `message`, it is sent: "<message> <ref>". A session
 * on a dialog gets it in its Thread box instead. */
export async function putInPrompt(title: string, it: Pick<HubItem, "ref" | "name">, message = ""): Promise<boolean> {
  const msg = message.trim();
  const text = msg ? msg + " " + it.ref : it.ref + " ";
  try {
    const r = await instApi<{ queued?: boolean }>(title, "/send", {
      json: msg ? { text, dialog_safe: true } : { text, submit: false, dialog_safe: true },
    });
    if (msg)
      toast((r?.queued ? "Queued for " : "Sent to ") + displayName(title) + " with " + it.ref);
    else toast(it.ref + " is in " + displayName(title) + "'s prompt — add your words there and press Enter");
    return true;
  } catch (err) {
    if ((err as ApiError)?.status === 409) {
      useHubUi.getState().insertInto(title, text);
      useUi.getState().threadOpen(title);
      toast(displayName(title) + " is busy with a prompt — " + it.ref + " is in its Thread box; send it from there");
      return true;
    }
    toast("Couldn't send " + it.ref + " to " + displayName(title) + ": " + errMsg(err));
    return false;
  }
}

export interface TakeResult {
  path: string;
  abs: string;
  replaced: boolean;
}

export async function copyInto(title: string, it: HubItem, path: string, replace: boolean): Promise<TakeResult> {
  const r = await instApi<TakeResult>(title, "/hub/take", { json: { id: it.id, path, replace } });
  toast(
    (r.replaced ? "Replaced " : "Copied " + it.name + " into ") +
      displayName(title) +
      (r.replaced ? "'s " + r.path : " at " + r.path)
  );
  return r;
}

export async function freePath(title: string, it: HubItem, path = ""): Promise<{ path: string; wanted: string; taken: boolean }> {
  return instApi(title, "/hub/free?id=" + encodeURIComponent(it.id) + (path ? "&path=" + encodeURIComponent(path) : ""));
}

export async function updateItem(it: HubItem, patch: Partial<Pick<HubItem, "name" | "note" | "pinned">>): Promise<HubItem | null> {
  try {
    const r = await api<HubItem>("/api/hub/items/" + encodeURIComponent(it.id), { method: "PATCH", json: patch });
    refreshHub();
    return r;
  } catch (err) {
    toast("Couldn't change " + it.name + ": " + errMsg(err));
    return null;
  }
}

export async function removeItem(it: HubItem): Promise<void> {
  try {
    await api("/api/hub/items/" + encodeURIComponent(it.id), { method: "DELETE" });
  } catch (err) {
    toast("Couldn't remove " + it.name + ": " + errMsg(err));
    return;
  }
  refreshHub();
  toast("Removed " + it.name + " from the hub", {
    duration: UNDO_MS,
    action: {
      label: "Undo",
      ariaLabel: "Undo removing " + it.name,
      onClick: () => {
        void api("/api/hub/items/" + encodeURIComponent(it.id) + "/restore", { method: "POST" })
          .then(() => {
            refreshHub();
            toast("Put " + it.name + " back");
          })
          .catch((err) => toast("Couldn't put it back: " + errMsg(err)));
      },
    },
  });
}

