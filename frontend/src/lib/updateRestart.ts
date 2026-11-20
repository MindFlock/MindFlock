/** The restart an update brings, from this window's side (docs/web-ui.md
 * "Updates"): once an install is under way (`update.installing`, or an Update
 * pressed here) the server going quiet is expected, not a failure. While
 * that holds, the connection card says the server is restarting onto the new
 * version (components/ConnBanner), and when the server answers again on the
 * new build this window reloads onto its bundle by itself, whichever way the
 * update started: automatic, a toast's Update, the bell, or Settings.
 *
 * Plus `startUpdateHere()`: the toast's and the bell row's Update button. */

import { useSyncExternalStore } from "react";
import { api, ApiError } from "../api/client";
import { toast } from "./toast";
import {
  installPath,
  restartLanded,
  runFailText,
  updateModeOf,
  vLabel,
  type UpdateCheck,
  type UpdateRun,
} from "./updates";

/** How long an install's restart stays expected: the install itself, then up
 * to 30 minutes waiting for a pause in typing, and some slack. */
export const EXPECT_MAX_MS = 45 * 60_000;
/** After the server went quiet, how long to wait for it on the new build. */
export const BACK_WAIT_MS = 180_000;
const POLL_MS = 2000;

export interface ExpectedRestart {
  /** The version being installed, "v"-less ("" when not known). */
  version: string;
  /** When the install started (ms). */
  since: number;
}

let expected: ExpectedRestart | null = null;
/** This page lost the server while the restart was expected: it is running
 * the old bundle against what comes back. */
let dropped = false;
const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

/** An install is under way: the next quiet spell is its restart. */
export function expectUpdateRestart(version: string, since = Date.now()): void {
  const v = String(version || "").replace(/^v/i, "");
  if (expected && expected.version === v && expected.since === since) return;
  expected = { version: v, since };
  dropped = false;
  emit();
}

/** The install finished on the new build, failed, or was never coming. */
export function clearUpdateRestart(): void {
  if (!expected) return;
  expected = null;
  dropped = false;
  stopPoll();
  emit();
}

