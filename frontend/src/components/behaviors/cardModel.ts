/** The Verify card's decisions, kept pure (vitest here is node-only — no
 * DOM — so everything worth pinning lives outside the components): what the
 * head says and its one action, which ⋯ items a card offers, what a row's
 * chip says and who does each step.
 *
 * VERIFY IS THE SESSION'S OWN AGENT (owner request 2026-11-10): it writes its
 * own steps and works them in its terminal, and tells the person whatever
 * only a person can do. So the steps here are READ-ONLY — nobody rewrites,
 * adds or answers them one by one. A person's part is one confirm: "Confirm
 * verified" / "Not verified…" once the agent is done (the row's `verify`). */

import type { SessionVerify, SessionVerifyFailedStep, TestPlan, TestStep } from "../../api/types";
import { CHECK_MARK, isFailing, latestRun, stepCheck, stepIsYours, type CheckState } from "../dialogs/verify";
import type { BehaviorSummary } from "../../lib/behaviors";
import type { Flight } from "../../lib/flights";
import { VERIFIED_LABEL, VERIFYING_LABEL, REASON_LABEL, verifyProgress } from "../../lib/sessionState";

/** The ⋯ menu's keys. A Verify stop's "Skip this stop" is a head button,
 * not a menu item. */
export type CardMenuKey = "verify" | "merged" | "deployed" | "fix" | "watch" | "cancel" | "session" | "diff" | "risk" | "pr" | "delete";

export interface CardMenuItem {
  key: CardMenuKey;
  label: string;
  title: string;
  danger?: boolean;
  disabled?: boolean;
}

export interface CardMenuCtx {
  summary: Pick<BehaviorSummary, "action"> | Pick<VerifyCardView, "action">;
  /** The open work session this plan belongs to, if any. */
  session?: { title: string; pr_url?: string | null; verify?: SessionVerify | null } | null;
}

/** The session a run of this plan lives (or lived) in, or "". */
export function watchSessionOf(plan: TestPlan): string {
  return plan.run_session || latestRun(plan)?.session || "";
}

/** Verify can start: nothing is checking or being written right now. */
function canVerify(plan: TestPlan, verify: SessionVerify | null | undefined): boolean {
  if (plan.state === "running" || plan.state === "generating") return false;
  return !verify || verify.state === "passed" || verify.state === "failed" || verify.state === "blocked";
}

/** Every applicable ⋯ item. The primary action is not repeated here. */
export function cardMenuItems(plan: TestPlan, ctx: CardMenuCtx): CardMenuItem[] {
  const items: CardMenuItem[] = [];
  const running = plan.state === "running";
  const primary = ctx.summary.action?.key || "";
  const verify = ctx.session?.verify || null;
  if (primary !== "verify" && canVerify(plan, verify) && (ctx.session || (plan.steps || []).length))
    items.push({ key: "verify", label: "Verify again", title: VERIFY_TITLE });
  // Waiting for its merge: a person can say it's out. A squash through
  // another branch's PR is a merge MindFlock can miss (docs/behaviors.md).
  if (plan.state === "generated" && !plan.merged_at && !plan.live_at)
    items.push({ key: "merged", ...mergedItem(plan) });
  if (plan.state === "generated" && plan.merged_at && !plan.live_at)
    items.push({ key: "deployed", label: "It's deployed — check now", title: "Don't wait out the deploy timer" });
  if (isFailing(plan) || verify?.state === "failed")
    items.push({ key: "fix", label: "Fix in a new session", title: "Start a new session with what failed attached" });
  const watch = watchSessionOf(plan);
  if (watch && watch !== ctx.session?.title)
    items.push({ key: "watch", label: "Open the agent verifying", title: "Open " + watch + " as a pane — closing it doesn't stop Verify" });
  if (running) items.push({ key: "cancel", label: "Stop verifying", title: "Stop the agent — what it found so far is kept" });
  if (ctx.session) {
    items.push({ key: "session", label: "Open the work session", title: "Show " + ctx.session.title + " in the Sky" });
    items.push({ key: "diff", label: "View diff", title: "Show what changed on " + (plan.branch || "this branch") });
    items.push({ key: "risk", label: "What could break", title: "Open the map on what imports these changes" });
    if (ctx.session.pr_url) items.push({ key: "pr", label: "Open PR ↗", title: ctx.session.pr_url });
  }
  items.push({ key: "delete", label: "Delete these steps", title: "Delete these steps and their results", danger: true });
  return items;
}

/** "It's merged into main" — the ⋯ item on a checklist waiting for its merge,
 * for the merges MindFlock can't see (a squash through another branch's PR).
 * Worded after the card's own "Not merged yet — … once it reaches main." */
export function mergedItem(plan: Pick<TestPlan, "live_branch">): { label: string; title: string } {
  const live = plan.live_branch || "the live branch";
  return {
    label: "It's merged into " + live,
    title: "Mark these changes as on " + live + " — for a merge MindFlock can't see, like a squash through another branch's PR",
  };
}

/** What Verify does, as its buttons' tooltip. */
export const VERIFY_TITLE =
  "The session's own agent writes the steps to check its work and works them in its terminal — it tells you anything only a person can do";

