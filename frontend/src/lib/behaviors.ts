/** A session's behaviors (the Verify redesign, docs/behaviors.md): what the
 * work should do in real use, each checked by the agent or by you, drawn on
 * the session's frame in the Sky.
 *
 * SEAM (pre-committed by the integrator). The signatures below are frozen —
 * the Sky tile, the Behaviors board and the shell code against them. vs-card
 * owns the bodies. The decisions are the old Verify rules
 * (components/dialogs/verify.ts: isFailing, isWaitingOnYou, latestRun,
 * openHumanSteps, …), imported rather than copied, so the tile, the card, the
 * board and the old verdict chip cannot disagree about a plan; only the WORDS
 * are new (docs/behaviors.md "Words"). */

import type { Instance, TestPlan } from "../api/types";
import {
  agoText,
  beforeMergeRun,
  beforeMergeSummary,
  cantCheckCount,
  checkTally,
  errorHeadline,
  failCount,
  isBeforeMerge,
  isConfigBlocked,
  isEmptyByChoice,
  isFailing,
  isGenerationSlow,
  isGenerationStalled,
  isNoCommits,
  isVerifySession,
  isWaitingOnYou,
  latestRun,
  openHumanSteps,
  passedBy,
  planForSession as legacyPlanFor,
  runElapsed,
  unansweredCount,
} from "../components/dialogs/verify";

/** Five tones, one per status family. */
export type BehaviorTone = "quiet" | "busy" | "you" | "bad" | "ok";

/** The words one session's behaviors wear everywhere (tile, card, board, chip). */
export interface BehaviorSummary {
  tone: BehaviorTone;
  /** One of: Writing… · Not checked · Checking… · Needs you · Failed · Verified · Verified live */
  status: string;
  /** The tile's sub line: "3 of 5 verified", "Needs you · 2", "Failed · 1", "Writing…". */
  short: string;
  /** The card head's one sentence. */
  sentence: string;
  /** The card head's ONE primary action, or null when there is nothing to press. */
  action: null | { key: "check" | "rerun" | "failed" | "write" | "answer"; label: string };
  total: number;
  verified: number;
  failed: number;
  /** Behaviors waiting on you. */
  yours: number;
}

type Action = BehaviorSummary["action"];

const CHECK: Action = { key: "check", label: "Check now" };
const RERUN: Action = { key: "rerun", label: "Rerun" };
const FAILED: Action = { key: "failed", label: "See what failed" };
const WRITE: Action = { key: "write", label: "Write them" };
const ANSWER: Action = { key: "answer", label: "Answer" };

/** "1 behavior" / "3 behaviors". */
export function behaviorsCount(n: number): string {
  return n + (n === 1 ? " behavior" : " behaviors");
}

/** A behavior's text cut to fit the head's sentence, at a word boundary. */
function clip(text: string, max = 70): string {
  const t = String(text || "").trim().replace(/\s+/g, " ").replace(/[.]$/, "");
  if (t.length <= max) return t;
  const cut = t.slice(0, max);
  const space = cut.lastIndexOf(" ");
  return (space > max / 2 ? cut.slice(0, space) : cut).replace(/[,;:.]$/, "") + "…";
}

/** The honest timing line, or "" (docs/behaviors.md §2): a check made on the
 * branch before merge is not a check of the live work. Two cases:
 * - the current answer IS a pre-merge run (not shipped yet) →
 *   "Checked before merge · checks again once live";
 * - it has shipped and the only check on record was before merge (latestRun
 *   skips it then) → the same line, naming what that check found. */
export function timingLine(plan: TestPlan): string {
  const run = latestRun(plan);
  if (run && isBeforeMerge(run) && !plan.live_at) return "Checked before merge · checks again once live";
  if (!run && plan.live_at) {
    const earlier = beforeMergeRun(plan);
    if (earlier) return "Checked before merge (" + beforeMergeSummary(plan, earlier).replace(/steps?/g, (m) => (m === "step" ? "behavior" : "behaviors")) + ") · checks again once live";
  }
  return "";
}

/** The card's one primary action for "run it": Rerun once an agent has had a
 * go, Check now before. Null when no behavior is the agent's. */
function runAction(plan: TestPlan): Action {
  const agentCan = (plan.steps || []).some((s) => s.actor !== "human");
  if (!agentCan) return null;
  const run = latestRun(plan);
  return run && (run.session || run.by === "agent") ? RERUN : CHECK;
}

