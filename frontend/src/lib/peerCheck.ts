/** The joint check (docs/peer-link.md "Joint check — CONTRACT"): a joint
 * task's checklist, "do the two halves agree?". The shapes the server sends
 * (`joint.check` on GET …/joint, `joint.check` on GET /api/peer's link
 * summary), the words every surface shares — the card's verdict line, the
 * session chip — and the one-step-per-line editor format.
 *
 * Kept apart from lib/peer.ts and free of the store, so its wording tests run
 * without one. */

import { api } from "../api/client";

export type CheckSide = "mine" | "theirs";
export type CheckActor = "agent" | "human";
export type CheckResult = "pass" | "fail" | "blocked" | "";
export type CheckVerdict = "verified" | "failed" | "needs_you" | "in_progress" | "empty";

export interface CheckStep {
  id: string;
  text: string;
  expect?: string;
  actor: CheckActor;
  side: CheckSide;
}

export interface CheckCell {
  result: CheckResult;
  note?: string;
  at?: string;
  by?: CheckActor;
}

export interface CheckSideResults {
  results: Record<string, CheckCell>;
  tested_sha?: string;
  at?: string;
}

/** `joint.check` from GET /api/peer/links/{id}/joint (and every check route). */
export interface JointCheckView {
  id?: string;
  rev?: number;
  by?: "me" | "peer";
  steps: CheckStep[];
  mine?: CheckSideResults;
  theirs?: CheckSideResults;
  verdict?: CheckVerdict;
  ask?: { state: "" | "asking" | "failed"; at?: string; error?: string };
  theirs_unsupported?: boolean;
  /** The link it belongs to (so the card can act without being told). */
  link_id?: string;
}

/** `link.joint.check` on GET /api/peer: what a session chip needs. */
export interface JointCheckSummary {
  verdict: CheckVerdict;
  steps: number;
  passed: number;
}

/** A step's result on the side that checks it. */
export function cellOf(check: JointCheckView, step: CheckStep): CheckCell | undefined {
  const side = step.side === "mine" ? check.mine : check.theirs;
  return side?.results?.[step.id];
}

/** The verdict, recomputed (the rule the server keeps — docs/peer-link.md). */
export function verdictOf(check: JointCheckView | null | undefined): CheckVerdict {
  const steps = check?.steps || [];
  if (!check || !steps.length) return "empty";
  const seen = steps.map((s) => [s, cellOf(check, s)?.result || ""] as const);
  if (seen.some(([, r]) => r === "fail")) return "failed";
  if (seen.some(([s, r]) => r === "" && s.actor === "human")) return "needs_you";
  if (seen.some(([, r]) => r === "")) return "in_progress";
  if (seen.some(([, r]) => r === "blocked")) return "needs_you";
  return "verified";
}

const plural = (n: number) => n + (n === 1 ? " step" : " steps");

/** The card's verdict line: "Verified — every step passed on both sides",
 * "Failed — step 2 on Bob's side", "Needs you — 1 step", "In progress". */
export function verdictLine(check: JointCheckView | null | undefined, peerName: string): string {
  const name = (peerName || "").trim() || "your peer";
  const steps = check?.steps || [];
  const v = verdictOf(check);
  if (!check || v === "empty") return "No checklist yet";
  if (v === "verified") return "Verified — every step passed on both sides";
  if (v === "in_progress") return "In progress";
  if (v === "failed") {
    const i = steps.findIndex((s) => cellOf(check, s)?.result === "fail");
    const where = steps[i].side === "mine" ? "your side" : name + "'s side";
    return "Failed — step " + (i + 1) + " on " + where;
  }
  const blankHuman = (side: CheckSide) =>
    steps.filter((s) => s.side === side && s.actor === "human" && !cellOf(check, s)?.result).length;
  if (blankHuman("mine")) return "Needs you — " + plural(blankHuman("mine"));
  if (blankHuman("theirs")) return "Needs " + name + " — " + plural(blankHuman("theirs"));
  const blocked = steps.filter((s) => cellOf(check, s)?.result === "blocked").length;
  return "Needs you — " + plural(blocked) + " couldn't be checked";
}

