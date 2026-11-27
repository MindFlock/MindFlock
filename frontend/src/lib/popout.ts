/**
 * Pop-out windows: a session in a window of its own, torn off the main window
 * like a browser tab (drag its pane header out, or "Move to new window") and
 * put back the same way (drag it onto the main window, its "Back" button, or
 * just close it).
 *
 * One API, two backends:
 *
 * - **Desktop app** — the preload bridge `window.mfwin` (electron/preload.js).
 *   The shell's main process owns the truth: which session is in which
 *   window, where each window sits (restored at the next launch), and focus.
 *   It pushes the list of popped-out titles to every window on each change.
 * - **Browser** — `window.open` a popup on the same origin, and a
 *   BroadcastChannel ({@link CHANNEL}) between it and the main window: each
 *   pop-out says "here" every {@link HEARTBEAT_MS} (and once on a "who?"),
 *   "bye" when it closes; the main window counts a title popped out while it
 *   has heard from it within {@link EXPIRE_MS}. Focus and close requests go
 *   the other way. A desktop shell built before pop-outs has no `mfwin` and
 *   uses this path too (its window-open handler then sends the popup to the
 *   system browser, so {@link popoutSupported} says no there).
 *
 * A pop-out window loads the app with `?popout=<title>` ({@link popoutUrl});
 * the app sees {@link isPopoutWindow} and mounts the single-session view
 * instead of the grid, and never writes the main window's saved layout.
 *
 * The main window treats a popped-out session like one that is on screen
 * elsewhere: it leaves the grid (its terminals are released, so tmux is never
 * sized by two windows at once), its rail row shows it is in its own window,
 * and selecting it focuses that window.
 */
import { create } from "zustand";
import { inShell } from "./shell";

export const POPOUT_PARAM = "popout";
export const CHANNEL = "mf-popouts";
export const HEARTBEAT_MS = 2000;
export const EXPIRE_MS = 6500;
/** A pop-out's size when the caller gives none (CSS px). */
export const DEFAULT_SIZE = { width: 900, height: 640 };
/** Below this viewport width the app is the phone layout: no pop-outs. */
const MIN_MAIN_WIDTH = 760;

/** Where a new pop-out goes: its top-left on the SCREEN (CSS px, as
 * `event.screenX/Y`), and its size. All optional. */
export interface PopoutPlacement {
  x?: number;
  y?: number;
  width?: number;
  height?: number;
}

/** The desktop shell's bridge (electron/preload.js `mfwin`). */
export interface WinBridge {
  /** Open (or focus, when one exists) the pop-out for `title`. */
  open(o: { title: string } & PopoutPlacement): Promise<boolean>;
  /** Raise the pop-out for `title`; false when there is none. */
  focus(title: string): Promise<boolean>;
  /** Close the pop-out for `title`: the session goes back to the main window. */
  close(title: string): Promise<boolean>;
  /** The session was renamed: its pop-out follows (URL, title, saved place). */
  rename(from: string, to: string): Promise<boolean>;
  /** The titles in pop-outs now. */
  list(): Promise<string[]>;
  /** Every change to that list; returns an unsubscribe. */
  onChanged(cb: (titles: string[]) => void): () => void;
  /** Raise the main window (a pop-out's "Back" after docking, a toast's
   * action). */
  focusMain(): Promise<boolean>;
}

type Msg =
  | { t: "here"; title: string }
  | { t: "bye"; title: string }
  | { t: "who" }
  | { t: "focus"; title: string }
  | { t: "close"; title: string }
  | { t: "rename"; from: string; to: string };

// --------------------------------------------------------------------------- //
// Where we are
// --------------------------------------------------------------------------- //

function bridge(): WinBridge | undefined {
  if (typeof window === "undefined") return undefined;
  return (window as unknown as { mfwin?: WinBridge }).mfwin;
}

/** The session this window is a pop-out of, or null in the main window. */
export function popoutTitle(search?: string): string | null {
  const s = search ?? (typeof location !== "undefined" ? location.search : "");
  try {
    const t = new URLSearchParams(s).get(POPOUT_PARAM);
    return t && t.trim() ? t : null;
  } catch {
    return null;
  }
}

/** True in a pop-out window. */
export function isPopoutWindow(): boolean {
  return popoutTitle() !== null;
}

/** The URL a pop-out of `title` loads (same origin, the app's own page). */
export function popoutUrl(title: string, origin?: string): string {
  const base = origin ?? (typeof location !== "undefined" ? location.origin : "");
  return `${base}/?${POPOUT_PARAM}=${encodeURIComponent(title)}`;
}