/** A row's result chip: the glyph and the words. */
export const CHIP_WORDS: Record<CheckState, string> = {
  pass: "Passed",
  fail: "Failed",
  cant: "Blocked",
  yours: "Needs you",
  pending: "Not done",
};

export function rowChip(plan: TestPlan, stepId: string): { state: CheckState; glyph: string; label: string } {
  const state = stepCheck(plan, stepId);
  return { state, glyph: CHECK_MARK[state], label: CHIP_WORDS[state] };
}

/** Who does one step, as a read-only mark. A step the agent handed back is a
 * person's now (stepIsYours). */
export function whoOf(plan: TestPlan, step: TestStep): { you: boolean; label: string; title: string; handed: boolean } {
  const you = stepIsYours(plan, step);
  const handed = you && step.actor !== "human";
  return {
    you,
    handed,
    label: you ? "🙋 you" : "🤖 agent",
    title: handed
      ? "The agent couldn't do this one — it's a person's now"
      : you
        ? "Only a person can do this step — the agent says so in its reply"
        : "The agent does this step in its terminal",
  };
}

/** "you" / "the agent" — who recorded a result. */
export function byWords(by: string): string {
  return by === "human" ? "you" : "the agent";
}

/** What the card's head says and offers, read from the session's `verify`
 * when there is one (an older server, or a session that never verified,
 * falls back to the plan's own summary, lib/behaviors). */
export interface VerifyCardView {
  tone: BehaviorSummary["tone"];
  status: string;
  sentence: string;
  /** The ONE primary action: start a verify, open the agent doing it, or
   * hand what failed to the session's own agent ("Let its agent fix it"). */
  action: null | { key: "verify" | "open" | "agentfix"; label: string; title: string };
  /** Failed steps (the halted line's count). */
  failed: number;
  /** failed: what failed, with the agent's note. */
  failedSteps: SessionVerifyFailedStep[];
  /** Show Confirm verified / Not verified…. */
  confirm: boolean;
  /** The confirm is a sign-off: every step was the agent's, nothing is left
   * for a person to DO — they sign the check off. */
  signOff: boolean;
}

/** "Let its agent fix it": what failed, with its evidence, goes to the
 * session's own agent. */
export const AGENT_FIX = {
  key: "agentfix" as const,
  label: "Let its agent fix it",
  title: "Send what failed, with the agent's evidence, to this session's own agent",
};

export function verifyCardView(
  plan: TestPlan,
  summary: BehaviorSummary,
  verify: SessionVerify | null | undefined,
  session: string,
): VerifyCardView {
  const base: VerifyCardView = {
    tone: summary.tone,
    status: summary.status,
    sentence: summary.sentence,
    action: null,
    failed: summary.failed,
    failedSteps: [],
    confirm: false,
    signOff: false,
  };
  const again = { key: "verify" as const, label: "Verify again", title: VERIFY_TITLE };
  const open = { key: "open" as const, label: "Open its agent", title: "Follow the agent's steps in its terminal, and talk to it there" };
  if (verify) {
    const detail = String(verify.detail || "").trim();
    switch (verify.state) {
      case "verifying":
        return { ...base, tone: "busy", status: VERIFYING_LABEL, sentence: "Its agent is checking its work in its terminal: " + verifyProgress(verify) + ".", action: session ? open : null };
      case "needs_you": {
        const n = (verify.human_steps || []).length || Number(verify.steps?.human) || 0;
        // The server's own line ("2 steps need you — check them, then
        // confirm." / "Every step passed — confirm it works."); a sign-off
        // with no person steps never says there are some.
        const fallback = n
          ? "The agent did its steps. " + (n === 1 ? "One step needs you — do it" : n + " steps need you — do them") + ", then confirm."
          : "Every step passed — sign it off.";
        return {
          ...base,
          tone: "you",
          status: REASON_LABEL.verify_confirm,
          sentence: detail || fallback,
          action: null,
          confirm: true,
          signOff: !n,
        };
      }
      case "failed":
        return {
          ...base,
          tone: "bad",
          status: REASON_LABEL.verify_failed,
          sentence: detail || "Some of its steps failed.",
          action: session ? AGENT_FIX : null,
          failed: Math.max(base.failed, (verify.failed_steps || []).length),
          failedSteps: verify.failed_steps || [],
        };
      case "blocked":
        return { ...base, tone: "bad", status: REASON_LABEL.verify_blocked, sentence: detail || "The agent couldn't finish verifying — open it to see why.", action: session ? again : null };
      case "passed":
        return { ...base, tone: "ok", status: VERIFIED_LABEL, sentence: detail || "The agent's steps passed" + (verify.steps?.human ? ", and a person confirmed theirs." : "."), action: null };
    }
  }
  if (plan.state === "running" || plan.state === "generating") return base;
  if (session && isFailing(plan)) return { ...base, action: AGENT_FIX };
  const before = !!latestRun(plan) || !!verify;
  return { ...base, action: session || (plan.steps || []).length ? { key: "verify", label: before ? "Verify again" : "Verify", title: VERIFY_TITLE } : null };
}

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
