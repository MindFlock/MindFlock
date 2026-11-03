/** The Behaviors card's decisions, kept pure (vitest here is node-only — no
 * DOM — so everything worth pinning lives outside the components): which ⋯
 * items a card offers, what a row's chip says, who checks a behavior, which
 * key does what, and where the primary action lands. */

import type { TestPlan, TestStep, TestStepResult, TestStepResultEntry } from "../../api/types";
import {
  CHECK_MARK,
  isFailing,
  isYourAnswer,
  latestRun,
  openHumanSteps,
  rewriteBlockedReason,
  stepCheck,
  stepIsYours,
  stepKeyAction,
  stepKeyIsUndo,
  stepResult,
  type CheckState,
} from "../dialogs/verify";
import type { BehaviorSummary } from "../../lib/behaviors";
import type { Flight } from "../../lib/flights";

/** The ⋯ menu's keys (docs/behaviors.md §2), plus the old row's extras that
 * must stay reachable (run again, What could break, Open PR). A Verify
 * stop's "Skip this stop" is a head button, not a menu item. */
export type CardMenuKey =
  | "rewrite"
  | "deployed"
  | "fix"
  | "run"
  | "watch"
  | "cancel"
  | "session"
  | "diff"
  | "risk"
  | "pr"
  | "delete";

export interface CardMenuItem {
  key: CardMenuKey;
  label: string;
  title: string;
  danger?: boolean;
  disabled?: boolean;
}

export interface CardMenuCtx {
  summary: BehaviorSummary;
  /** The open work session this plan belongs to, if any. */
  session?: { title: string; pr_url?: string | null } | null;
}

/** The session a run of this plan lives (or lived) in, or "". */
export function watchSessionOf(plan: TestPlan): string {
  return plan.run_session || latestRun(plan)?.session || "";
}

/** Every applicable ⋯ item, in the contract's order. The primary action is
 * not repeated here. */
export function cardMenuItems(plan: TestPlan, ctx: CardMenuCtx): CardMenuItem[] {
  const items: CardMenuItem[] = [];
  const steps = plan.steps || [];
  const running = plan.state === "running";
  const primary = ctx.summary.action?.key || "";
  const agentCan = steps.some((s) => s.actor !== "human");
  if (plan.state !== "generating" && primary !== "write") {
    const blocked = rewriteBlockedReason(plan);
    items.push({ key: "rewrite", label: "Rewrite behaviors", title: blocked || "Ask the agent to write them again — you can say what to focus on", disabled: !!blocked });
  }
  if (plan.state === "generated" && plan.merged_at && !plan.live_at)
    items.push({ key: "deployed", label: "It's deployed — check now", title: "Don't wait out the deploy timer" });
  if (isFailing(plan))
    items.push({ key: "fix", label: "Fix in a new session", title: "Starts a new session with what failed attached" });
  if (!running && steps.length && plan.state !== "generating" && agentCan && primary !== "check" && primary !== "rerun")
    items.push({ key: "run", label: latestRun(plan) ? "Rerun" : "Check now", title: "The agent checks the behaviors it can" });
  const watch = watchSessionOf(plan);
  if (watch) items.push({ key: "watch", label: "Open the checking run", title: "Open " + watch + " as a pane — closing it doesn't stop the check" });
  if (running) items.push({ key: "cancel", label: "Cancel the check", title: "Stop the agent — what it found so far is kept" });
  if (ctx.session) {
    items.push({ key: "session", label: "Open the work session", title: "Show " + ctx.session.title + " in the Sky" });
    items.push({ key: "diff", label: "View diff", title: "Show what changed on " + (plan.branch || "this branch") });
    items.push({ key: "risk", label: "What could break", title: "Open the map on what imports these changes" });
    if (ctx.session.pr_url) items.push({ key: "pr", label: "Open PR ↗", title: ctx.session.pr_url });
  }
  items.push({ key: "delete", label: "Delete these behaviors", title: "Delete these behaviors and their answers", danger: true });
  return items;
}

/** A row's result chip: the glyph and the words. */
export const CHIP_WORDS: Record<CheckState, string> = {
  pass: "Passed",
  fail: "Failed",
  cant: "Couldn't check",
  yours: "Needs you",
  pending: "Not checked",
};

