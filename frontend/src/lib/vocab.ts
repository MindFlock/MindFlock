/** One sentence per concept, for the places that describe it in passing
 * (top-bar tooltip, palette, tour, shortcut sheet) — so a newcomer reads the
 * same words wherever they meet it. */

import { LANE_CHOICES, LANE_LABEL, type Lane } from "./autopilot";

/** What Verify is for. */
export const VERIFY_BLURB = "check that pushed changes work";

/** Fast-track's rungs, in order, with the one label each wears on every
 * screen that offers them (the New form, the ⏩ popover, the Schedule form):
 * "Off / Commit / Push / Open PR / Merge when green". Built from the ladder's
 * own label table so the two can never drift. */
export const FAST_TRACK_LEVELS: ReadonlyArray<{ lane: Lane; label: string }> = LANE_CHOICES.map(
  (lane) => ({ lane, label: LANE_LABEL[lane] })
);

/** The launch-flag chip that turns off the agent's permission prompts
 * (`--dangerously-skip-permissions` and its kin), wherever it is offered —
 * the New form's Launch flags and Settings → Agents. */
export const SKIP_PERMS_LABEL = "⚠ Skip permission prompts";
export const SKIP_PERMS_TITLE = "The agent runs commands and edits files without asking you";

/** Why a SHIP item is greyed: nothing has changed yet. */
export const NOTHING_TO_PUSH = "Nothing to push yet";
/** Why a PR item can't do it for you: GitHub isn't connected. */
export const CONNECT_GITHUB_FOR_PRS = "Connect GitHub to open PRs";
