/** A session's Verify, in one set of words (docs/behaviors.md): the steps its
 * agent checks (or a person does), drawn on the session's frame in the Sky.
 *
 * ONE VOCABULARY. The row's pill (lib/sessionState), the card, the chip, the
 * Ship menu and the bell all say a verify's state with the SAME label
 * constants — `verifyStateLabel` maps the session row's `verify.state`
 * through them — and the same progress phrase ("3 of 5 steps done",
 * `verifyProgress`). When the row carries no `verify` (an older server, or a
 * verify that settled more than ten minutes ago), `behaviorSummary` reads the
 * plan with the old Verify rules (components/dialogs/verify.ts), imported
 * rather than copied, in the same words. */

import type { Instance, SessionVerify, SessionVerifyState, TestPlan } from "../api/types";
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
  isSelfBlocked,
  isVerifySession,
  isWaitingOnYou,
  latestRun,
  openHumanSteps,
  passedBy,
  planForSession as legacyPlanFor,
  stepIsYours,
  unansweredCount,
} from "../components/dialogs/verify";
import { REASON_LABEL, VERIFIED_LABEL, VERIFYING_LABEL, verifyLine, verifyProgress } from "./sessionState";

/** A verify's state in the pill's own words: Verifying · Confirm verified
 * (the needs-you label) · Verified · Verify failed · Verify blocked. */
export function verifyStateLabel(state: SessionVerifyState): string {
  switch (state) {
    case "verifying":
      return VERIFYING_LABEL;
    case "needs_you":
      return REASON_LABEL.verify_confirm;
    case "passed":
      return VERIFIED_LABEL;
    case "failed":
      return REASON_LABEL.verify_failed;
    case "blocked":
      return REASON_LABEL.verify_blocked;
  }
  return VERIFYING_LABEL;
}

/** The one mark per verify state (chip, Ship menu note). */
export const VERIFY_GLYPH: Record<SessionVerifyState, string> = {
  verifying: "\u2026",
  needs_you: "\u25cf",
  passed: "\u2713",
  failed: "\u2717",
  blocked: "!",
};

/** A session's `verify` in the shared words: the state's label and mark, its
 * tone, and its one line ("Verifying — 3 of 5 steps done", "Verify failed —
 * 2 steps", …) — what the chip, the Ship menu and the bell say. */
export function verifyWords(v: SessionVerify): { label: string; glyph: string; tone: BehaviorTone; line: string } {
  const tone: BehaviorTone = v.state === "verifying" ? "busy" : v.state === "needs_you" ? "you" : v.state === "passed" ? "ok" : "bad";
  return { label: verifyStateLabel(v.state), glyph: VERIFY_GLYPH[v.state] || "", tone, line: verifyLine(v) };
}

/** "3 of 5 steps done" for a plan — the pill's phrase (the agent's own steps;
 * a person's are not its to do), counted off the plan's latest run. */
export function planProgress(plan: TestPlan): string {
  const steps = plan.steps || [];
  const run = latestRun(plan);
  const human = steps.filter((s) => stepIsYours(plan, s)).length;
  const done = steps.filter((s) => !stepIsYours(plan, s) && !!run?.results?.[s.id]?.result).length;
  return verifyProgress({ steps: { total: steps.length, human, agent_done: done } });
}

/** The card head's compact summary: "4 passed · 1 failed · 1 needs you"
 * ("" with no steps). */
export function tallyLine(plan: TestPlan): string {
  const t = checkTally(plan);
  return [
    t.passed ? t.passed + " passed" : "",
    t.failed ? t.failed + " failed" : "",
    t.cant ? t.cant + " blocked" : "",
    t.yours ? t.yours + (t.yours === 1 ? " needs" : " need") + " you" : "",
    t.pending ? t.pending + " not done" : "",
  ]
    .filter(Boolean)
    .join(" \u00b7 ");
}

/** Five tones, one per status family. */
export type BehaviorTone = "quiet" | "busy" | "you" | "bad" | "ok";