/** The restart in progress, or null (one older than EXPECT_MAX_MS is not). */
export function updateRestartExpected(now = Date.now()): ExpectedRestart | null {
  return expected && now - expected.since <= EXPECT_MAX_MS ? expected : null;
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

/** For components: the restart in progress, or null. */
export function useUpdateRestart(): ExpectedRestart | null {
  return useSyncExternalStore(subscribe, () => updateRestartExpected(), () => null);
}

// --- coming back on the new build ------------------------------------------ //

let poll: ReturnType<typeof setInterval> | null = null;

function stopPoll(): void {
  if (poll) clearInterval(poll);
  poll = null;
}

function reload(): void {
  stopPoll();
  if (typeof window !== "undefined") window.location.reload();
}

/** The server went quiet while a restart was expected: ask it every 2 s
 * whether it is back, and on the new build. Back and landed: reload onto the
 * new bundle. Back but still waiting for its restart (the drop was a blip):
 * stop asking and keep expecting. Back with the install failed: stop
 * expecting (update.failed says why). Never back within BACK_WAIT_MS: stop
 * expecting, so the connection card says what is really wrong. */
export function watchForNewBuild(): void {
  if (poll || !updateRestartExpected()) return;
  const t0 = Date.now();
  poll = setInterval(async () => {
    if (!updateRestartExpected()) return stopPoll();
    let c: UpdateCheck;
    try {
      c = await api<UpdateCheck>("/api/update/check");
    } catch (err) {
      // A guest (or a signed-out page) gets a refusal: the server answered,
      // so it is back — reload and let the new build decide.
      if (err instanceof ApiError && err.status > 0 && err.status < 500) return reload();
      if (Date.now() - t0 > BACK_WAIT_MS) clearUpdateRestart();
      return; // still restarting
    }
    if (restartLanded(c)) return reload();
    if (c.state === "failed" || c.state === "rolled_back") return clearUpdateRestart();
    stopPoll(); // answered, restart still to come: wait for the next drop
  }, POLL_MS);
}

/** Wire the event stream's status in: a drop while a restart is expected
 * starts the watch. Returns the unsubscribe. */
export function installUpdateRecovery(ev: { onStatus(cb: (s: "connected" | "disconnected") => void): () => void }): () => void {
  return ev.onStatus((s) => {
    if (s !== "disconnected" || !updateRestartExpected()) return;
    dropped = true;
    watchForNewBuild();
  });
}

/** `update.done` arrived: the restart is over. Live, on a page that went
 * through it (still on the old bundle), reload; anything else (a replay, a
 * page that loaded after the restart) just stops expecting. Returns whether
 * the page is reloading. */
export function onUpdateDone(live: boolean): boolean {
  const wasDropped = dropped && !!updateRestartExpected();
  clearUpdateRestart();
  if (!(live && wasDropped)) return false;
  reload();
  return true;
}

// --- the toast's and the bell's Update ------------------------------------- //

type ShellBridge = {
  install?: () => Promise<unknown>;
  updateInfo?: () => Promise<{ source?: boolean }>;
};

function shellBridge(): ShellBridge | null {
  if (typeof window === "undefined") return null;
  return (window as unknown as { mfengine?: ShellBridge }).mfengine || null;
}

let starting = false;

/** Install `version` here: through the desktop shell where it manages this
 * engine (so the app updates too), else through the server. The restart is
 * expected from here on. Toasts only what nothing else will say: a start
 * that failed, and, on an older server that sends no update.* events, how
 * its install ended. */
export async function startUpdateHere(version: string): Promise<void> {
  if (starting) return;
  starting = true;
  try {
    let check: UpdateCheck | null = null;
    try {
      check = await api<UpdateCheck>("/api/update/check");
    } catch {
      /* decide without it */
    }
    const shell = shellBridge();
    let shellSource = false;
    if (shell?.install && typeof check?.managed_by_shell !== "boolean") {
      shellSource = !!(await shell.updateInfo?.().catch(() => null))?.source;
    }
    const path = installPath({
      desktop: !!shell?.install,
      shellSource,
      managedByShell: check?.managed_by_shell,
    });
    const v = String(version || check?.latest || "").replace(/^v/i, "");
    const words = (v ? "Updating to " + vLabel(v) : "Updating MindFlock") + "…";
    if (path === "shell") {
      // The shell's run restarts the server and reloads this window itself.
      expectUpdateRestart(v);
      toast(words);
      shell!.install!().catch((err: unknown) => {
        clearUpdateRestart();
        toast(failText("The update didn’t start", err), { error: true });
      });
      return;
    }
    try {
      await api("/api/update/start", { method: "POST", json: {} });
    } catch (err) {
      toast(failText("The update didn’t start", err), { error: true });
      return;
    }
    expectUpdateRestart(v);
    toast(words);
    if (!updateModeOf(check)) followOldServerRun();
  } finally {
    starting = false;
  }
}

/** "<what>: <reason>." — the reason a rejection carried, if any. */
function failText(what: string, err: unknown): string {
  const why = String((err as { message?: unknown } | null)?.message ?? "").trim();
  return why ? what + ": " + why.replace(/\.$/, "") + "." : what + ".";
}

/** An older server emits no update.failed: follow its run and say how it
 * failed (its finished install restarts the server, which the recovery
 * above reloads onto). */
function followOldServerRun(): void {
  const t0 = Date.now();
  const t = setInterval(async () => {
    if (Date.now() - t0 > 10 * 60_000) return clearInterval(t);
    let st: UpdateRun;
    try {
      st = await api<UpdateRun>("/api/update/state");
    } catch {
      return; // restarting
    }
    if (st.restarting || st.state === "done") {
      clearInterval(t);
      watchForNewBuild();
    } else if (st.state === "failed" || st.state === "rolled_back") {
      clearInterval(t);
      clearUpdateRestart();
      toast(runFailText(st), { error: true });
    }
  }, POLL_MS);
}

/** Tests only: forget everything. */
export function _resetUpdateRestart(): void {
  expected = null;
  dropped = false;
  stopPoll();
  starting = false;
}
