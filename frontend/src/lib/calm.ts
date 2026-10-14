/** The calm first hour: what a brand-new install leaves off the main screen.
 *
 * A first-time user needs three things: their sessions, + New, and Setup. The
 * rest of the main screen — the Assistant / Usage / extension bars, the usage
 * strip, the View presets, the "more bars" hint, row numbers, a pane header's
 * dozen controls — is for someone who already runs a flock. This module is the
 * ONE rule that decides which of the two people is looking.
 *
 * A NEWCOMER is someone whose first session is under a week old (or who has
 * none yet), read from the server's own setup record — the `repo` step's `at`
 * (backend/web/core/setup_steps.py), stamped by the first session create. It
 * is the same on every device and every browser, and a veteran is never
 * mistaken for a newcomer by a cleared profile or a second computer. An
 * install from before the checklist existed reads as a veteran (no date).
 *
 * CALM is a newcomer with fewer than three sessions. Three sessions, or a
 * week, is when the full screen earns its place — it comes back by itself,
 * no switch to find. Nothing here deletes anything: every hidden control is a
 * fold, a "⋯" item, a Customize checkbox or a palette entry away, and a
 * choice the user made (Customize → Sidebar, a View preset) always wins.
 *
 * Unknown (the setup payload not in yet) is NOT calm: a veteran's screen must
 * never flash empty while /api/setup is in flight. The server's `onboarded`
 * flag stands in meanwhile — false only on a genuinely fresh install. */

import type { SetupStatus } from "../api/types";

/** A first session younger than this still counts as the first hour. */
export const NEWCOMER_DAYS = 7;
/** From this many sessions on, the full main screen shows. */
export const CALM_MAX_SESSIONS = 3;

export interface CalmInput {
  /** The person's own sessions on the rail (no verify runs, no windows). */
  sessions: number;
  status: SetupStatus | null | undefined;
  /** config.onboarded — the stand-in until /api/setup lands. */
  onboarded?: boolean;
  /** Epoch ms (tests); default Date.now(). */
  now?: number;
}

/** Whether this is someone's first week: no session yet, or the first one
 * started under NEWCOMER_DAYS ago. `null` = can't tell yet. */
export function isNewcomer(status: SetupStatus | null | undefined, onboarded?: boolean, now = Date.now()): boolean | null {
  if (!status) return onboarded === false ? true : null;
  const repo = (status.steps || []).find((s) => s.id === "repo");
  if (!repo) return null;
  if (!repo.done) return true;
  const at = repo.at ? Date.parse(repo.at) : NaN;
  // Done with no date: an install from before the checklist kept dates.
  if (!isFinite(at)) return false;
  return now - at < NEWCOMER_DAYS * 86400_000;
}

/** The calm main screen is on: a newcomer with fewer than three sessions. */
export function isCalm(opts: CalmInput): boolean {
  if (opts.sessions >= CALM_MAX_SESSIONS) return false;
  return isNewcomer(opts.status, opts.onboarded, opts.now) === true;
}

/** Sidebar bars a calm screen leaves out by default (Customize brings any of
 * them back): the usage strip, the Assistant, and every extension's bar
 * (Database, Usage optimizer…). Tickets stays — it draws nothing until a
 * tracker is connected, and connecting one is a first-hour thing. */
export const CALM_HIDDEN_BARS = ["usage", "assistant"];

/** Whether a bar key is one the calm default hides. */
export function calmHidesBar(key: string): boolean {
  return CALM_HIDDEN_BARS.includes(key) || key.startsWith("ext:");
}

/** The welcome tour opens by itself exactly once, on a genuinely first
 * launch: never seen on any device (the synced `welcome_tour` pref), and no
 * session ever started here (the setup record's `repo` step — the server's
 * `onboarded` flag until /api/setup answers). Unknown = no: a veteran must
 * never get a tour because a payload is late. */
export function firstLaunchTour(opts: {
  seen: boolean;
  sessions: number;
  status: SetupStatus | null | undefined;
  onboarded?: boolean;
}): boolean {
  if (opts.seen || opts.sessions !== 0) return false;
  if (!opts.status) return opts.onboarded === false;
  const repo = (opts.status.steps || []).find((s) => s.id === "repo");
  return !!repo && !repo.done;
}
