/** The Behaviors card (components/behaviors/BehaviorsCard.tsx, docs/behaviors.md
 * §2): what it renders for a plan, how an answer and a new behavior reach the
 * server and the cache, which ⋯ items it offers, and the row keys. vitest is
 * node-only, so the card is rendered to static markup and its decisions are
 * pinned through components/behaviors/cardModel.ts. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Instance, TestPlan, TestPlansResponse, TestRun, TestStep, TestStepActor, TestStepResult, TestStepResultEntry } from "../api/types";

const api = vi.fn();
const toast = vi.fn();
vi.mock("../api/client", async (orig) => ({ ...((await orig()) as object), api: (...a: unknown[]) => api(...a) }));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
// The card looks up its open work session; the real hook polls (and reads
// `document`), which node has none of.
let instances: Instance[] = [];
vi.mock("../state/queries", async (orig) => ({ ...((await orig()) as object), useInstances: () => ({ data: instances }) }));

const { BehaviorsCard, addBehavior } = await import("../components/behaviors/BehaviorsCard");
const { cardMenuItems, primaryTarget, rowChip, rowKey, showsAnswers, whoOf } = await import("../components/behaviors/cardModel");
const { behaviorSummary } = await import("../lib/behaviors");
const { recordStepAnswer } = await import("../lib/verifyActions");
const { queryClient } = await import("../state/queries");

const step = (id: string, actor: TestStepActor = "agent", over: Partial<TestStep> = {}): TestStep => ({ id, text: "do " + id, expect: id + " worked", actor, ...over });
const results = (answers: Record<string, TestStepResult>, by = "agent", note = ""): Record<string, TestStepResultEntry> => {
  const map: Record<string, TestStepResultEntry> = {};
  for (const [id, result] of Object.entries(answers)) map[id] = { result, note, at: 100, by };
  return map;
};
const run = (over: Partial<TestRun> = {}): TestRun => ({ at: 100, by: "agent", session: "", results: {}, verdict: "pass", ...over });
const plan = (over: Partial<TestPlan> = {}): TestPlan => ({
  id: "p1",
  title: "Queue tab badges",
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
});
const session = { title: "p1", branch: "feature/p1", repo: "shop", pr_url: "https://github.com/acme/shop/pull/7" } as unknown as Instance;

const FAILED = plan({
  state: "done",
  steps: [step("a", "agent", { text: "The badge shows the queue count" }), step("h", "human", { text: "It looks right on a phone", manual: true })],
  runs: [run({ session: "verify-p1", results: { ...results({ a: "fail" }, "agent", "badge read 0 with 3 queued") } })],
});

const render = (p: TestPlan, props: Record<string, unknown> = {}) => renderToStaticMarkup(createElement(BehaviorsCard, { plan: p, ...props }));
/** The words a person sees (tags, attributes and entities stripped). */
const visible = (html: string) => html.replace(/<[^>]*>/g, " ").replace(/&[a-z#0-9]+;/g, " ");

beforeEach(() => {
  queryClient.clear();
  instances = [session];
  api.mockReset();
  toast.mockReset();
});

describe("BehaviorsCard renders", () => {
  it("the head: status, sentence, ONE primary action, ⋯ and ×", () => {
    const html = render(FAILED, { onClose() {} });
    expect(html).toContain('class="bh-card is-bad"');
    expect(html).toContain('<span class="bh-status" data-tone="bad">Failed</span>');
    expect(html).toContain(behaviorSummary(FAILED).sentence);
    expect(html.match(/class="btn-primary bh-primary"/g)).toHaveLength(1);
    expect(html).toMatch(/data-action="failed"[^>]*>See what failed</);
    expect(html).toContain('aria-label="More for these behaviors"');
    expect(html).toContain('aria-label="Close"');
    expect(html).not.toContain("Skip this stop");
  });

  it("one row per behavior: text, expect, who checks it, the result chip", () => {
    const html = render(FAILED);
    expect(html.match(/<li class="bh-row/g)).toHaveLength(2);
    expect(html).toContain("The badge shows the queue count");
    expect(html).toContain("Expect: a worked");
    expect(html).toContain("🤖 agent");
    expect(html).toContain("🙋 you");
    expect(html).toMatch(/class="bh-chip" data-check="fail"[^>]*>.*Failed/);
    expect(html).toMatch(/class="bh-chip" data-check="yours"[^>]*>.*Needs you/);
  });

  it("yours get Pass · Fail · Can't check; the agent's show its note as evidence", () => {
    const html = render(FAILED);
    // Only the human row carries the three answers.
    expect(html.match(/bh-answer-pass/g)).toHaveLength(1);
    expect(html).toContain(">Can&#x27;t check</button>");
    expect(html).toContain("The agent:</span> badge read 0 with 3 queued");
    // A failed row offers Fix; a person-added one Remove; every row Edit.
    expect(html).toContain(">Fix</button>");
    expect(html.match(/>Remove<\/button>/g)).toHaveLength(1);
    expect(html.match(/>Edit<\/button>/g)).toHaveLength(2);
  });

  it("+ Add a behavior at the foot; Skip this stop on a Verify stop's card", () => {
    const html = render(FAILED, { flightStep: { flightId: "f1", stepId: "s3" } });
    expect(html).toContain(">+ Add a behavior</button>");
    expect(html).toContain(">Skip this stop</button>");
    expect(html).not.toMatch(/>Done</);
  });

  it("compact keeps everything, narrower", () => {
    expect(render(FAILED, { compact: true })).toContain('class="bh-card is-bad is-compact"');
  });

  it("never says checklist, test plan or step", () => {
    for (const p of [FAILED, plan({ state: "generating" }), plan({ state: "generated", origin: "manual" })]) {
      expect(visible(render(p, { onClose() {}, flightStep: { flightId: "f", stepId: "s" } }))).not.toMatch(/checklist|test plan|\bsteps?\b/i);
    }
  });
});

describe("answering", () => {
  it("records an answer and applies the server's plan to the cache", async () => {
    const answered: TestPlan = { ...FAILED, runs: [run({ session: "verify-p1", results: { ...FAILED.runs[0].results, ...results({ h: "pass" }, "human") } })] };
    queryClient.setQueryData<TestPlansResponse>(["test-plans"], { plans: [FAILED], live_branch: "main" });
    api.mockResolvedValue({ plan: answered });
    await recordStepAnswer(FAILED, FAILED.steps[1], "pass", "looked fine");
    expect(api).toHaveBeenCalledWith("/api/test-plans/p1/result", { json: { step_id: "h", result: "pass", note: "looked fine" } });
    expect(queryClient.getQueryData<TestPlansResponse>(["test-plans"])?.plans[0]).toEqual(answered);
  });

  it("keys on a focused row: 1/p 2/f 3/b answer, u undoes, n opens the note", () => {
    const k = (key: string, answered = false) => rowKey(key, { answered });
    expect([k("1"), k("p"), k("2"), k("f"), k("3"), k("b")]).toEqual(["pass", "pass", "fail", "fail", "blocked", "blocked"]);
    expect(k("u")).toBe("");
    expect(k("u", true)).toBe("undo");
    expect(k("n")).toBe("note");
    expect(k("x")).toBe("");
    expect(rowKey("1", { answered: false, mods: true })).toBe("");
    expect(rowKey("1", { answered: false, repeat: true })).toBe("");
    expect(rowKey("1", { answered: false, busy: true })).toBe("");
  });

  it("who checks it: the agent's hand-back makes it yours", () => {
    const handed = plan({ state: "done", steps: [step("a")], runs: [run({ results: results({ a: "blocked" }) })] });
    expect(whoOf(handed, handed.steps[0])).toMatchObject({ you: true, handed: true, label: "🙋 you" });
    expect(whoOf(FAILED, FAILED.steps[0])).toMatchObject({ you: false, label: "🤖 agent" });
    expect(showsAnswers(handed, handed.steps[0])).toBe(true);
    expect(showsAnswers(FAILED, FAILED.steps[0])).toBe(false);
    expect(rowChip(handed, "a")).toEqual({ state: "yours", glyph: "●", label: "Needs you" });
  });

  it("the primary action lands on the first failed / first yours", () => {
    expect(primaryTarget(FAILED, "failed")).toBe("a");
    expect(primaryTarget(FAILED, "answer")).toBe("h");
  });
});

describe("adding a behavior", () => {
  it("posts it and applies the server's copy", async () => {
    const next = { ...FAILED, steps: [...FAILED.steps, step("m1", "human", { manual: true, text: "Coupons apply" })] };
    queryClient.setQueryData<TestPlansResponse>(["test-plans"], { plans: [FAILED], live_branch: "main" });
    api.mockResolvedValue({ plan: next });
    await addBehavior(FAILED, { text: "Coupons apply", expect: "", actor: "human" });
    expect(api).toHaveBeenCalledWith("/api/test-plans/p1/steps", { method: "POST", json: { text: "Coupons apply", expect: "", actor: "human" } });
    expect(queryClient.getQueryData<TestPlansResponse>(["test-plans"])?.plans[0].steps).toHaveLength(3);
  });

  it("rejects with the server's message so the form keeps its text", async () => {
    api.mockRejectedValue(new Error("plan is being written"));
    await expect(addBehavior(FAILED, { text: "x", expect: "", actor: "agent" })).rejects.toThrow("plan is being written");
  });
});

describe("the ⋯ menu", () => {
  const labels = (p: TestPlan, withSession = true) =>
    cardMenuItems(p, { summary: behaviorSummary(p), session: withSession ? (session as unknown as { title: string; pr_url?: string }) : null }).map((i) => i.label);

  it("on a failure: rewrite, fix, rerun, the checking run, the work session, diff, delete last", () => {
    expect(labels(FAILED)).toEqual([
      "Rewrite behaviors",
      "Fix in a new session",
      "Rerun",
      "Open the checking run",
      "Open the work session",
      "View diff",
      "What could break",
      "Open PR ↗",
      "Delete these behaviors",
    ]);
    const del = cardMenuItems(FAILED, { summary: behaviorSummary(FAILED) }).at(-1);
    expect(del).toMatchObject({ key: "delete", danger: true });
  });

  it("while checking: Cancel the check; Rewrite is offered but held", () => {
    const p = plan({ state: "running", run_session: "verify-p1", steps: [step("a")], runs: [run({ session: "verify-p1" })] });
    const items = cardMenuItems(p, { summary: behaviorSummary(p) });
    expect(items.map((i) => i.label)).toEqual(["Rewrite behaviors", "Open the checking run", "Cancel the check", "Delete these behaviors"]);
    expect(items[0]).toMatchObject({ disabled: true });
  });

  it("merged and waiting for the deploy: It's deployed — check now", () => {
    const p = plan({ state: "generated", live_at: 0, merged_at: 10, steps: [step("a")] });
    expect(labels(p, false)).toContain("It's deployed — check now");
    // The primary is not the run, so the ⋯ offers it.
    expect(labels(p, false)).toContain("Check now");
  });

  it("no work session open: no Open the work session / View diff", () => {
    expect(labels(FAILED, false)).not.toContain("Open the work session");
    expect(labels(FAILED, false)).not.toContain("View diff");
  });

  it("the primary action is never repeated in the menu", () => {
    const p = plan({ state: "due", steps: [step("a")] });
    expect(behaviorSummary(p).action?.label).toBe("Check now");
    expect(labels(p)).not.toContain("Check now");
    expect(labels(plan({ state: "failed" }))).not.toContain("Rewrite behaviors");
  });
});