/** The Verify chip's look for a verdict (`.vchip` data-tone + glyph). */
export function verdictTone(v: CheckVerdict): { tone: string; glyph: string; short: string } {
  switch (v) {
    case "verified":
      return { tone: "ok", glyph: "✓", short: "Joint verified" };
    case "failed":
      return { tone: "bad", glyph: "✗", short: "Joint failed" };
    case "needs_you":
      return { tone: "you", glyph: "●", short: "Joint: needs a person" };
    case "in_progress":
      return { tone: "busy", glyph: "…", short: "Joint checking" };
    default:
      return { tone: "wait", glyph: "○", short: "Joint check" };
  }
}

/** The session chip for a shared session whose link has a joint check;
 * null when there is none. */
export function jointCheckChip(
  summary: JointCheckSummary | null | undefined,
  peerName: string
): { verdict: CheckVerdict; tone: string; glyph: string; short: string; title: string } | null {
  if (!summary || !summary.steps) return null;
  const look = verdictTone(summary.verdict);
  const name = (peerName || "").trim() || "your peer";
  const words: Record<CheckVerdict, string> = {
    verified: "every step passed on both sides",
    failed: "a step failed",
    needs_you: "a person's step is waiting",
    in_progress: summary.passed + " of " + summary.steps + " passed so far",
    empty: "no steps yet",
  };
  return {
    verdict: summary.verdict,
    ...look,
    title: "Joint check with " + name + ": " + words[summary.verdict] + ". Open it in People.",
  };
}

// --------------------------------------------------------------------------- //
// The editor: one step per line, [theirs] / [human] tags
// --------------------------------------------------------------------------- //
const TAG_RE = /\[(theirs|mine|human|person|agent)\]/gi;
const EXPECT_SEP = /\s+(?:->|→)\s+/;

/** "[theirs] [human] Open the page -> the list shows" → a step body for
 * POST …/joint/check. Blank lines are skipped; untagged = my agent's. */
export function parseStepLines(text: string): Array<Omit<CheckStep, "id">> {
  const out: Array<Omit<CheckStep, "id">> = [];
  for (const raw of (text || "").split(/\r?\n/)) {
    let side: CheckSide = "mine";
    let actor: CheckActor = "agent";
    const body = raw
      .replace(TAG_RE, (_m, tag: string) => {
        const t = tag.toLowerCase();
        if (t === "theirs" || t === "mine") side = t;
        else actor = t === "agent" ? "agent" : "human";
        return " ";
      })
      .replace(/^\s*(?:[-*•]|\d+[.)])\s+/, "")
      .trim();
    if (!body) continue;
    const [what, ...rest] = body.split(EXPECT_SEP);
    out.push({ text: what.trim(), expect: rest.join(" -> ").trim(), actor, side });
  }
  return out;
}

/** The steps back as editor lines (round-trips through parseStepLines). */
export function stepsToLines(steps: CheckStep[]): string {
  return (steps || [])
    .map((s) => {
      const tags = (s.side === "theirs" ? "[theirs] " : "") + (s.actor === "human" ? "[human] " : "");
      return tags + s.text + (s.expect ? " -> " + s.expect : "");
    })
    .join("\n");
}

// --------------------------------------------------------------------------- //
// The routes
// --------------------------------------------------------------------------- //
const base = (linkId: string) => "/api/peer/links/" + encodeURIComponent(linkId) + "/joint/check";

export type CheckReply = { check: JointCheckView; synced?: boolean; steps?: number };

export function writeCheck(linkId: string, steps: Array<Omit<CheckStep, "id">>): Promise<CheckReply> {
  return api<CheckReply>(base(linkId), { method: "POST", json: { steps } });
}

export function askCheck(linkId: string): Promise<CheckReply> {
  return api<CheckReply>(base(linkId) + "/ask", { method: "POST" });
}

export function runCheck(linkId: string): Promise<CheckReply> {
  return api<CheckReply>(base(linkId) + "/run", { method: "POST" });
}

export function answerStep(linkId: string, stepId: string, result: CheckResult, note = ""): Promise<CheckReply> {
  return api<CheckReply>(base(linkId) + "/answer", {
    method: "POST",
    json: { step_id: stepId, result, note },
  });
}

/** The current copy (after an event for this link). */
export async function fetchCheck(linkId: string): Promise<JointCheckView | null> {
  const r = await api<{ joint: { check?: JointCheckView | null } | null }>(
    "/api/peer/links/" + encodeURIComponent(linkId) + "/joint"
  );
  return r?.joint?.check || null;
}