/** The words one session's Verify wears when its row carries no `verify` (tile, card, board, chip). */
export interface BehaviorSummary {
  tone: BehaviorTone;
  /** One of: Verifying · Not verified · Needs you · Verify failed · Verify blocked · Verified · Verified live */
  status: string;
  /** The tile's sub line: "3 of 5 steps done", "Needs you · 2", "Verify failed · 1", "Verifying". */
  short: string;
  /** The card head's one sentence. */
  sentence: string;
  /** The card head's ONE primary action, or null when there is nothing to press. */
  action: null | { key: "check" | "rerun" | "failed" | "write" | "answer"; label: string };
  total: number;
  verified: number;
  failed: number;
  /** Steps waiting on you. */
  yours: number;
}

type Action = BehaviorSummary["action"];

const CHECK: Action = { key: "check", label: "Verify" };
const RERUN: Action = { key: "rerun", label: "Verify again" };
const FAILED: Action = { key: "failed", label: "See what failed" };
const WRITE: Action = { key: "write", label: "Verify again" };
const ANSWER: Action = { key: "answer", label: "Confirm verified" };

/** "1 step" / "3 steps". */
export function stepsCount(n: number): string {
  return n + (n === 1 ? " step" : " steps");
}

/** A step's text cut to fit the head's sentence, at a word boundary. */
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
    if (earlier) return "Checked before merge (" + beforeMergeSummary(plan, earlier) + ") · checks again once live";
  }
  return "";
}

/** The card's one primary action for "run it": Rerun once an agent has had a
 * go, Verify before. Null when no step is the agent's. */
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
      return { tone: "quiet", sentence: "Writing its steps stopped part-way — nothing for five minutes.", action: WRITE };
    return {
      tone: "busy",
      sentence: isGenerationSlow(plan) ? "Writing the steps — taking longer than usual…" : "Writing the steps from the diff — up to three minutes.",
      action: null,
    };
  }

  if (plan.state === "failed" && !steps.length) {
    if (isNoCommits(plan))
      return { tone: "quiet", sentence: "No steps yet — this branch has no commits. They're written once the session commits.", action: null };
    const why = errorHeadline(plan);
    return {
      tone: "quiet",
      sentence: why ? "The agent couldn't write steps — " + why.charAt(0).toLowerCase() + why.slice(1) : "The agent couldn't write steps for this.",
      action: WRITE,
    };
  }

  // Verify ran and its agent wrote no steps: blocked, not "add some" — the
  // card has no Add, and the way on is Verify again (isSelfBlocked).
  if (isSelfBlocked(plan))
    return {
      tone: "bad",
      status: REASON_LABEL.verify_blocked,
      sentence: "Its agent wrote no steps. Verify again to have it look once more.",
      action: RERUN,
    };
  if (isEmptyByChoice(plan)) return { tone: "quiet", sentence: "No steps yet — Verify has its agent write them and work them.", action: CHECK };
  // (A running Verify with no steps yet is its agent writing them — below.)
  if (!steps.length && plan.state !== "running") return { tone: "quiet", sentence: "The agent came back with no steps.", action: WRITE };

  // A rewrite that failed leaves the old steps standing — say so first.
  const kept = plan.state === "failed" ? "Rewriting didn't work — these steps are unchanged. " : "";

  if (plan.state === "running") {
    if (plan.run_session) {
      const mine = openHumanSteps(plan).length;
      return {
        tone: "busy",
        sentence:
          "Its agent is checking its work: " +
          planProgress(plan) +
          (mine ? " — " + stepsCount(mine) + (mine === 1 ? " needs" : " need") + " you after." : "."),
        action: null,
      };
    }
    return { tone: "quiet", sentence: "The agent that was verifying this is gone — nothing is running now.", action: runAction(plan) };
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
        (first ? "Failed: “" + clip(first.text) + "”" : "A step failed") +
        (failed > 1 ? " (+" + (failed - 1) + " more)" : "") +
        "." +
        (open ? " " + stepsCount(open) + " still " + (open === 1 ? "needs" : "need") + " you." : ""),
      action: FAILED,
    };
  }

  if (!run) {
    if (isConfigBlocked(plan)) return { tone: "you", sentence: kept + plan.live_problem, action: runAction(plan) };
    if (plan.live_at || plan.state === "due") {
      if (!agentCan)
        return {
          tone: "you",
          sentence: kept + (steps.length === 1 ? "Live — its one step is yours to do." : "Live — all " + steps.length + " steps are yours to do."),
          action: ANSWER,
        };
      return {
        tone: "you",
        sentence:
          kept +
          "Live — nobody has verified it yet. " +
          (agentCan === steps.length
            ? "The agent can do " + (steps.length === 1 ? "its step." : "all " + steps.length + " steps.")
            : "The agent can do " + agentCan + " of " + steps.length + " steps; the rest are yours."),
        action: CHECK,
      };
    }
    if (plan.merged_at) return { tone: "quiet", sentence: kept + "Merged " + agoText(plan.merged_at) + " — waiting for it to deploy.", action: null };
    if (plan.live_problem) return { tone: "quiet", sentence: kept + plan.live_problem, action: agentCan ? CHECK : null };
    return {
      tone: "quiet",
      sentence: kept + "Not merged yet — verify the branch now, or it's verified once it reaches " + (plan.live_branch || "the live branch") + ".",
      action: agentCan ? CHECK : null,
    };
  }

  const open = openHumanSteps(plan).length;
  if (!open && isWaitingOnYou(plan))
    return { tone: "you", sentence: kept + "Live — not verified since it shipped.", action: runAction(plan) || ANSWER };
  if (open)
    return {
      tone: "you",
      sentence: kept + (open === 1 ? "1 step needs you — an agent can't judge it." : stepsCount(open) + " need you — an agent can't judge them."),
      action: ANSWER,
    };

  const missing = unansweredCount(plan);
  const cant = cantCheckCount(plan);
  if (missing) {
    const passed = steps.length - missing - failCount(plan);
    const blank = missing - cant;
    const bits = [
      passed ? passed + " of " + steps.length + " steps passed" : "",
      cant ? cant + " blocked" : "",
      blank ? (run.session || run.by === "agent" ? "verifying stopped early — " : "") + blank + " with no answer" : "",
    ].filter(Boolean);
    const line = bits.join(" · ");
    return { tone: "quiet", sentence: kept + line.charAt(0).toUpperCase() + line.slice(1) + ".", action: runAction(plan) };
  }

  const by = passedBy(plan);
  return { tone: "ok", sentence: kept + "Every step passed" + (by ? " — checked by " + by : "") + ".", action: null };
}

