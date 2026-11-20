/** Updates (docs/web-ui.md "Updates"): the server's answer to
 * `GET /api/update/check`, the one setting (`updates.mode`), and the words
 * for the update.* events. Pure, so vitest pins them; the stateful half (the
 * expected restart, starting an install from a toast) is lib/updateRestart.
 *
 * Every field the overhaul added is optional: an older server sends none of
 * them, and then Settings → Advanced and the toasts behave as they did. */

import { compareVersions, isReleaseVersion } from "./whatsNew";

/** `updates.mode`: install by itself, offer once per version, or stay quiet. */
export type UpdateMode = "auto" | "ask" | "off";

export const UPDATE_MODES: ReadonlyArray<{ value: UpdateMode; label: string; hint: string }> = [
  { value: "auto", label: "Automatic", hint: "Installs new versions by itself and restarts when you pause." },
  { value: "ask", label: "Ask me", hint: "Tells you once per version; one click installs it." },
  { value: "off", label: "Off", hint: "Never checks by itself. Check now still works." },
];

/** A development checkout (`dev` in the check): which branch it is on and
 * whether the server can fast-forward it. */
export interface DevCheckout {
  branch?: string;
  default_branch?: string;
  dirty?: boolean;
  behind?: number;
  ahead?: number;
  can_update?: boolean;
  /** One plain sentence when `can_update` is false. */
  why_not?: string;
}

/** `GET /api/update/check`. */
export interface UpdateCheck {
  current?: string;
  latest?: string;
  tag?: string;
  release_url?: string;
  checked?: boolean;
  available?: boolean;
  /** How this engine is installed: uv-tool · pipx · editable · other. */
  kind?: string;
  method_source?: string;
  checkout?: string;
  /** Still sent for old clients; never shown any more. */
  editable_commands?: string[];
  blocked?: string;
  repo?: string;
  /** The last update's state: idle · started · done · failed · rolled_back. */
  state?: string;
  /** Installed, but this server isn't running it yet. */
  restart_pending?: boolean;
  last?: { ref?: string; version?: string; from_version?: string; error?: string; code?: number };
  // --- the overhaul's additions (absent on an older server) ---------------- //
  mode?: string;
  mode_set?: boolean;
  managed_by_shell?: boolean;
  auto?: { active?: boolean; why?: string; waiting?: string };
  dev?: DevCheckout | null;
}

/** `GET /api/update/state` while an install runs. */
export interface UpdateRun {
  state?: string;
  code?: number;
  error?: string;
  log?: string[];
  restarting?: boolean;
}

/** The effective mode, or null when the server doesn't have the setting (an
 * older server: no choice is drawn, and the screen behaves as before). */
export function updateModeOf(c: Pick<UpdateCheck, "mode"> | null | undefined): UpdateMode | null {
  const m = c?.mode;
  return m === "auto" || m === "ask" || m === "off" ? m : null;
}

/** "v0.14.22" of "0.14.22" or "v0.14.22"; "" stays "". */
export function vLabel(raw: unknown): string {
  const v = String(raw ?? "").trim().replace(/^v/i, "");
  return v ? "v" + v : "";
}

/** The one plain line of state under the choice: why a development checkout
 * can't update, else what automatic updates are doing ("" when the server
 * says nothing). */
export function updateStateLine(c: UpdateCheck | null | undefined): string {
  if (!c) return "";
  if (c.dev && c.dev.can_update === false) return String(c.dev.why_not || "").trim();
  return String(c.auto?.why || "").trim();
}

/** Whether there is something to install here: for a development checkout,
 * exactly when it is behind and can fast-forward (the server already folds
 * that into `available`; a stale or older answer is read the same way). */
export function updateAvailable(c: UpdateCheck | null | undefined): boolean {
  if (!c) return false;
  if (c.dev) return !!c.available && c.dev.can_update !== false;
  return !!c.available && !c.blocked;
}

/** What the button says: "Update to v0.14.22", or for a checkout with no
 * release among its new commits "Update (3 new commits)". */
export function updateButtonLabel(c: UpdateCheck | null | undefined): string {
  const v = vLabel(c?.latest);
  const behind = Number(c?.dev?.behind) || 0;
  if (c?.dev && behind > 0 && (!v || !c.available || v === vLabel(c.current)))
    return "Update (" + behind + " new commit" + (behind === 1 ? "" : "s") + ")";
  return v ? "Update to " + v : "Update";
}

