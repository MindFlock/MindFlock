/** A session's behaviors (the Verify redesign, docs/behaviors.md): what the
 * work should do in real use, each checked by the agent or by you, drawn on
 * the session's frame in the Sky.
 *
 * SEAM (pre-committed by the integrator). The signatures below are frozen —
 * the Sky tile, the Behaviors board and the shell code against them. vs-card
 * owns the bodies: the first versions here wrap the old Verify helpers
 * (components/dialogs/verify.ts) so the other workers have real data now. */

import type { Instance, TestPlan } from "../api/types";
import { isFailing, isWaitingOnYou, planForSession as legacyPlanFor, planVerdict, checkTally } from "../components/dialogs/verify";

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

const TONE: Record<string, BehaviorTone> = { ok: "ok", bad: "bad", you: "you", busy: "busy", wait: "quiet", warn: "you" };

export function behaviorSummary(plan: TestPlan): BehaviorSummary {
  const v = planVerdict(plan);
  const t = checkTally(plan);
  const total = (plan.steps || []).length;
  const tone: BehaviorTone = isFailing(plan) ? "bad" : isWaitingOnYou(plan) ? "you" : TONE[v.tone] || "quiet";
  const status =
    tone === "bad" ? "Failed" : tone === "you" ? "Needs you" : tone === "busy" ? (plan.state === "generating" ? "Writing…" : "Checking…")
      : tone === "ok" ? (plan.live_at ? "Verified live" : "Verified") : "Not checked";
  const short =
    tone === "bad" ? "Failed · " + Math.max(1, t.failed) : tone === "you" ? "Needs you · " + Math.max(1, t.yours)
      : tone === "busy" ? status : total ? t.passed + " of " + total + " verified" : status;
  return {
    tone, status, short, sentence: v.label,
    action: null,
    total, verified: t.passed, failed: t.failed, yours: t.yours,
  };
}

/** The behaviors covering one session (by title, then repo + branch), or null. */
export function planForSession(plans: readonly TestPlan[] | null | undefined, row: Pick<Instance, "title" | "branch" | "repo">): TestPlan | null {
  return legacyPlanFor([...(plans || [])], row);
}

/** Behaviors whose session has no frame in the Sky (closed session, another
 * branch, a watched repo's push): the Behaviors board's rows. */
export function orphanPlans(plans: readonly TestPlan[] | null | undefined, rows: readonly Pick<Instance, "title" | "branch" | "repo">[]): TestPlan[] {
  const list = plans || [];
  const owned = new Set<string>();
  for (const r of rows) {
    const p = planForSession(list, r);
    if (p) owned.add(p.id);
  }
  return list.filter((p) => !owned.has(p.id));
}

/** Alt+V's target: the first plan that needs you, else the first that failed. */
export function nextNeedingYou(plans: readonly TestPlan[] | null | undefined): TestPlan | null {
  const list = plans || [];
  return list.find((p) => isWaitingOnYou(p)) || list.find((p) => isFailing(p)) || null;
}
