/** Verify as a walkthrough (the Behaviors card): a step's Do / Expect /
 * needs, a person's behavior an agent walked (`walked` + its evidence), the
 * "Let the agent walk every behavior" action and the run body it posts
 * ({walk: "all"}; the ordinary run's body unchanged). vitest is node-only, so
 * the card renders to static markup and the decisions are pinned through
 * components/behaviors/cardModel.ts. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TestPlan, TestRun, TestStep, TestStepActor, TestStepResultEntry } from "../api/types";

const api = vi.fn();
const toast = vi.fn();
vi.mock("../api/client", async (orig) => ({ ...((await orig()) as object), api: (...a: unknown[]) => api(...a) }));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
vi.mock("../state/queries", async (orig) => ({
  ...((await orig()) as object),
  useInstances: () => ({ data: [] }),
  useFlights: () => ({ data: [] }),
  refreshInstances: () => Promise.resolve(),
}));

const { BehaviorsCard, addBehavior } = await import("../components/behaviors/BehaviorsCard");
const { cardMenuItems, earlierSteps, needsChips, offersWalkAll, walkAllBesidePrimary, walkedWords } = await import("../components/behaviors/cardModel");
const { behaviorSummary } = await import("../lib/behaviors");
const { requestPlanRun, startChecklistRun } = await import("../lib/verifyActions");
const { queryClient } = await import("../state/queries");

const step = (id: string, actor: TestStepActor = "agent", over: Partial<TestStep> = {}): TestStep => ({ id, text: "do " + id, actor, ...over });
const run = (over: Partial<TestRun> = {}): TestRun => ({ at: 100, by: "agent", session: "verify-p1", results: {}, verdict: "pass", ...over });
const plan = (over: Partial<TestPlan> = {}): TestPlan =>
  ({
    id: "p1",
    title: "Slack ask",
    repo_root: "/r/shop",
    branch: "feature/p1",
    sha: "a1b2c3d4",
    live_branch: "main",
    effective_live_branch: "main",
    state: "due",
    error: "",
    generated_at: 10,
    gen_started: Date.now() / 1000,
    gen_attempts: 0,
    merged_at: 0,
    live_at: 50,
    merged_into: "",
    merged_into_at: 0,
    merged_into_all: [],
    summary: "",
    intent: "",
    focus: "",
    notified_at: 0,
    live_problem: "",
    steps: [],
    runs: [],
    run_session: "",
    ...over,
  }) as TestPlan;

const render = (p: TestPlan, props: Record<string, unknown> = {}) => renderToStaticMarkup(createElement(BehaviorsCard, { plan: p, expanded: true, ...props }));
const visible = (html: string) => html.replace(/<[^>]*>/g, " ").replace(/&[a-z#0-9]+;/g, " ").replace(/\s+/g, " ");

const WALK = plan({
  steps: [
    step("s1", "agent", { text: "POST /slack/commands with /sbx-ask hello", expect: "a 200 and a reply in the channel" }),
    step("s2", "agent", { text: "Search Loki for the ask", expect: "one line with the request id", needs: ["s1"] }),
    step("s3", "human", { text: "Read the reply in Slack", expect: "it reads as a sentence", needs: ["s1", "s9", "s3", "s4"] }),
    step("s4", "agent", { text: "Old step with no expect" }),
  ],
});

beforeEach(() => {
  queryClient.clear();
  api.mockReset();
  toast.mockReset();
});

describe("a walkthrough step renders Do / Expect / needs", () => {
  it("the text is the action, Expect under it, needs as 'after #n' chips", () => {
    const html = render(WALK);
    expect(html).toContain("POST /slack/commands with /sbx-ask hello");
    expect(html).toContain('<div class="bh-expect">Expect: a 200 and a reply in the channel</div>');
    // s2 relies on s1 → numbered as the card shows it.
    expect(html).toMatch(/<span class="bh-need-chip" title="Relies on behavior 1: POST \/slack\/commands with \/sbx-ask hello">after #1<\/span>/);
  });

  it("an old step (no expect, no needs) renders as before: no Expect line, no chips", () => {
    const html = render(plan({ steps: [step("a", "agent", { text: "The badge shows the count" })] }));
    expect(html).toContain("The badge shows the count");
    expect(html).not.toContain("bh-expect");
    expect(html).not.toContain("bh-needs");
    expect(html).not.toContain("bh-walked");
  });

  it("needs chips keep only EARLIER, known ids, once each", () => {
    expect(needsChips(WALK, WALK.steps[2]).map((c) => c.label)).toEqual(["after #1"]); // s9 unknown, s3 self, s4 forward
    expect(needsChips(WALK, WALK.steps[0])).toEqual([]);
  });

  it("the needs picker offers only earlier behaviors (all of them for a new one)", () => {
    expect(earlierSteps(WALK, "s3").map((e) => [e.step.id, e.n])).toEqual([["s1", 1], ["s2", 2]]);
    expect(earlierSteps(WALK, "s1")).toEqual([]);
    expect(earlierSteps(WALK).map((e) => e.step.id)).toEqual(["s1", "s2", "s3", "s4"]);
    expect(earlierSteps(WALK, "gone")).toEqual([]);
  });
});

describe("a person's behavior an agent walked", () => {
  const walkedEntry = (over: Partial<TestStepResultEntry> = {}): TestStepResultEntry => ({ result: "pass", note: "posted via the Slack API; reply ts 1730 read back", at: 100, by: "agent", walked: true, ...over });

  it("says 'walked by the agent' with its evidence visible", () => {
    const p = plan({ steps: WALK.steps, runs: [run({ walk: "all", results: { s3: walkedEntry() } })] });
    const html = render(p);
    expect(html).toContain('<span class="bh-walked"');
    expect(visible(html)).toContain("walked by the agent");
    expect(html).toContain('class="bh-evidence is-walked"');
    expect(visible(html)).toContain("The agent walked it: posted via the Slack API; reply ts 1730 read back");
  });

  it("a walked 'blocked' says it tried and the behavior is yours; a plain agent answer has no marker", () => {
    expect(walkedWords(walkedEntry({ result: "blocked", note: "needs you: no Slack token" }))).toBe("the agent tried — needs you");
    expect(walkedWords(walkedEntry({ walked: undefined }))).toBe("");
    expect(walkedWords(walkedEntry({ by: "human" }))).toBe("");
    expect(walkedWords(null)).toBe("");
    const html = render(plan({ steps: WALK.steps, runs: [run({ results: { s1: { result: "pass", note: "200 OK", at: 1, by: "agent" } } })] }));
    expect(html).not.toContain("bh-walked");
    expect(visible(html)).toContain("The agent: 200 OK");
  });
});

describe("Let the agent walk every behavior", () => {
  it("is offered only when a plan has a person's behavior and nothing is running or being written", () => {
    expect(offersWalkAll(WALK)).toBe(true);
    expect(offersWalkAll(plan({ steps: [step("a"), step("b")] }))).toBe(false);
    expect(offersWalkAll(plan({ ...WALK, state: "running" }))).toBe(false);
    expect(offersWalkAll(plan({ ...WALK, state: "generating" }))).toBe(false);
  });

  it("sits beside the head's Check now, and is then not repeated in ⋯", () => {
    const s = behaviorSummary(WALK);
    expect(["check", "rerun"]).toContain(s.action?.key);
    expect(walkAllBesidePrimary(WALK, s.action?.key || "")).toBe(true);
    const html = render(WALK);
    expect(html).toMatch(/class="test-btn bh-walk-all" data-action="walk"[^>]*>Let the agent walk every behavior</);
    expect(cardMenuItems(WALK, { summary: s }).map((i) => i.key)).not.toContain("walk");
  });

  it("goes in ⋯ when the primary is something else; absent with no person's behavior", () => {
    const failed = plan({ ...WALK, state: "done", runs: [run({ verdict: "fail", results: { s1: { result: "fail", note: "500", at: 1, by: "agent" } } })] });
    const s = behaviorSummary(failed);
    expect(s.action?.key).not.toBe("check");
    expect(walkAllBesidePrimary(failed, s.action?.key || "")).toBe(false);
    expect(cardMenuItems(failed, { summary: s }).find((i) => i.key === "walk")?.label).toBe("Let the agent walk every behavior");
    expect(render(failed)).not.toContain("bh-walk-all");

    const agentOnly = plan({ steps: [step("a")] });
    expect(render(agentOnly)).not.toContain("bh-walk-all");
    expect(cardMenuItems(agentOnly, { summary: behaviorSummary(agentOnly) }).map((i) => i.key)).not.toContain("walk");
  });
});

describe("the run body", () => {
  it("the ordinary run is unchanged ({} / {steps}); walk all posts {walk: 'all'}", async () => {
    api.mockResolvedValue({ session: "verify-p1" });
    await requestPlanRun("p1");
    expect(api).toHaveBeenLastCalledWith("/api/test-plans/p1/run", { method: "POST", json: {} });
    await requestPlanRun("p1", ["s2"]);
    expect(api).toHaveBeenLastCalledWith("/api/test-plans/p1/run", { method: "POST", json: { steps: ["s2"] } });
    await requestPlanRun("p1", undefined, "agent");
    expect(api).toHaveBeenLastCalledWith("/api/test-plans/p1/run", { method: "POST", json: {} });
    await requestPlanRun("p1", ["s3"], "all");
    expect(api).toHaveBeenLastCalledWith("/api/test-plans/p1/run", { method: "POST", json: { steps: ["s3"], walk: "all" } });
  });

  it("startChecklistRun with walk 'all' posts it and says so", async () => {
    api.mockResolvedValue({ session: "verify-p1" });
    await startChecklistRun(WALK, { walk: "all" });
    expect(api).toHaveBeenCalledWith("/api/test-plans/p1/run", { method: "POST", json: { walk: "all" } });
    expect(toast).toHaveBeenCalledWith(expect.stringContaining("it walks every behavior, yours too"));
  });

  it("adding a behavior sends needs only when some were picked", async () => {
    api.mockResolvedValue({ plan: WALK });
    await addBehavior(WALK, { text: "Check the thread", expect: "a reply", actor: "agent", needs: ["s1"] });
    expect(api).toHaveBeenCalledWith("/api/test-plans/p1/steps", { method: "POST", json: { text: "Check the thread", expect: "a reply", actor: "agent", needs: ["s1"] } });
  });
});