export function rowChip(plan: TestPlan, stepId: string): { state: CheckState; glyph: string; label: string } {
  const state = stepCheck(plan, stepId);
  return { state, glyph: CHECK_MARK[state], label: CHIP_WORDS[state] };
}

/** Who checks one behavior, as the toggle shows it. A behavior the agent
 * handed back is yours now (stepIsYours). */
export function whoOf(plan: TestPlan, step: TestStep): { you: boolean; label: string; title: string; handed: boolean } {
  const you = stepIsYours(plan, step);
  const handed = you && step.actor !== "human";
  return {
    you,
    handed,
    label: you ? "🙋 you" : "🤖 agent",
    title: handed
      ? "The agent couldn't check this one — it's yours now"
      : you
        ? "Checked by you — press to let the agent check it"
        : "Checked by the agent — press to check it yourself",
  };
}

/** "you" / "the agent" — who recorded an answer. */
export function byWords(by: string): string {
  return by === "human" ? "you" : "the agent";
}

/** Whether the row's answer buttons show: behaviors checked by you, and any
 * behavior a person already answered (so it can be changed or undone). */
export function showsAnswers(plan: TestPlan, step: TestStep, entry: TestStepResultEntry | null = stepResult(plan, step.id)): boolean {
  return stepIsYours(plan, step) || entry?.by === "human";
}

/** What a key on a focused row does: an answer, "undo", "note", or "". */
export function rowKey(key: string, opts: { answered: boolean; mods?: boolean; repeat?: boolean; busy?: boolean }): TestStepResult | "undo" | "note" | "" {
  if (opts.mods || opts.repeat || opts.busy) return "";
  if (stepKeyIsUndo(key)) return opts.answered ? "undo" : "";
  const a = stepKeyAction(key);
  return a === "" ? "" : a;
}

/** Whether a row holds an answer Undo can take back: a pass or fail by
 * anyone, or your own Can't check (an agent's "blocked" is a hand-back, not
 * an answer — see isYourAnswer). */
export function rowAnswered(entry: TestStepResultEntry | null | undefined): boolean {
  return !!entry && isYourAnswer(entry.result, entry.by);
}

/** Where the primary action lands (the row to focus) for "failed" / "answer". */
export function primaryTarget(plan: TestPlan, key: string): string {
  const run = latestRun(plan);
  const steps = plan.steps || [];
  if (key === "failed") return steps.find((s) => run?.results?.[s.id]?.result === "fail")?.id || "";
  if (key === "answer") return openHumanSteps(plan)[0]?.id || steps.find((s) => stepCheck(plan, s.id) === "yours")?.id || steps[0]?.id || "";
  return "";
}

/** What the note composer holds after an answer — the old rule
 * (noteDraftAfter) without its stale-closure bug: Fail / Can't check open it
 * empty, a saved note closes it. */
export { noteDraftAfter } from "../dialogs/verify";

/** Where the Verify stop this card belongs to stands on its route
 * (docs/behaviors.md §5): `halted` — its behaviors failed and the route waits
 * at it (Retry · Skip · Let the agent fix it); `you` — behaviors wait on you
 * (the route is on Needs you; it moves on once they are answered, or on
 * Skip); `fixing` — the agent is on the failure; `""` otherwise. Pure, so the
 * buttons it picks are pinned by a test. */
export type VerifyStopStand = "halted" | "you" | "fixing" | "";

export function verifyStopStand(
  flight: Pick<Flight, "state" | "steps" | "cursor" | "step_states"> | null | undefined,
  stepId: string,
): VerifyStopStand {
  if (!flight) return "";
  const cur = flight.steps[flight.cursor];
  const rec = flight.step_states?.[stepId];
  if (!cur || cur.id !== stepId || !rec) return "";
  if (flight.state === "halted" && rec.state === "failed") return "halted";
  if (rec.state === "running" && (rec.detail as { phase?: string } | undefined)?.phase === "fix") return "fixing";
  if (flight.state === "waiting_you" && rec.state === "waiting_you") return "you";
  return "";
}

/** The halted Verify stop's line on its card: the failed count leads, as on
 * its face ("Failed · 2 — halted"). */
export function haltedLine(failed: number): string {
  return (failed > 0 ? "Failed · " + failed + " — halted" : "Halted") + ". The route waits here until you Retry, Skip or let the agent fix it.";
}