/** Whether this window can pop sessions out: the main window of the desktop
 * app (with a shell that has pop-outs) or of a desktop browser — never a
 * pop-out itself, a phone-width layout, or an old shell that would send the
 * window to the system browser. */
export function popoutSupported(): boolean {
  if (typeof window === "undefined" || isPopoutWindow()) return false;
  if (inShell()) return !!bridge();
  if (typeof BroadcastChannel === "undefined") return false;
  return (window.innerWidth || 0) > MIN_MAIN_WIDTH;
}

// --------------------------------------------------------------------------- //
// The popped-out set (the main window's view of it)
// --------------------------------------------------------------------------- //

interface PopoutState {
  titles: ReadonlySet<string>;
  set: (titles: Iterable<string>) => void;
}

export const usePopouts = create<PopoutState>((set) => ({
  titles: new Set<string>(),
  set: (titles) => set({ titles: new Set(titles) }),
}));

/** The titles in windows of their own (a React hook). */
export function usePoppedOut(): ReadonlySet<string> {
  return usePopouts((s) => s.titles);
}

/** The same, outside React. */
export function poppedOut(): ReadonlySet<string> {
  return usePopouts.getState().titles;
}

export function isPoppedOut(title: string): boolean {
  return poppedOut().has(title);
}

// Browser path: title -> last heard (ms).
const heard = new Map<string, number>();
let channel: BroadcastChannel | null = null;
let sweep: ReturnType<typeof setInterval> | null = null;

function chan(): BroadcastChannel | null {
  if (channel || typeof BroadcastChannel === "undefined") return channel;
  try {
    channel = new BroadcastChannel(CHANNEL);
  } catch {
    channel = null;
  }
  return channel;
}

function post(m: Msg): void {
  try {
    chan()?.postMessage(m);
  } catch {
    /* a closed channel: nothing to tell */
  }
}

function publishHeard(now = Date.now()): void {
  for (const [t, at] of heard) if (now - at > EXPIRE_MS) heard.delete(t);
  const next = [...heard.keys()].sort();
  const cur = [...poppedOut()].sort();
  if (next.join("\0") !== cur.join("\0")) usePopouts.getState().set(next);
}

let registryInstalled = false;

/** The main window: keep {@link usePopouts} current. Idempotent; a no-op in
 * a pop-out. Returns an uninstall (tests). */
export function installPopoutRegistry(): () => void {
  if (registryInstalled || isPopoutWindow() || typeof window === "undefined") return () => {};
  registryInstalled = true;
  const b = bridge();
  if (b) {
    const off = b.onChanged((titles) => usePopouts.getState().set(titles || []));
    void b.list().then((titles) => usePopouts.getState().set(titles || []), () => {});
    return () => {
      off();
      registryInstalled = false;
    };
  }
  const c = chan();
  if (!c) return () => {};
  const onMsg = (e: MessageEvent<Msg>) => {
    const m = e.data;
    if (!m || typeof m !== "object") return;
    if (m.t === "here") {
      heard.set(m.title, Date.now());
      publishHeard();
    } else if (m.t === "bye") {
      heard.delete(m.title);
      publishHeard();
    }
  };
  c.addEventListener("message", onMsg);
  post({ t: "who" });
  sweep = setInterval(() => publishHeard(), HEARTBEAT_MS);
  return () => {
    c.removeEventListener("message", onMsg);
    if (sweep) clearInterval(sweep);
    sweep = null;
    registryInstalled = false;
  };
}

// --------------------------------------------------------------------------- //
// Acting on pop-outs (from the main window)
// --------------------------------------------------------------------------- //

/** Pop `title` out into a window of its own; resolves false when it could not
 * (unsupported here, or the browser blocked the popup — a drag's end is not a
 * click to a popup blocker, so the caller should offer the menu item then). */
export async function openPopout(title: string, at: PopoutPlacement = {}): Promise<boolean> {
  if (!title || !popoutSupported()) return false;
  const width = Math.round(at.width || DEFAULT_SIZE.width);
  const height = Math.round(at.height || DEFAULT_SIZE.height);
  const b = bridge();
  if (b) {
    try {
      return !!(await b.open({ title, x: at.x, y: at.y, width, height }));
    } catch {
      return false;
    }
  }
  const feats = [`popup`, `width=${width}`, `height=${height}`];
  if (at.x != null) feats.push(`left=${Math.round(at.x)}`);
  if (at.y != null) feats.push(`top=${Math.round(at.y)}`);
  let w: Window | null = null;
  try {
    w = window.open(popoutUrl(title), popoutName(title), feats.join(","));
  } catch {
    w = null;
  }
  if (!w) return false;
  // Counted at once (its first heartbeat is a page load away), so the pane
  // leaves the grid without a flicker of both.
  heard.set(title, Date.now());
  publishHeard();
  return true;
}

