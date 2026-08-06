/** The autopilot depth ladder, client side — pure, so it is unit-testable.
 *
 * Mirrors backend/web/core/autopilot.py. The rungs are TARGETS ("carry it as far
 * as Open PR"); the session stages they are compared against are past-tense facts
 * ("committed", "pushed"). Keeping the two vocabularies distinct, and mapping
 * between them in one place, is what stops "am I there yet?" turning into a pile
 * of special cases.
 */

import type { AutopilotRun, Instance, MergeState } from "../api/types";

export const DEPTH_ORDER = ["off", "agent", "commit", "push", "pr", "merge"] as const;

/** Rungs a caller may arm. "off" is the absence of a run, not a target. */
export const DEPTHS = ["agent", "commit", "push", "pr", "merge"] as const;

/** Rungs the per-session fast-track button offers. "agent" is intake-only —
 * arming it on a session that already exists would mean "do nothing". */
export const SESSION_DEPTHS = ["commit", "push", "pr", "merge"] as const;

/** Rungs a whole intake SOURCE may default to. Merge is deliberately absent: a
 * source default applies to every future item with no human in the loop, and
 * merging is the one step in the ladder that cannot be undone. Merge is still
 * available on an individual item. */
export const SOURCE_DEPTHS = ["agent", "commit", "push", "pr"] as const;

export const DEPTH_LABELS: Record<string, string> = {
  off: "Off",
  agent: "Agent only",
  commit: "Commit",
  push: "Push",
  pr: "Open PR",
  merge: "Merge",
};

/** How the ladder reads in a dropdown. No leading "…": inside a CLOSED select a
 * leading ellipsis reads as text that has been clipped. */
export const DEPTH_STEP_LABELS: Record<string, string> = {
  off: "Off",
  agent: "Agent only",
  commit: "Commit only",
  push: "Then push",
  pr: "Then open PR",
  merge: "Then merge",
};

/** Ultra-short target suffix for the pane header, where width is scarce. */
export const DEPTH_SHORT: Record<string, string> = {
  agent: "→ agent",
  commit: "→ commit",
  push: "→ push",
  pr: "→ PR",
  merge: "→ merge",
};

/** Which rung a given session stage PROVES is complete. Stages that mean "still
 * working" (provisioning/agent/precommit/interrupt) prove nothing. */
const STAGE_ACHIEVED: Record<string, string> = {
  committed: "commit",
  pushed: "push",
  pr: "pr",
};

export function depthLabel(depth: string): string {
  return DEPTH_LABELS[depth] || depth || "Off";
}

export function normalizeDepth(value: string | null | undefined): string {
  const d = String(value || "")
    .trim()
    .toLowerCase();
  if (d === "off") return "off";
  return (DEPTHS as readonly string[]).includes(d) ? d : "";
}

/** Whether `stage` already satisfies the target `depth`.
 *
 * `merge` is never satisfied by a stage: the server has no "merged" stage (a
 * merged PR moves the stage OFF "pr"), so that rung completes when the merge
 * call returns ok, not by observation. */
export function atOrPastDepth(stage: string, depth: string): boolean {
  const d = normalizeDepth(depth);
  if (!d || d === "off" || d === "merge") return false;
  const got = STAGE_ACHIEVED[String(stage || "")];
  if (!got) return false;
  return DEPTH_ORDER.indexOf(got as never) >= DEPTH_ORDER.indexOf(d as never);
}

/** The chip text for a live or finished run: short enough for a pane header. */
export function autopilotChipLabel(run: AutopilotRun): string {
  const target = depthLabel(run.depth);
  if (run.state === "halted") return "auto ✗";
  if (run.state === "done") return "auto ✓";
  return "auto → " + target;
}

/** The tooltip: what it is doing, or why it stopped. */
export function autopilotChipTitle(run: AutopilotRun): string {
  const target = depthLabel(run.depth);
  if (run.state === "halted")
    return "Fast-track stopped: " + (run.reason || "unknown reason");
  if (run.state === "done") return "Fast-track finished at " + target;
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
  return "Fast-tracking to " + target + where + "\nClick to stop." + skipped;
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

/** A running chain on this session, or null. */
export function liveRun(inst: Partial<Instance>): AutopilotRun | null {
  const run = inst.autopilot;
  if (!run || !run.depth) return null;
  return run.state === "running" ? run : null;
}
