/** The one floating folder picker: who it is filling, where it sits, and how
 * far it may be dragged.
 *
 * Every editable path field opens the SAME window (components/
 * FolderPickerWindow.tsx, mounted once by App). A second field asking for it
 * re-targets it rather than stacking a second window: the earlier field's
 * browse is cancelled (its `onCancel` runs) and the window starts over for the
 * new one. Requests carry an id so a field that closes its own browse late —
 * an effect cleanup after the window already moved on — can never close
 * somebody else's.
 *
 * The window's position and size are a per-browser convenience (where it was
 * left on THIS screen), so they live in localStorage under one key and never in
 * the synced prefs: a laptop's corner is not a phone's. */

import { useEffect, useRef, useSyncExternalStore } from "react";

export interface FolderPickerRequest {
  /** Set by openFolderPicker. */
  id: number;
  /** Title-bar text. Default "Pick a folder". */
  title?: string;
  /** Whose disk: "" here, else the tailnet device. */
  device?: string;
  /** Where the browse starts — the field's value. A path that does not exist
   * falls back to its nearest existing parent, then home (seedChain). */
  start: string;
  /** The highlighted folder at open (default: `start`). */
  selected?: string;
  /** The element that opened it: the window opens beside it the first time. */
  anchor?: HTMLElement | null;
  /** A row was clicked (the window stays open). */
  onSelect?(path: string): void;
  /** "Use this folder": fill the field. The window closes. */
  onPick(path: string): void;
  /** Closed without a pick: ✕, Escape, or another field took the window. */
  onCancel?(): void;
}

let current: FolderPickerRequest | null = null;
let nextId = 1;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

export function getFolderPicker(): FolderPickerRequest | null {
  return current;
}

/** Open the window for a field (or re-target it). Returns the request id. */
export function openFolderPicker(req: Omit<FolderPickerRequest, "id">): number {
  const prev = current;
  const id = nextId++;
  current = { ...req, id };
  emit();
  // After the swap, so a cancel that closes its own browse (and so calls
  // closeFolderPicker with its old id) finds a newer request and does nothing.
  prev?.onCancel?.();
  return id;
}

/** Close the window without a pick. With an id, only if that request is still
 * the one on screen. `notify` false skips the owner's onCancel — for an owner
 * closing its own browse, which already knows. */
export function closeFolderPicker(id?: number, notify = true): void {
  if (!current || (id !== undefined && current.id !== id)) return;
  const r = current;
  current = null;
  emit();
  if (notify) r.onCancel?.();
}

/** "Use this folder". */
export function pickFolder(path: string): void {
  if (!current) return;
  const r = current;
  current = null;
  emit();
  r.onPick(path);
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

export function useFolderPicker(): FolderPickerRequest | null {
  return useSyncExternalStore(subscribe, getFolderPicker, getFolderPicker);
}

/** Keep the window open while `open` is true, for a field whose browse state
 * lives in its own form (the New form's folderReducer): opening it opens the
 * window, the form closing it closes the window (silently — the form already
 * knows), and the window closing itself reports back through onCancel/onPick.
 * The callbacks are read at call time, so they may change every render. */
export function useFolderPickerWhile(open: boolean, req: () => Omit<FolderPickerRequest, "id">): void {
  const latest = useRef(req);
  latest.current = req;
  useEffect(() => {
    if (!open) return;
    const first = latest.current();
    const id = openFolderPicker({
      ...first,
      onSelect: (p) => latest.current().onSelect?.(p),
      onPick: (p) => latest.current().onPick(p),
      onCancel: () => latest.current().onCancel?.(),
    });
    return () => closeFolderPicker(id, false);
  }, [open]);
}

// ---- where the browse starts ------------------------------------------------

/** The folder above `p`, as text: "/a/b" → "/a", "/a" → "/", "~/x" → "~",
 * "./x" → ".", "x" → "". "" once there is nothing above. */
export function parentPath(p: string): string {
  const t = p.length > 1 ? p.replace(/[\\/]+$/, "") : p;
  if (t === "/" || t === "~" || t === "." || t === "") return "";
  const i = Math.max(t.lastIndexOf("/"), t.lastIndexOf("\\"));
  if (i < 0) return "";
  if (i === 0) return "/";
  return t.slice(0, i);
}

/** The folders to try, in order, when the picker opens on `value`: the value
 * itself, each parent up to the root, then "" (the server's home). A field
 * naming a folder that does not exist yet opens on the nearest one that does. */
export function seedChain(value: string): string[] {
  const out: string[] = [];
  let p = value.trim();
  while (p && out.length < 32) {
    out.push(p);
    p = parentPath(p);
  }
  out.push("");
  return out;
}

// ---- geometry ---------------------------------------------------------------

export interface PickerRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** localStorage key of the last position + size (a per-browser convenience). */
export const PICKER_RECT_KEY = "mf_folder_picker_rect";
export const PICKER_MARGIN = 8;
export const PICKER_MIN = { w: 280, h: 240 };
export const PICKER_DEFAULT = { w: 440, h: 440 };

/** Keep the window wholly on screen: never bigger than the viewport (less a
 * margin each side), never smaller than the minimum unless the viewport is,
 * and moved back inside so the title bar is always reachable to drag. */
export function clampRect(r: PickerRect, vw: number, vh: number, m = PICKER_MARGIN): PickerRect {
  const maxW = Math.max(0, vw - 2 * m);
  const maxH = Math.max(0, vh - 2 * m);
  const w = Math.min(Math.max(r.w, PICKER_MIN.w), maxW);
  const h = Math.min(Math.max(r.h, PICKER_MIN.h), maxH);
  const x = Math.min(Math.max(r.x, m), vw - m - w);
  const y = Math.min(Math.max(r.y, m), vh - m - h);
  return { x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) };
}

/** Where a drag that started at (sx, sy) with the window at `from` puts it
 * when the pointer is at (px, py). */
export function dragRect(
  from: PickerRect,
  sx: number,
  sy: number,
  px: number,
  py: number,
  vw: number,
  vh: number
): PickerRect {
  return clampRect({ ...from, x: from.x + (px - sx), y: from.y + (py - sy) }, vw, vh);
}

/** Where the window opens the first time: beside the button that opened it
 * (right of it, else left of it), else centred. */
export function defaultRect(
  anchor: { left: number; right: number; top: number } | null,
  vw: number,
  vh: number,
  size: { w: number; h: number } = PICKER_DEFAULT
): PickerRect {
  const gap = 12;
  const { w, h } = size;
  const centred = { x: (vw - w) / 2, y: (vh - h) / 2, w, h };
  if (!anchor) return clampRect(centred, vw, vh);
  const y = anchor.top - 60;
  if (anchor.right + gap + w <= vw - PICKER_MARGIN) return clampRect({ x: anchor.right + gap, y, w, h }, vw, vh);
  if (anchor.left - gap - w >= PICKER_MARGIN) return clampRect({ x: anchor.left - gap - w, y, w, h }, vw, vh);
  return clampRect(centred, vw, vh);
}

export function loadPickerRect(): PickerRect | null {
  try {
    const v = JSON.parse(localStorage.getItem(PICKER_RECT_KEY) || "null");
    if (v && [v.x, v.y, v.w, v.h].every((n) => typeof n === "number" && Number.isFinite(n))) return v;
  } catch {
    /* private mode, bad JSON: open at the default */
  }
  return null;
}

export function savePickerRect(r: PickerRect): void {
  try {
    localStorage.setItem(PICKER_RECT_KEY, JSON.stringify(r));
  } catch {
    /* the position is a convenience; losing it costs nothing */
  }
}