interface Said {
  tone: BehaviorTone;
  sentence: string;
  action: Action;
  /** Overrides the tone's own status word (Writing… vs Checking…). */
  status?: string;
}

/** The ordered decision — the old `planStatus` order, in the new words. The
 * loud honest answers (it failed, it waits on you) come first so a cheerful
 * one below can never shadow them. */
function said(plan: TestPlan): Said {
  const steps = plan.steps || [];
  const agentCan = steps.filter((s) => s.actor !== "human").length;

  if (plan.state === "generating") {
    if (isGenerationStalled(plan))
      return { tone: "quiet", sentence: "Writing stopped part-way — nothing for five minutes.", action: WRITE };
    return {
      tone: "busy",
      status: "Writing…",
      sentence: isGenerationSlow(plan)
        ? "Writing the behaviors — taking longer than usual…"
        : "Writing the behaviors from the diff — up to three minutes.",
      action: null,
    };
  }

  if (plan.state === "failed" && !steps.length) {
    if (isNoCommits(plan))
      return { tone: "quiet", sentence: "No behaviors yet — this branch has no commits. They're written once the session commits.", action: null };
    const why = errorHeadline(plan);
    return {
      tone: "quiet",
      sentence: why ? "The agent couldn't write behaviors — " + why.charAt(0).toLowerCase() + why.slice(1) : "The agent couldn't write behaviors for this.",
      action: WRITE,
    };
  }

  if (isEmptyByChoice(plan)) return { tone: "you", sentence: "No behaviors yet — add what this work should do.", action: WRITE };
  if (!steps.length) return { tone: "quiet", sentence: "The agent came back with no behaviors.", action: WRITE };

  // A rewrite that failed leaves the old behaviors standing — say so first.
  const kept = plan.state === "failed" ? "Rewriting didn't work — these are unchanged. " : "";

  if (plan.state === "running") {
    if (plan.run_session) {
      const mine = openHumanSteps(plan).length;
      return {
        tone: "busy",
        status: "Checking…",
        sentence:
          "An agent is checking the behaviors it can" +
          runElapsed(latestRun(plan)) +
          (mine ? " — " + behaviorsCount(mine) + (mine === 1 ? " needs" : " need") + " you." : "."),
        action: null,
      };
    }
    return { tone: "quiet", sentence: "The agent that was checking these is gone — nothing is running now.", action: runAction(plan) };
  }

  const run = latestRun(plan);
  if (isFailing(plan) && run) {
    const failed = failCount(plan);
    const first = steps.find((s) => run.results?.[s.id]?.result === "fail");
    const open = openHumanSteps(plan).length;
    return {
      tone: "bad",
      sentence:
        kept +
        (first ? "Failed: “" + clip(first.text) + "”" : "Failed") +
        (failed > 1 ? " (+" + (failed - 1) + " more)" : "") +
        "." +
        (open ? " " + behaviorsCount(open) + " still " + (open === 1 ? "needs" : "need") + " you." : ""),
      action: FAILED,
    };
  }

  if (!run) {
    if (isConfigBlocked(plan)) return { tone: "you", sentence: kept + plan.live_problem, action: runAction(plan) };
    if (plan.live_at || plan.state === "due") {
      if (!agentCan)
        return {
          tone: "you",
          sentence: kept + (steps.length === 1 ? "Live — its one behavior is yours to check." : "Live — all " + steps.length + " behaviors are yours to check."),
          action: ANSWER,
        };
      return {
        tone: "you",
        sentence:
          kept +
          "Live — nobody has checked it yet. " +
          (agentCan === steps.length
            ? "The agent can check " + (steps.length === 1 ? "it." : "all " + steps.length + ".")
            : "The agent can check " + agentCan + " of " + steps.length + "; the rest are yours."),
        action: CHECK,
      };
    }
    if (plan.merged_at) return { tone: "quiet", sentence: kept + "Merged " + agoText(plan.merged_at) + " — waiting for it to deploy.", action: null };
    if (plan.live_problem) return { tone: "quiet", sentence: kept + plan.live_problem, action: agentCan ? CHECK : null };
    return {
      tone: "quiet",
      sentence: kept + "Not merged yet — check the branch now, or they're checked once it reaches " + (plan.live_branch || "the live branch") + ".",
      action: agentCan ? CHECK : null,
    };
  }

  const open = openHumanSteps(plan).length;
  if (!open && isWaitingOnYou(plan))
    return { tone: "you", sentence: kept + "Live — not checked since it shipped.", action: runAction(plan) || ANSWER };
  if (open)
    return {
      tone: "you",
      sentence: kept + (open === 1 ? "1 behavior needs you — an agent can't judge it." : behaviorsCount(open) + " need you — an agent can't judge them."),
      action: ANSWER,
    };

  const missing = unansweredCount(plan);
  const cant = cantCheckCount(plan);
  if (missing) {
    const passed = steps.length - missing - failCount(plan);
    const blank = missing - cant;
    const bits = [
      passed ? passed + " of " + steps.length + " verified" : "",
      cant ? "you couldn't check " + cant : "",
      blank ? (run.session || run.by === "agent" ? "the check stopped early — " : "") + blank + " with no answer" : "",
    ].filter(Boolean);
    const line = bits.join(" · ");
    return { tone: "quiet", sentence: kept + line.charAt(0).toUpperCase() + line.slice(1) + ".", action: runAction(plan) };
  }

  const by = passedBy(plan);
  return { tone: "ok", sentence: kept + "Every behavior passed" + (by ? " — checked by " + by : "") + ".", action: null };
}