/** Who installs when the button (or a toast's Update) is pressed: the desktop
 * shell (its install updates the app too) or the server itself.
 *
 *  - not in the desktop app (no `mfengine.install`): the server;
 *  - the server says no shell manages this engine (a dev engine, a desktop
 *    window over a checkout): the server;
 *  - the server says a shell manages it: the shell;
 *  - an older server that says neither: today's split — the shell, unless it
 *    runs from source (a dev shell never installs). */
export function installPath(o: {
  desktop: boolean;
  shellSource?: boolean;
  managedByShell?: boolean;
}): "shell" | "server" {
  if (!o.desktop) return "server";
  if (o.managedByShell === false) return "server";
  if (o.managedByShell === true) return "shell";
  return o.shellSource ? "server" : "shell";
}

/** A check taken after the restart says the new build is running: nothing
 * installed-but-waiting and no install mid-way. */
export function restartLanded(c: Pick<UpdateCheck, "restart_pending" | "state"> | null | undefined): boolean {
  return !!c && !c.restart_pending && c.state !== "started";
}

/** What a failed run says, in one sentence. */
export function runFailText(st: UpdateRun): string {
  if (st.state === "rolled_back") return "The new version didn’t start, so the previous one was put back.";
  if (st.error === "interrupted") return "The update was interrupted. Try again.";
  if (st.error && !/^\w+$/.test(st.error)) return st.error.replace(/\.?$/, ".");
  return "The update didn’t finish (exit " + (st.code ?? "?") + ").";
}

// --- the update.* events --------------------------------------------------- //

export interface UpdateEventNote {
  /** The bell row's words ("" = no row: `update.installing` is toast-only). */
  text: string;
  /** The toast's words. */
  toast: string;
  cls: "n-info" | "n-done" | "n-warn";
  /** The version it is about, "v"-less. */
  version: string;
}

/** `update.installing` / `update.done` / `update.failed` in words, or null for
 * any other event (or one without a version). */
export function updateEventNote(event: string, data: Record<string, unknown> | null | undefined): UpdateEventNote | null {
  const d = data || {};
  const version = String(d.version || d.latest || "").trim().replace(/^v/i, "");
  const v = vLabel(version);
  switch (event) {
    case "update.installing":
      return {
        text: "",
        toast: (v ? "Updating to " + v : "Updating MindFlock") + "…",
        cls: "n-info",
        version,
      };
    case "update.done":
      return {
        text: v ? "Updated to " + v : "Updated MindFlock",
        toast: v ? "Updated to " + v : "Updated MindFlock",
        cls: "n-done",
        version,
      };
    case "update.failed": {
      const why = String(d.error || "").trim().replace(/\.$/, "");
      const head = v ? "Couldn’t update to " + v : "Couldn’t update MindFlock";
      const text = why ? head + ": " + why + "." : head + ".";
      return { text, toast: text, cls: "n-warn", version };
    }
    default:
      return null;
  }
}

/** Whether `update.available` carries an "Update" button that installs here:
 * only for the owner, only when THIS device is behind and nothing blocks it,
 * and never on an automatic answer (`auto: true`: it is installing already). */
export function canOfferUpdate(data: Record<string, unknown> | null | undefined, owner: boolean): boolean {
  const d = data || {};
  if (!owner || !d.latest) return false;
  if (d.auto === true) return false;
  if (d.here === false) return false;
  return !String(d.blocked || "").trim();
}

/** How long after the fact an `update.done` replayed on (re)connect still
 * earns its toast: the page that reloaded onto the new version sees the
 * event as a replay, and should still say "Updated". */
export const DONE_TOAST_FRESH_S = 600;

/** Whether `update.done` toasts: live, or replayed but fresh, and never twice
 * for the same version (`seen` = the version last toasted in this browser). */
export function doneToastWanted(o: {
  version: string;
  replay: boolean;
  ts: number;
  nowS: number;
  seen: string | null;
}): boolean {
  if (o.seen && o.version && o.seen === o.version) return false;
  if (!o.replay) return true;
  return o.nowS - o.ts <= DONE_TOAST_FRESH_S;
}

/** Whether What's new has something to show for an update from `from` to
 * `to`: an entry above the old version and at or below the new one. */
export function whatsNewCovers(
  releases: ReadonlyArray<{ version: string }>,
  to: string,
  from: string | null | undefined
): boolean {
  if (!isReleaseVersion(to)) return false;
  const since = isReleaseVersion(from) ? from : null;
  return releases.some((r) => compareVersions(r.version, to) <= 0 && (!since || compareVersions(r.version, since) > 0));
}
