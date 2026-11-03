/** Asking a session for MindFlock workers without typing the ask each time.
 *
 * "Workers" is the one word for sessions a session starts. Their one entry is
 * the "Workers…" picker (components/grid/WorkersMenu): the pane's Workers
 * control opens it, and so do the row / pane ⋯ menu's "Workers…". It has
 * three modes — "Let the agent split this now" (a split team run), and the
 * two below — whose shortcuts stay:
 *
 *   Alt+G        "Ask for workers" — types the workers request into the
 *                session's agent terminal (as one pasted chunk, a trailing
 *                space, no Enter), so you add the task after it or just press
 *                Enter.
 *   Alt+Shift+G  "Workers on every prompt" — the sticky Flock mode: while on,
 *                the server adds the request to every prompt submitted in
 *                that session (`POST /api/instances/{title}/flock {on}`), so
 *                you type nothing extra. The pane's Workers control (the
 *                MindFlock raven, lib/flock's sprite as a mask) reads
 *                "Workers ✓" while it is on.
 *
 * The request text is the server's (`GET /api/config` → `flock.request`,
 * Settings → Agent orchestration "Workers request" with `{model}` already
 * filled in); an
 * older server sends no `flock`, and then the default below is used. */

import { useSyncExternalStore } from "react";
import type { Config, Instance } from "../api/types";
import { instApi } from "../api/client";
import { patchInstance, queryClient } from "../state/queries";
import { displayName } from "../state/store";
import { instances, selectSession } from "./sessionActions";
import { peekTerm } from "./terminals";
import { isRemote } from "./playbooks";
import { errMsg } from "./format";
import { toast } from "./toast";

/** The model workers run on when Settings says nothing. */
export const DEFAULT_WORKERS_MODEL = "claude-opus-5-5";

/** Settings → Agent orchestration "Workers request" when unset: `{model}` is replaced by
 * "Workers model". */
export const DEFAULT_WORKERS_TEMPLATE =
  "Use MindFlock workers for this: spawn as many MindFlock agents as the work needs (model {model}), " +
  "each fenced to disjoint files with a self-contained brief; wait for them, review and merge their " +
  "branches, then report. Do the small connecting pieces yourself.";

/** The default hotkeys, as the "?" sheet, the menu and the tooltips say them
 * (keymap.ts binds them; the sheet shows a rebind instead). */
export const ASK_WORKERS_KEYS = "Alt+G";
export const FLOCK_KEYS = "Alt+Shift+G";

/** A request template with its `{model}` filled in (blank model → the
 * default). */
export function fillWorkersTemplate(template: string, model?: string | null): string {
  const m = (model || "").trim() || DEFAULT_WORKERS_MODEL;
  return template.split("{model}").join(m);
}

/** The words the hotkey types: the server's request, else the default. */
export function workersRequestText(config: Pick<Config, "flock"> | null | undefined): string {
  const req = (config?.flock?.request || "").trim();
  if (req) return req;
  return fillWorkersTemplate(DEFAULT_WORKERS_TEMPLATE, config?.flock?.model);
}

function cachedConfig(): Config | undefined {
  return queryClient.getQueryData<Config>(["config"]);
}

/** Whether Flock mode can be offered for a row: one of this machine's own
 * sessions (not another device's, not a peer's, not a shared folder), past
 * its start, and never to a guest. */
export function flockAvailable(inst: Instance | undefined, config?: Pick<Config, "guest"> | null): boolean {
  if (!inst || inst.pending) return false;
  if (config?.guest) return false;
  if (inst.peer_share || isRemote(inst)) return false;
  return !(inst as { remote_peer?: boolean }).remote_peer;
}

/** What the pane control says: "Workers", with a ✓ while "Workers on every
 * prompt" is on. */
export function flockChipLabel(on = false): string {
  return on ? "Workers ✓" : "Workers";
}

/** The pane control's tooltip, on or off. `keys`: the live combos for
 * [every prompt, ask once] (rebinds included); defaults otherwise. */
export function flockTooltip(on: boolean, keys: [string, string] = [FLOCK_KEYS, ASK_WORKERS_KEYS]): string {
  return (
    "Workers — let the agent split this now, ask for workers once, or on every prompt" +
    ` (${keys[1]} asks once; ${keys[0]} turns every prompt on or off)` +
    (on ? "\nWorkers on every prompt: on" : "")
  );
}

