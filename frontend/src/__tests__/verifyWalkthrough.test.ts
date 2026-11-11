/** Verify as a walkthrough (the Behaviors card): a step's Do / Expect and the
 * agent's evidence, READ-ONLY — since 2026-11-10 the steps are the agent's
 * own, so there is no "needs" picker, no per-step answer and no "Let the
 * agent walk every behavior" (the session's agent walks every step and tells
 * the person what only a person can do). The older run route's body is
 * unchanged. vitest is node-only, so the card renders to static markup and
 * the decisions are pinned through components/behaviors/cardModel.ts. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TestPlan, TestRun, TestStep, TestStepActor } from "../api/types";

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

const { BehaviorsCard } = await import("../components/behaviors/BehaviorsCard");
const { cardMenuItems } = await import("../components/behaviors/cardModel");
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

describe("a walkthrough step renders Do / Expect, read-only", () => {
  it("the text is the action, Expect under it — no 'after #n' needs chips", () => {
    const html = render(WALK);
    expect(html).toContain("POST /slack/commands with /sbx-ask hello");
    expect(html).toContain('<div class="bh-expect">Expect: a 200 and a reply in the channel</div>');
    expect(html).not.toContain("bh-need");
  });

  it("an old step (no expect) renders as before: no Expect line", () => {
    const html = render(plan({ steps: [step("a", "agent", { text: "The badge shows the count" })] }));
    expect(html).toContain("The badge shows the count");
    expect(html).not.toContain("bh-expect");
  });

  it("shows what the agent recorded under the step, as evidence", () => {
    const html = render(plan({ steps: WALK.steps, runs: [run({ results: { s1: { result: "pass", note: "200 OK", at: 1, by: "agent" } } })] }));
    expect(visible(html)).toContain("The agent: 200 OK");
  });
});

describe("no 'Let the agent walk every behavior'", () => {
  it("is gone from the head and the ⋯ menu — the session's agent walks every step", () => {
    const html = render(WALK);
    expect(html).not.toContain("bh-walk-all");
    expect(visible(html)).not.toContain("Let the agent walk every behavior");
    expect(cardMenuItems(WALK, { summary: behaviorSummary(WALK) }).map((i) => i.key as string)).not.toContain("walk");
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

  it("startChecklistRun (the card's Verify when the session is gone) still posts walk 'all' when asked", async () => {
    api.mockResolvedValue({ session: "verify-p1" });
    await startChecklistRun(WALK, { walk: "all" });
    expect(api).toHaveBeenCalledWith("/api/test-plans/p1/run", { method: "POST", json: { walk: "all" } });
    expect(toast).toHaveBeenCalledWith(expect.stringContaining("it walks every behavior, yours too"));
  });
});