/** Raise the pop-out showing `title`. False when it is not popped out. */
export function focusPopout(title: string): boolean {
  if (!isPoppedOut(title)) return false;
  const b = bridge();
  if (b) {
    void b.focus(title).catch(() => {});
    return true;
  }
  // A browser only lets a window raise ITSELF, and only sometimes; the
  // pop-out tries (window.focus) and the named handle is the fallback.
  post({ t: "focus", title });
  try {
    window.open("", popoutName(title))?.focus();
  } catch {
    /* blocked: the pop-out's own focus() was the best we had */
  }
  return true;
}

/** Close the pop-out showing `title`: the session goes back to the grid. */
export function closePopout(title: string): void {
  const b = bridge();
  if (b) {
    void b.close(title).catch(() => {});
    return;
  }
  post({ t: "close", title });
  heard.delete(title);
  publishHeard();
}

/** A session was renamed: its pop-out (if any) follows. */
export function renamePopout(from: string, to: string): void {
  if (!from || !to || from === to) return;
  const b = bridge();
  if (b) {
    void b.rename(from, to).catch(() => {});
    return;
  }
  if (heard.has(from)) {
    heard.set(to, heard.get(from) || Date.now());
    heard.delete(from);
    publishHeard();
  }
  post({ t: "rename", from, to });
}

/** Raise the main window (desktop app; a browser can't be made to). */
export function focusMainWindow(): void {
  void bridge()?.focusMain().catch(() => {});
}

/** The `window.open` target name of `title`'s pop-out: the same window is
 * reused (and focused) when it is opened again. */
export function popoutName(title: string): string {
  let h = 0;
  for (let i = 0; i < title.length; i++) h = (h * 31 + title.charCodeAt(i)) | 0;
  return `mf-popout-${(h >>> 0).toString(36)}`;
}

// --------------------------------------------------------------------------- //
// Inside a pop-out
// --------------------------------------------------------------------------- //

export interface PopoutSelfHandlers {
  /** The main window asked this pop-out to close (browser path). */
  onClose?: () => void;
  /** The session was renamed: show the new title (browser path; the shell
   * reloads the window itself). */
  onRename?: (to: string) => void;
}

/**
 * A pop-out window: tell the main window this title is here (browser path;
 * the desktop shell already knows), and follow its requests. `getTitle`
 * reads the current title (it changes on a rename). Returns a stop.
 */
export function announcePopout(getTitle: () => string, h: PopoutSelfHandlers = {}): () => void {
  if (bridge()) return () => {};
  const c = chan();
  if (!c) return () => {};
  const here = () => post({ t: "here", title: getTitle() });
  const onMsg = (e: MessageEvent<Msg>) => {
    const m = e.data;
    if (!m || typeof m !== "object") return;
    const me = getTitle();
    if (m.t === "who") here();
    else if (m.t === "focus" && m.title === me) {
      try {
        window.focus();
      } catch {
        /* the browser said no */
      }
    } else if (m.t === "close" && m.title === me) {
      h.onClose ? h.onClose() : window.close();
    } else if (m.t === "rename" && m.from === me) {
      h.onRename?.(m.to);
    }
  };
  c.addEventListener("message", onMsg);
  here();
  const beat = setInterval(here, HEARTBEAT_MS);
  const bye = () => post({ t: "bye", title: getTitle() });
  window.addEventListener("pagehide", bye);
  return () => {
    clearInterval(beat);
    c.removeEventListener("message", onMsg);
    window.removeEventListener("pagehide", bye);
    bye();
  };
}

/** Close THIS pop-out (its "Back to main window"): the session returns to the
 * grid. The desktop shell closes it through the bridge (so it also forgets
 * its place); a browser popup closes itself. */
export function closeThisPopout(): void {
  const title = popoutTitle();
  const b = bridge();
  if (b && title) {
    void b.close(title).catch(() => window.close());
    return;
  }
  if (title) post({ t: "bye", title });
  window.close();
}

/** Drag data that marks a session being dragged BETWEEN windows (a pop-out's
 * header dropped onto the main window docks it). */
export const SESSION_DRAG_MIME = "application/x-mf-session";
