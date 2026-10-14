/** THE Fast-track ladder, client side — pure, so it is unit-testable.
 *
 * ONE ladder and ONE label table for every Fast-track control: the pane's ⏩,
 * the row › menu, New's Options, the Commit dialog and Intake's pickers all
 * read Off / Commit / Push / Open PR / Merge when green from here
 * (lib/laneActions re-exports it for the controls that act).
 *
 * The wire has two spellings of the bottom rung and both stay accepted: the
 * lane routes and a row's `lane` say "leave", the autopilot / intake routes
 * (and MCP set_autopilot) say "off"; an older intake config may say "agent"
 * (stop once the agent stops — which is what Off already does to a session).
 * normalizeLane folds all three into "leave". Nothing is migrated: each
 * control keeps EMITTING the spelling its route already stores — the lane
 * controls "leave", Intake's pickers "off" / "".
 *
 * The rungs are TARGETS ("carry it as far as Open PR"); the session stages
 * they are compared against are past-tense facts ("committed", "pushed").
 * Mirrors backend/web/core/autopilot.py. */

import type { AutopilotRun, MergeState } from "../api/types";

/** A Fast-track target, keyed the way the lane routes name it ("leave" = Off). */
export type Lane = "leave" | "commit" | "push" | "pr" | "merge";

/** The rungs in ladder order. Every fast-track control offers all five —
 * Push included: "pushed, no PR" is a real place to stop, and leaving it out
 * of one control was how the same choice read differently in two places. */
export const LANE_ORDER: readonly Lane[] = ["leave", "commit", "push", "pr", "merge"];

/** What every fast-track control offers, nearest first. */
export const LANE_CHOICES: readonly Lane[] = LANE_ORDER;

/** Rungs Intake's per-item picker offers beside its Default / Off choices. */
export const SESSION_DEPTHS: readonly Lane[] = ["commit", "push", "pr", "merge"];

/** Rungs a whole intake SOURCE may default to. Merge is deliberately absent: a
 * source default applies to every future item with no human in the loop, and
 * merging is the one step in the ladder that cannot be undone. Merge is still
 * available on an individual item. */
export const SOURCE_DEPTHS: readonly Lane[] = ["commit", "push", "pr"];

/** THE user-facing word for each rung. */
export const LANE_LABEL: Record<Lane, string> = {
  leave: "Off",
  commit: "Commit",
  push: "Push",
  pr: "Open PR",
  merge: "Merge when green",
};

/** The ⏩ button's own text for each rung, where a pane head has no room. */
export const LANE_SHORT: Record<Lane, string> = {
  leave: "off",
  commit: "Commit",
  push: "Push",
  pr: "PR",
  merge: "Merge",
};

/** What each rung means, in one line: the picker's second line and the
 * segmented control's tooltips. */
export const LANE_DESC: Record<Lane, string> = {
  leave: "MindFlock doesn't commit anything — you take it from there",
  commit: "Commit with a message written from the diff once the agent stops and your hooks pass",
  push: "Commit and push once the agent stops and your hooks pass",
  pr: "Commit, push and open a PR once the agent stops and your hooks pass",
  merge: "Commit, push, open a PR and merge it once its checks pass",
};

/** The picker's letters: Off, Commit, pUsh, PR, Merge (A is "ask first"). */
export const LANE_KEY: Record<Lane, string> = {
  leave: "O",
  commit: "C",
  push: "U",
  pr: "P",
  merge: "M",
};

/** A target from anything the server or a setting might hand us: "leave",
 * "off", "agent" and "none" are all Off. Unknown → "". */
export function normalizeLane(v: string | null | undefined): Lane | "" {
  const s = String(v || "")
    .trim()
    .toLowerCase();
  if (s === "agent" || s === "off" || s === "none") return "leave";
  return (LANE_ORDER as readonly string[]).includes(s) ? (s as Lane) : "";
}

/** THE default for batches and ticket runs: Settings → Workspace
 * "Fast-track goes as far as" (`/api/config` fasttrack_default). Out of the
 * box — unset, blank or unknown — it is Off: nothing is fast-tracked unless
 * someone asked. A single new session never reads it (runStart.defaultLaneFor
 * starts it Off). */
export function laneDefault(setting: string | null | undefined): Lane {
  return normalizeLane(setting) || "leave";
}

/** Any wire spelling of a rung, in the ladder's word. Empty is Off; an
 * unknown value is shown as-is rather than guessed at. */
export function laneLabel(value: string | null | undefined): string {
  if (!String(value || "").trim()) return LANE_LABEL.leave;
  const lane = normalizeLane(value);
  return lane ? LANE_LABEL[lane] : String(value);
}

/** The tooltip: what it is doing, or why it stopped. */
export function autopilotChipTitle(run: AutopilotRun): string {
  const target = laneLabel(run.depth);
  if (run.state === "halted")
    // A halt's `note` is set only when the agent was sent a fix-it note.
    return "Fast-track stopped: " + (run.reason || "unknown reason") + (run.note ? "\n" + run.note : "");
  if (run.state === "done")
    return "Fast-track finished at " + target + " and switched itself off.";
  // Prefer the server's own sentence for what this pass is waiting on — it knows
  // whether it is the agent, the prompt queue, checks or a usage limit. Guessing
  // from an empty `step` made every legitimate pause read as "waiting on the
  // agent", so a deliberate wait looked like a hang.
  const where = run.note
    ? " — " + run.note
    : run.step
      ? " (last step: " + run.step + ")"
      : " (waiting to start)";
  const skipped = run.skipped?.length
    ? "\nSkipped hooks: " + run.skipped.join(", ")
    : "";
  return "Fast-tracking to " + target + where + skipped;
}

/** A short header label naming WHY a PR cannot merge.
 *
 * The full sentences live in `merge_state.blockers` and go in the tooltip; this is
 * the ≤14-char version for a pane header where "PR open" would be true but
 * useless when what you want to know is why Merge is greyed out. */
export function mergeBlockerLabel(ms: MergeState): string {
  switch (ms.state) {
    case "dirty":
      return "conflicts";
    case "behind":
      return "behind base";
    case "draft":
      return "draft PR";
    case "blocked":
      if (ms.checks === "failed") return "checks ✗";
      if (ms.checks === "pending") return "checks…";
      return "review needed";
    default:
      return ms.mergeable === null ? "checking…" : "can't merge";
  }
}