/** The row menu's / palette's label for the toggle. */
export function flockActionLabel(on: boolean): string {
  return on ? "Workers on every prompt: turn off" : "Workers on every prompt: turn on";
}

/** What the toggle says once the server answered. */
export function flockToast(name: string, on: boolean): string {
  return on
    ? `${name}: Workers on every prompt — on`
    : `${name}: Workers on every prompt — off`;
}

/** Type the workers request into a session's agent terminal and give it the
 * keyboard. A bracketed paste (xterm wraps it when the CLI asked for that
 * mode), so the CLI takes it as one chunk, then a typed space — never Enter:
 * the person adds the task, or just presses Enter. False when the terminal
 * isn't open or connected. */
export function insertWorkersRequest(title: string, config: Config | null | undefined = cachedConfig()): boolean {
  const h = peekTerm(title, "agent");
  if (!h || h.state !== "connected") {
    toast(`${displayName(title)}: its terminal isn’t connected — open the session first`);
    return false;
  }
  h.term.paste(workersRequestText(config));
  h.send(" ");
  // Again after a frame: run from the palette, the closing dialog hands focus
  // back to whatever had it before.
  h.term.focus();
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => h.term.focus());
  return true;
}

/** Flip a session's "Workers on every prompt" (`on` given: set it). Resolves the server's
 * answer, or null when the call failed (already toasted). */
export async function toggleFlock(title: string, on?: boolean): Promise<boolean | null> {
  const before = !!instances().find((r) => r.title === title)?.flock;
  const want = on ?? !before;
  const name = displayName(title);
  // The control flips the instant it is clicked — the server's answer only
  // confirms it, and the listing stamps the live flag on every poll, so no
  // refetch is forced here (it used to race the cached snapshot and snap the
  // control back for a tick, which read as "slow").
  patchInstance(title, { flock: want });
  try {
    const r = await instApi<{ flock?: boolean }>(title, "/flock", { json: { on: want } });
    const now = typeof r?.flock === "boolean" ? r.flock : want;
    if (now !== want) patchInstance(title, { flock: now });
    toast(flockToast(name, now));
    return now;
  } catch (err) {
    patchInstance(title, { flock: before });
    toast(`${name}: Workers on every prompt — ${errMsg(err)}`, { duration: 6000 });
    return null;
  }
}

// --- The Workers… picker's open state -----------------------------------------
// One picker open at a time, hung under its pane's Workers control. A tiny
// store of its own (not the UI store): only that control and its pane read it.

let workersMenuTitle: string | null = null;
const workersMenuSubs = new Set<() => void>();

function setWorkersMenu(title: string | null) {
  if (workersMenuTitle === title) return;
  workersMenuTitle = title;
  workersMenuSubs.forEach((f) => f());
}

/** The session whose Workers… picker is open (null = none). */
export function workersMenuOpenFor(): string | null {
  return workersMenuTitle;
}

/** Whether THIS pane's picker is the open one. */
export function useWorkersMenu(title: string): boolean {
  return useSyncExternalStore(
    (f) => {
      workersMenuSubs.add(f);
      return () => workersMenuSubs.delete(f);
    },
    () => workersMenuTitle === title,
    () => false
  );
}

/** Open (or, `toggle`, flip) a session's Workers… picker — from its pane's
 * Workers control, the row / pane ⋯ menu's "Workers…" or the palette. The
 * session is selected first so its pane (the picker's anchor) is on screen. */
export function openWorkersMenu(title: string, toggle = false): void {
  if (toggle && workersMenuTitle === title) {
    setWorkersMenu(null);
    return;
  }
  const inst = instances().find((r) => r.title === title);
  if (!inst || inst.pending) {
    toast("That session isn't ready yet");
    return;
  }
  selectSession(title, { noKeyboard: true });
  // A frame later: the pane may have just been un-hidden, and its Workers
  // control is what the picker measures itself against.
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => setWorkersMenu(title));
  else setWorkersMenu(title);
}

export function closeWorkersMenu(): void {
  setWorkersMenu(null);
}
