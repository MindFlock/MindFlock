/** Asking a session for MindFlock workers without typing the ask each time.
 *
 * Two ways, both on the focused session:
 *
 *   Alt+G        "Ask for workers" — types the workers request into the
 *                session's agent terminal (as one pasted chunk, a trailing
 *                space, no Enter), so you add the task after it or just press
 *                Enter.
 *   Alt+Shift+G  "Workers on every prompt" — the sticky Flock mode: while on,
 *                the server adds the request to every prompt submitted in
 *                that session (`POST /api/instances/{title}/flock {on}`), so
 *                you type nothing extra. The pane's Flock control (the
 *                MindFlock raven, lib/flock's sprite as a mask) and the
 *                row menu flip the same switch.
 *
 * The request text is the server's (`GET /api/config` → `flock.request`,
 * Settings → General "Workers request" with `{model}` already filled in); an
 * older server sends no `flock`, and then the default below is used. */

import type { Config, Instance } from "../api/types";
import { instApi } from "../api/client";
import { patchInstance, queryClient, refreshInstances } from "../state/queries";
import { displayName } from "../state/store";
import { instances } from "./sessionActions";
import { peekTerm } from "./terminals";
import { isRemote } from "./playbooks";
import { errMsg } from "./format";
import { toast } from "./toast";

/** The model workers run on when Settings says nothing. */
export const DEFAULT_WORKERS_MODEL = "claude-opus-5-5";

/** Settings → General "Workers request" when unset: `{model}` is replaced by
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

/** What the pane control says. */
export function flockChipLabel(): string {
  return "Flock";
}

/** The pane control's tooltip, on or off. `keys`: the live combos for
 * [toggle, add once] (rebinds included); defaults otherwise. */
export function flockTooltip(on: boolean, keys: [string, string] = [FLOCK_KEYS, ASK_WORKERS_KEYS]): string {
  return (
    "Workers on every prompt — the request is added to everything you send here" +
    ` (${keys[0]} to toggle; ${keys[1]} to add it once)` +
    (on ? "\nOn: click to turn it off" : "\nOff: click to turn it on")
  );
}

/** The row menu's / palette's label for the toggle. */
export function flockActionLabel(on: boolean): string {
  return on ? "Workers on every prompt: turn off" : "Workers on every prompt: turn on";
}

/** What the toggle says once the server answered. */
export function flockToast(name: string, on: boolean): string {
  return on
    ? `${name}: Flock on — the workers request is added to every prompt`
    : `${name}: Flock off — prompts go as typed`;
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

/** Flip a session's Flock mode (`on` given: set it). Resolves the server's
 * answer, or null when the call failed (already toasted). */
export async function toggleFlock(title: string, on?: boolean): Promise<boolean | null> {
  const want = on ?? !instances().find((r) => r.title === title)?.flock;
  const name = displayName(title);
  try {
    const r = await instApi<{ flock?: boolean }>(title, "/flock", { json: { on: want } });
    const now = typeof r?.flock === "boolean" ? r.flock : want;
    // The 4 s poll would get there; the control flips now.
    patchInstance(title, { flock: now });
    toast(flockToast(name, now));
    return now;
  } catch (err) {
    toast(`${name}: Flock — ${errMsg(err)}`, { duration: 6000 });
    return null;
  } finally {
    void refreshInstances();
  }
}