export function behaviorSummary(plan: TestPlan): BehaviorSummary {
  const t = checkTally(plan);
  const s = said(plan);
  const status =
    s.status ||
    (s.tone === "bad" ? "Failed" : s.tone === "you" ? "Needs you" : s.tone === "ok" ? (plan.live_at ? "Verified live" : "Verified") : s.tone === "busy" ? "Checking…" : "Not checked");
  const total = (plan.steps || []).length;
  const short =
    s.tone === "bad"
      ? "Failed · " + Math.max(1, t.failed)
      : s.tone === "you"
        ? t.yours
          ? "Needs you · " + t.yours
          : "Needs you"
        : s.tone === "busy" || s.tone === "ok"
          ? status
          : !total
            ? "None yet"
            : t.passed
              ? t.passed + " of " + total + " verified"
              : status;
  const timing = timingLine(plan);
  // Errors carry a self-describing first sentence; the head shows it after
  // the status when it is not already the status (the old planStatus rule).
  const headline = plan.state !== "failed" && plan.state !== "generating" && total ? errorHeadline(plan) : "";
  const sentence = [s.sentence, headline, timing ? timing + "." : ""].filter(Boolean).join(" ");
  return { tone: s.tone, status, short, sentence, action: s.action, total, verified: t.passed, failed: t.failed, yours: t.yours };
}

/** The behaviors covering one session (by title, then repo + branch), or null.
 * A verify run's own session never owns behaviors — it is checking them. */
export function planForSession(plans: readonly TestPlan[] | null | undefined, row: Pick<Instance, "title" | "branch" | "repo">): TestPlan | null {
  if (!row || isVerifySession(row)) return null;
  return legacyPlanFor([...(plans || [])], row);
}

/** Behaviors whose session has no frame in the Sky (closed session, another
 * branch, a watched repo's push): the Behaviors board's rows. */
export function orphanPlans(plans: readonly TestPlan[] | null | undefined, rows: readonly Pick<Instance, "title" | "branch" | "repo">[]): TestPlan[] {
  const list = plans || [];
  const owned = new Set<string>();
  for (const r of rows || []) {
    const p = planForSession(list, r);
    if (p) owned.add(p.id);
  }
  return list.filter((p) => !owned.has(p.id));
}

/** Alt+V's target: the first plan that needs you, else the first that failed. */
export function nextNeedingYou(plans: readonly TestPlan[] | null | undefined): TestPlan | null {
  const list = plans || [];
  return list.find((p) => isWaitingOnYou(p) && !isFailing(p)) || list.find((p) => isFailing(p)) || null;
}

/** A Verify stop's face line (docs/behaviors.md §5): the behaviors' summary,
 * and "— halted" when its failure halted the route ("Failed · 2 — halted").
 * `stopState` is the diagram node's state (lib/flightDiagram). The face is
 * drawn by lib/flightDiagram `withBehaviors` / SkyChain, which are not this
 * module's: they call this (docs/vision/verify.md). */
export function verifyStopFace(short: string, stopState: string): string {
  return stopState === "halted" ? short + " — halted" : short;
}
