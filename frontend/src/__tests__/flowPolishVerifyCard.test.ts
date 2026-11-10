/** Flow polish (fp-verify-card): ONE Verify vocabulary on the card, the chip,
 * the Ship menu and the bell — every one maps the session row's `verify`
 * through lib/sessionState's own labels — one progress phrase, one place each
 * thing goes (the session's Verify steps), the card's rows showing who
 * answered and the evidence in full, "Let its agent fix it" as the card's
 * fix, and a Verify whose agent wrote no steps reading "Verify blocked"
 * rather than the old empty-plan dead end. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Instance, SessionVerify, TestPlan, TestRun, TestStep } from "../api/types";
import { code } from "./sourceText";

let instances: Instance[] = [];
vi.mock("../state/queries", async (orig) => ({
  ...((await orig()) as object),
  useInstances: () => ({ data: instances }),
  useFlights: () => ({ data: [] }),
}));

const S = await import("../lib/sessionState");
const { verifyStateLabel, verifyWords, planProgress, behaviorSummary } = await import("../lib/behaviors");
const { chipWords } = await import("../components/dialogs/VerdictChip");
const { BehaviorRow, rowEvidence } = await import("../components/behaviors/BehaviorRow");
const { BehaviorsCard, defaultFixInSession } = await import("../components/behaviors/BehaviorsCard");
const { verifyCardView, AGENT_FIX } = await import("../components/behaviors/cardModel");
const { isEmptyByChoice, isSelfBlocked, isSelfRun, isWaitingOnYou, planVerdict } = await import("../components/dialogs/verify");
const { notifFromEvent, verifyNotifTitle } = await import("../components/NotificationsBell");

const verify = (over: Partial<SessionVerify> = {}): SessionVerify => ({
  state: "verifying",
  plan: "p1",
  steps: { total: 5, agent_done: 3, human: 0 },
  human_steps: [],
  failed_steps: [],
  detail: "",
  at: 1,
  ...over,
});
const step = (id: string, actor: "agent" | "human" = "agent"): TestStep => ({ id, text: "do " + id, expect: id + " worked", actor });
const run = (over: Partial<TestRun> & { self?: boolean } = {}): TestRun => ({ at: 100, by: "agent", session: "", results: {}, verdict: "pass", ...over }) as TestRun;
const plan = (over: Partial<TestPlan> = {}): TestPlan =>
  ({
    id: "p1",
    title: "p1",
    repo_root: "/r/shop",
    branch: "feature/p1",
    state: "done",
    error: "",
    live_at: 0,
    merged_at: 0,
    steps: [],
    runs: [],
    run_session: "",
    ...over,
  }) as TestPlan;
const visible = (html: string) => html.replace(/<[^>]*>/g, " ").replace(/&[a-z#0-9]+;/g, " ").replace(/\s+/g, " ");

beforeEach(() => {
  instances = [];
});

describe("one vocabulary: every surface maps verify.state through sessionState's labels", () => {
  it("verifyStateLabel is the pill's own constants", () => {
    expect(verifyStateLabel("verifying")).toBe(S.VERIFYING_LABEL);
    expect(verifyStateLabel("needs_you")).toBe(S.REASON_LABEL.verify_confirm);
    expect(verifyStateLabel("passed")).toBe(S.VERIFIED_LABEL);
    expect(verifyStateLabel("failed")).toBe(S.REASON_LABEL.verify_failed);
    expect(verifyStateLabel("blocked")).toBe(S.REASON_LABEL.verify_blocked);
  });

  it("the chip reads the row's verify first: the label, the pill's one line", () => {
    const w = chipWords(verify(), null)!;
    expect(w).toMatchObject({ key: "verifying", tone: "busy", pane: S.VERIFYING_LABEL });
    expect(w.title).toBe("Verify: Verifying — 3 of 5 steps done. Click to see its steps.");
    expect(chipWords(verify({ state: "failed", failed_steps: [{ id: "a", text: "x", note: "" }] }), plan())!.pane).toBe(S.REASON_LABEL.verify_failed);
    expect(verifyWords(verify({ state: "passed" })).glyph).toBe("✓");
  });

  it("with no verify the chip says the plan's verdict and the card's own sentence", () => {
    const p = plan({ steps: [step("a")], runs: [run({ results: { a: { result: "fail", note: "", at: 1, by: "agent" } } })] });
    const w = chipWords(null, p)!;
    expect(w.pane).toBe("Verify failed");
    expect(w.title).toContain(behaviorSummary(p).sentence);
    expect(w.title).not.toMatch(/behaviou?rs?/i);
    expect(chipWords(null, null)).toBeNull();
  });

  it("one progress phrase: the pill's, for the plan too (agent steps)", () => {
    const p = plan({ state: "running", run_session: "p1", steps: [step("a"), step("b"), step("h", "human")], runs: [run({ session: "p1", results: { a: { result: "pass", note: "", at: 1, by: "agent" } } })] });
    expect(planProgress(p)).toBe("1 of 2 steps done");
    expect(planProgress(p)).toBe(S.verifyProgress({ steps: { total: 3, agent_done: 1, human: 1 } }));
  });

  it("the chip goes where the Ship menu and the bell go: the session's Verify steps", () => {
    expect(code("components/dialogs/VerdictChip.tsx")).toContain("openBehaviors({ planId: verify?.plan || plan?.id, title: inst.title, branch });");
    expect(code("lib/stage.ts")).toContain("const show = () => openBehaviors({ planId: verify?.plan || plan?.id, title: inst.title");
    expect(code("components/NotificationsBell.tsx")).toContain("openBehaviors({ planId: n.verify, title: n.session || n.verify });");
  });
});

describe("the bell, in Verify's words", () => {
  it("one step failed / a person's Fail", () => {
    const one = notifFromEvent({ event: "session.test_plan_checked", session: "sc-1", seq: 1, ts: 0, old: null, new: "failed", data: { plan: "sc-1", failed: 1 } })!;
    expect(one).toMatchObject({ verifyTitle: "Verify failed", text: "1 step failed", verify: "sc-1" });
    expect(verifyNotifTitle(one, "sc-1")).toBe("Verify failed — sc-1");
    const step3 = notifFromEvent({ event: "session.test_plan_step_failed", session: "sc-1", seq: 2, ts: 0, old: null, new: "failed", data: { plan: "sc-1", step: 3 } })!;
    expect(step3.text).toBe("step 3 failed");
  });
});

describe("a Verify whose agent wrote no steps is blocked, not empty-by-choice", () => {
  const selfBlocked = plan({ origin: "manual", runs: [run({ session: "p1", self: true })] });

  it("isSelfBlocked; never empty-by-choice, never waiting on you forever", () => {
    expect(isSelfRun(selfBlocked.runs[0])).toBe(true);
    expect(isSelfRun(run())).toBe(false);
    expect(isSelfBlocked(selfBlocked)).toBe(true);
    expect(isEmptyByChoice(selfBlocked)).toBe(false);
    expect(isWaitingOnYou(selfBlocked)).toBe(false);
    expect(planVerdict(selfBlocked).label).toBe("Verify blocked — its agent wrote no steps");
    // While it is still writing them, it is Verifying — not empty, not blocked.
    const writing = plan({ origin: "manual", state: "running", run_session: "p1", runs: [run({ session: "p1", self: true })] });
    expect(isSelfBlocked(writing)).toBe(false);
    expect(isEmptyByChoice(writing)).toBe(false);
    expect(behaviorSummary(writing)).toMatchObject({ status: S.VERIFYING_LABEL, sentence: "Its agent is checking its work: writing its steps." });
  });

  it("the card offers Verify again on it (the session is open)", () => {
    instances = [{ title: "p1", branch: "feature/p1", repo: "shop" } as Instance];
    const html = renderToStaticMarkup(createElement(BehaviorsCard, { plan: selfBlocked }));
    expect(html).toContain('data-tone="bad">Verify blocked</span>');
    expect(html).toMatch(/data-action="verify"[^>]*>Verify again</);
    expect(visible(html)).toContain("Its agent wrote no steps.");
    expect(visible(html)).not.toMatch(/add a step|Write them|No behaviors yet/);
  });
});

describe("the card's fix is a prop: Let its agent fix it", () => {
  it("a failed verify's primary is Let its agent fix it; Verify again moves to ⋯", () => {
    const p = plan({ steps: [step("a")] });
    const v = verifyCardView(p, behaviorSummary(p), verify({ state: "failed", detail: "1 step failed: do a." }), "p1");
    expect(v.action).toEqual(AGENT_FIX);
    expect(v.action!.label).toBe("Let its agent fix it");
    expect(v.sentence).toBe("1 step failed: do a.");
  });

  it("the default calls the existing fix; the head and every failed row go through the one prop", () => {
    expect(typeof defaultFixInSession).toBe("function");
    const src = code("components/behaviors/BehaviorsCard.tsx");
    expect(src).toContain("onFixInSession = defaultFixInSession");
    expect(src).toContain('else if (a.key === "agentfix") void withBusy("primary", () => onFixInSession(plan));');
    expect(src).toContain('withBusy("fix", () => onFixInSession(plan, [step.id]))');
    expect(src).toContain("return actions.fixPlanFailures(plan, stepIds);");
  });
});

describe("each step's row: result, who answered, the evidence in full", () => {
  const entry = (o: object) => ({ result: "pass", note: "", at: 1, by: "agent", ...o }) as never;

  it("rowEvidence: who, the note, the agent's note when a person overrode it, a blocked step's why", () => {
    expect(rowEvidence(entry({ note: "200 OK" }))).toEqual({ by: "the agent", note: "200 OK", agentNote: "", why: "" });
    expect(rowEvidence(entry({ result: "fail", by: "human", note: "looks wrong", agent_note: "saw the banner" }))).toMatchObject({ by: "you", agentNote: "saw the banner" });
    // The agent's own entry never shows an "agent had said" line.
    expect(rowEvidence(entry({ agent_note: "x" })).agentNote).toBe("");
    expect(rowEvidence(entry({ result: "blocked", note: "staging is down" })).why).toBe("staging is down");
    expect(rowEvidence(entry({ result: "blocked" })).why).toBe("No reason was given.");
    expect(rowEvidence(null)).toEqual({ by: "", note: "", agentNote: "", why: "" });
  });

  it("open: the result and who answered, the whole note, what the agent had said", () => {
    const long = "the badge read 0 while three items were queued; ".repeat(4).trim();
    const p = plan({
      steps: [step("a"), step("h", "human")],
      runs: [
        run({
          results: {
            a: { result: "fail", note: long, at: 1, by: "agent" },
            h: { result: "pass", note: "fine on my phone", at: 1, by: "human", agent_note: "no phone to open it on" } as never,
          },
        }),
      ],
    });
    const html = (id: string, i: number) => visible(renderToStaticMarkup(createElement(BehaviorRow, { plan: p, step: p.steps[i], index: i, allOpen: { open: true, n: 1 }, onFix() {} })));
    expect(html("a", 0)).toContain("Failed · by the agent");
    expect(html("a", 0)).toContain(long);
    expect(html("a", 0)).toContain("Let its agent fix it");
    expect(html("h", 1)).toContain("Passed · by you");
    expect(html("h", 1)).toContain("Your note: fine on my phone");
    expect(html("h", 1)).toContain("The agent had said: no phone to open it on");
  });

  it("folded: a blocked step still says why", () => {
    const p = plan({ steps: [step("a")], runs: [run({ results: { a: { result: "blocked", note: "staging is down", at: 1, by: "human" } } })] });
    const html = renderToStaticMarkup(createElement(BehaviorRow, { plan: p, step: p.steps[0], index: 0 }));
    expect(visible(html)).toContain("Why: staging is down");
    expect(html).toMatch(/class="bh-chip" data-check="cant"[^>]*>.*Blocked/);
  });
});