export function behaviorSummary(plan: TestPlan): BehaviorSummary {
  const t = checkTally(plan);
  const s = said(plan);
  const status =
    s.status ||
    (s.tone === "bad"
      ? REASON_LABEL.verify_failed
      : s.tone === "you"
        ? "Needs you"
        : s.tone === "ok"
          ? VERIFIED_LABEL + (plan.live_at ? " live" : "")
          : s.tone === "busy"
            ? VERIFYING_LABEL
            : "Not verified");
  const total = (plan.steps || []).length;
  const short =
    s.tone === "bad"
      ? t.failed
        ? REASON_LABEL.verify_failed + " · " + t.failed
        : status
      : s.tone === "you"
        ? t.yours
          ? "Needs you · " + t.yours
          : "Needs you"
        : s.tone === "busy" || s.tone === "ok"
          ? status
          : !total
            ? "No steps yet"
            : t.passed
              ? t.passed + " of " + total + (total === 1 ? " step" : " steps") + " done"
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

/** Is there a Behaviors stop on any route in the Sky — a shown session whose
 * behaviors its route draws (the Verify stop, or the tile standing in for
 * it)? What first opens the Verify tour (SkyView). */
export function behaviorsOnARoute(
  plans: readonly TestPlan[] | null | undefined,
  rows: readonly Pick<Instance, "title" | "branch" | "repo">[],
  titles: readonly string[]
): boolean {
  if (!plans?.length) return false;
  const shown = new Set(titles);
  return rows.some((r) => shown.has(r.title) && !!planForSession(plans, r));
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
