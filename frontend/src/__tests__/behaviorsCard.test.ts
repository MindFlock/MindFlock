/** The Behaviors card (components/behaviors/BehaviorsCard.tsx): what it
 * renders for a plan — read-only steps, each with who does it and its result
 * (the steps are the agent's own since 2026-11-10; a person's part is one
 * Confirm verified, pinned in verifySelf.test.ts) — and which ⋯ items it
 * offers. vitest is node-only, so the card is rendered to static markup and
 * its decisions are pinned through components/behaviors/cardModel.ts. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Instance, SessionVerify, TestPlan, TestRun, TestStep, TestStepActor, TestStepResult, TestStepResultEntry } from "../api/types";

const api = vi.fn();
const toast = vi.fn();
vi.mock("../api/client", async (orig) => ({ ...((await orig()) as object), api: (...a: unknown[]) => api(...a) }));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
// The card looks up its open work session; the real hook polls (and reads
// `document`), which node has none of.
let instances: Instance[] = [];
// The route a Verify stop's card belongs to (useFlights): halted, parked…
let flights: unknown[] = [];
vi.mock("../state/queries", async (orig) => ({
  ...((await orig()) as object),
  useInstances: () => ({ data: instances }),
  useFlights: () => ({ data: flights }),
}));

const { BehaviorsCard } = await import("../components/behaviors/BehaviorsCard");
const { cardMenuItems, rowChip, verifyCardView, whoOf } = await import("../components/behaviors/cardModel");
const { behaviorSummary } = await import("../lib/behaviors");
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
  it("the head: status, sentence, ONE primary action (Verify), ⋯ and ×", () => {
    const html = render(FAILED, { onClose() {} });
    expect(html).toContain('class="bh-card is-bad"');
    expect(html).toContain('<span class="bh-status" data-tone="bad">Failed</span>');
    expect(html).toContain(behaviorSummary(FAILED).sentence);
    expect(html.match(/class="btn-primary bh-primary"/g)).toHaveLength(1);
    expect(html).toMatch(/data-action="verify"[^>]*>Verify again</);
    expect(html).toContain('aria-label="More for these behaviors"');
    expect(html).toContain('aria-label="Close"');
    expect(html).not.toContain("Skip this stop");
  });

  it("one row per step: text, expect, who does it, the result chip", () => {
    const html = render(FAILED, { expanded: true });
    expect(html.match(/<li class="bh-row/g)).toHaveLength(2);
    expect(html).toContain("The badge shows the queue count");
    expect(html).toContain("Expect: a worked");
    expect(html).toContain("🤖 agent");
    expect(html).toContain("🙋 you");
    expect(html).toMatch(/class="bh-chip" data-check="fail"[^>]*>.*Failed/);
    expect(html).toMatch(/class="bh-chip" data-check="yours"[^>]*>.*A person&#x27;s/);
  });

  it("READ-ONLY: no answers, edits, removes, rewrite or add — the agent's note as evidence, Fix on a failure", () => {
    const html = render(FAILED, { expanded: true });
    expect(html).toContain("The agent:</span> badge read 0 with 3 queued");
    expect(html).toContain(">Fix</button>");
    for (const gone of ["bh-answer", ">Can&#x27;t check</button>", ">Edit</button>", ">Remove</button>", "+ Add a behavior", "Rewrite behaviors", "Let the agent walk every behavior", "bh-need"])
      expect(html, gone).not.toContain(gone);
    // Who does it is a mark, not a toggle.
    expect(html).toMatch(/<span class="bh-who"[^>]*>🤖 agent<\/span>/);
    expect(html).toMatch(/<span class="bh-who is-you"[^>]*>🙋 you<\/span>/);
  });

  it("rows start collapsed: the chip and the text (a toggle) only, with Expand all", () => {
    const html = render(FAILED);
    expect(html.match(/<li class="bh-row/g)).toHaveLength(2);
    expect(html).toContain("The badge shows the queue count");
    expect(html).toMatch(/class="bh-text bh-text-toggle is-clamped"[^>]*aria-expanded="false"/);
    expect(html).not.toContain("Expect: a worked");
    expect(html).not.toContain("The agent:</span>");
    expect(html).toContain(">Expand all</button>");
    expect(html).toMatch(/class="bh-chip" data-check="fail"[^>]*>.*Failed/);
  });

  it("Skip this stop on a Verify stop's card", () => {
    const html = render(FAILED, { flightStep: { flightId: "f1", stepId: "s3" } });
    expect(html).toContain(">Skip this stop</button>");
    expect(html).not.toMatch(/>Done</);
  });

  it("a Verify stop whose behaviors failed HALTS the route: Retry · Skip · Let the agent fix it", () => {
    flights = [
      {
        id: "f1",
        state: "halted",
        cursor: 0,
        steps: [{ id: "s3", kind: "verify", title: "Verify", config: {}, on_fail: "halt" }],
        step_states: { s3: { state: "failed", since: 1, note: "Failed · 1: a", detail: {}, rounds: 0 } },
      },
    ];
    try {
      const html = render(FAILED, { flightStep: { flightId: "f1", stepId: "s3" } });
      expect(visible(html)).toContain("Failed · 1 — halted. The route waits here until you Retry, Skip or let the agent fix it.");
      expect(html).toMatch(/class="btn-primary bh-retry"[^>]*>Retry</);
      expect(html).toMatch(/class="test-btn bh-skip"[^>]*>Skip</);
      expect(html).toMatch(/class="test-btn bh-fix-agent"[^>]*>Let the agent fix it</);
      expect(html).not.toContain("Skip this stop");
      expect(html).not.toMatch(/>Done</);
    } finally {
      flights = [];
    }
  });

  it("compact keeps everything, narrower", () => {
    expect(render(FAILED, { compact: true })).toContain('class="bh-card is-bad is-compact"');
  });

  it("never says checklist or test plan", () => {
    for (const p of [FAILED, plan({ state: "generating" }), plan({ state: "generated", origin: "manual" })]) {
      expect(visible(render(p, { onClose() {}, flightStep: { flightId: "f", stepId: "s" } }))).not.toMatch(/checklist|test plan/i);
    }
  });
});

describe("who does it", () => {
  it("the agent's hand-back makes it a person's", () => {
    const handed = plan({ state: "done", steps: [step("a")], runs: [run({ results: results({ a: "blocked" }) })] });
    expect(whoOf(handed, handed.steps[0])).toMatchObject({ you: true, handed: true, label: "🙋 you" });
    expect(whoOf(FAILED, FAILED.steps[0])).toMatchObject({ you: false, label: "🤖 agent" });
    expect(rowChip(handed, "a")).toEqual({ state: "yours", glyph: "●", label: "A person's" });
  });
});

describe("the ⋯ menu", () => {
  // The card hands the menu its head's view, so the primary is never repeated.
  const labels = (p: TestPlan, withSession = true) =>
    cardMenuItems(p, {
      summary: verifyCardView(p, behaviorSummary(p), null, withSession ? "p1" : ""),
      session: withSession ? (session as unknown as { title: string; pr_url?: string }) : null,
    }).map((i) => i.label);

  it("on a failure: fix, the checking run, the work session, diff, delete last — no rewrite or walk", () => {
    expect(labels(FAILED)).toEqual([
      "Fix in a new session",
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

  it("while checking: Cancel the check, no Verify again", () => {
    const p = plan({ state: "running", run_session: "verify-p1", steps: [step("a")], runs: [run({ session: "verify-p1" })] });
    const items = cardMenuItems(p, { summary: behaviorSummary(p) });
    expect(items.map((i) => i.label)).toEqual(["Open the checking run", "Cancel the check", "Delete these behaviors"]);
  });

  it("merged and waiting for the deploy: It's deployed — check now", () => {
    const p = plan({ state: "generated", live_at: 0, merged_at: 10, steps: [step("a")] });
    expect(labels(p, false)).toContain("It's deployed — check now");
  });

  it("no work session open: no Open the work session / View diff", () => {
    expect(labels(FAILED, false)).not.toContain("Open the work session");
    expect(labels(FAILED, false)).not.toContain("View diff");
  });

  it("the primary action is never repeated in the menu; a passed verify offers Verify again there", () => {
    const p = plan({ state: "due", steps: [step("a")] });
    const view = verifyCardView(p, behaviorSummary(p), null, "p1");
    expect(view.action).toMatchObject({ key: "verify", label: "Verify" });
    expect(cardMenuItems(p, { summary: view, session }).map((i) => i.key)).not.toContain("verify");
    const passed: SessionVerify = { state: "passed", plan: "p1", steps: { total: 2, agent_done: 2, human: 0 }, human_steps: [], failed_steps: [], detail: "", at: 1 };
    const pv = verifyCardView(p, behaviorSummary(p), passed, "p1");
    expect(pv.action).toBeNull();
    expect(cardMenuItems(p, { summary: pv, session: { ...session, verify: passed } }).find((i) => i.key === "verify")?.label).toBe("Verify again");
  });
});
