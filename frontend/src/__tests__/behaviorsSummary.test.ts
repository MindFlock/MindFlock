/** The words one session's behaviors wear (lib/behaviors.ts `behaviorSummary`,
 * docs/behaviors.md "Words" + §2): tone, status, the tile's short line, the
 * card head's sentence and its ONE primary action — for every state a plan
 * can be in. Plus the seam's lookups (planForSession, orphanPlans,
 * nextNeedingYou). */

import { describe, expect, it } from "vitest";
import type { TestPlan, TestRun, TestStep, TestStepActor, TestStepResult, TestStepResultEntry } from "../api/types";
import { GENERATION_STALE_S } from "../components/dialogs/verify";
import { behaviorSummary, nextNeedingYou, orphanPlans, planForSession, timingLine } from "../lib/behaviors";

const step = (id: string, actor: TestStepActor = "agent", text = "do " + id): TestStep => ({ id, text, expect: id + " worked", actor });

const results = (answers: Record<string, TestStepResult>, by = "agent", note = ""): Record<string, TestStepResultEntry> => {
  const map: Record<string, TestStepResultEntry> = {};
  for (const [id, result] of Object.entries(answers)) map[id] = { result, note, at: 100, by };
  return map;
};

const run = (over: Partial<TestRun> = {}): TestRun => ({ at: 100, by: "agent", session: "", results: {}, verdict: "pass", ...over });

const plan = (over: Partial<TestPlan> = {}): TestPlan => ({
  id: "p1",
  title: "Queue tab badges",
  repo_root: "/repo/shop",
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
  live_at: 0,
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

const TWO = [step("a"), step("b")];
const MIXED = [step("a"), step("h", "human")];

/** Every fixture below, for the vocabulary sweep at the end. */
const ALL: Record<string, TestPlan> = {
  writing: plan({ state: "generating" }),
  stalled: plan({ state: "generating", gen_started: Date.now() / 1000 - GENERATION_STALE_S - 5 }),
  unwritten: plan({ state: "failed", error: "The writer timed out. raw detail" }),
  noCommits: plan({ state: "failed", error: "This branch has no commits yet." }),
  emptyByChoice: plan({ state: "generated", origin: "manual" }),
  notMerged: plan({ state: "generated", steps: TWO }),
  notMergedHuman: plan({ state: "generated", steps: [step("h", "human")] }),
  merged: plan({ state: "generated", steps: TWO, merged_at: Date.now() / 1000 - 300 }),
  dueAgent: plan({ state: "due", live_at: 50, steps: MIXED }),
  dueHuman: plan({ state: "due", live_at: 50, steps: [step("h", "human"), step("i", "human")] }),
  checking: plan({ state: "running", run_session: "verify-p1", steps: TWO, live_at: 50, runs: [run({ session: "verify-p1" })] }),
  gone: plan({ state: "running", run_session: "", steps: TWO, live_at: 50, runs: [run({ results: results({ a: "pass" }) })] }),
  failed: plan({ state: "done", live_at: 50, steps: [step("a", "agent", "The badge shows the queue count"), step("b")], runs: [run({ results: results({ a: "fail", b: "fail" }) })] }),
  yours: plan({ state: "done", live_at: 50, steps: MIXED, runs: [run({ results: results({ a: "pass" }) })] }),
  verifiedLive: plan({ state: "done", live_at: 50, steps: TWO, runs: [run({ results: results({ a: "pass", b: "pass" }) })] }),
  verifiedBranch: plan({ state: "generated", steps: TWO, runs: [run({ prelive: true, results: results({ a: "pass", b: "pass" }) })] }),
  shippedAfterBranch: plan({ state: "due", live_at: 500, steps: TWO, runs: [run({ prelive: true, results: results({ a: "pass", b: "pass" }) })] }),
  stoppedEarly: plan({ state: "done", live_at: 50, steps: TWO, runs: [run({ session: "", by: "agent", results: results({ a: "pass" }) })] }),
  cantCheck: plan({ state: "done", live_at: 50, steps: MIXED, runs: [run({ results: { ...results({ a: "pass" }), ...results({ h: "blocked" }, "human") } })] }),
  rewriteFailed: plan({ state: "failed", live_at: 50, steps: TWO, error: "Rewriting failed. boom", runs: [run({ results: results({ a: "pass", b: "pass" }) })] }),
};

describe("behaviorSummary — status, tone, short line, action", () => {
  it("Writing… while the agent writes them; nothing to press", () => {
    const s = behaviorSummary(ALL.writing);
    expect(s).toMatchObject({ tone: "busy", status: "Writing…", short: "Writing…", action: null });
    expect(s.sentence).toBe("Writing the behaviors from the diff — up to three minutes.");
  });

  it("writing that stalled offers Write them", () => {
    const s = behaviorSummary(ALL.stalled);
    expect(s).toMatchObject({ tone: "quiet", status: "Not checked", action: { key: "write", label: "Write them" } });
    expect(s.sentence).toContain("Writing stopped part-way");
  });

  it("the agent couldn't write them: quiet, the reason, Write them", () => {
    const s = behaviorSummary(ALL.unwritten);
    expect(s).toMatchObject({ tone: "quiet", status: "Not checked", short: "None yet", action: { key: "write" } });
    expect(s.sentence).toBe("The agent couldn't write behaviors — the writer timed out.");
  });

  it("no commits yet fixes itself, so nothing to press", () => {
    const s = behaviorSummary(ALL.noCommits);
    expect(s.action).toBe(null);
    expect(s.sentence).toContain("They're written once the session commits.");
  });

  it("empty on purpose: Needs you, Write them (you add them)", () => {
    const s = behaviorSummary(ALL.emptyByChoice);
    expect(s).toMatchObject({ tone: "you", status: "Needs you", short: "Needs you", action: { key: "write", label: "Write them" } });
  });

  it("not merged and never checked: Not checked, Check now (only when the agent has one)", () => {
    const s = behaviorSummary(ALL.notMerged);
    expect(s).toMatchObject({ tone: "quiet", status: "Not checked", short: "Not checked", action: { key: "check", label: "Check now" } });
    expect(s.sentence).toBe("Not merged yet — check the branch now, or they're checked once it reaches main.");
    expect(behaviorSummary(ALL.notMergedHuman).action).toBe(null);
  });

  it("merged, waiting to deploy: says so, no button (⋯ has It's deployed)", () => {
    const s = behaviorSummary(ALL.merged);
    expect(s.action).toBe(null);
    expect(s.sentence).toMatch(/^Merged 5m ago — waiting for it to deploy\.$/);
  });

  it("live and unchecked: Needs you; Check now when the agent can, Answer when all are yours", () => {
    const a = behaviorSummary(ALL.dueAgent);
    expect(a).toMatchObject({ tone: "you", status: "Needs you", short: "Needs you · 1", action: { key: "check", label: "Check now" } });
    expect(a.sentence).toBe("Live — nobody has checked it yet. The agent can check 1 of 2; the rest are yours.");
    const h = behaviorSummary(ALL.dueHuman);
    expect(h).toMatchObject({ short: "Needs you · 2", action: { key: "answer", label: "Answer" } });
    expect(h.sentence).toBe("Live — all 2 behaviors are yours to check.");
  });

  it("Checking… while an agent runs them; nothing to press", () => {
    const s = behaviorSummary(ALL.checking);
    expect(s).toMatchObject({ tone: "busy", status: "Checking…", short: "Checking…", action: null });
    expect(s.sentence).toMatch(/^An agent is checking the behaviors it can/);
  });

  it("a run whose agent is gone: Rerun", () => {
    const s = behaviorSummary(ALL.gone);
    expect(s).toMatchObject({ tone: "quiet", action: { key: "rerun", label: "Rerun" } });
    expect(s.sentence).toContain("nothing is running now");
  });

  it("Failed names the first failed behavior and offers See what failed", () => {
    const s = behaviorSummary(ALL.failed);
    expect(s).toMatchObject({ tone: "bad", status: "Failed", short: "Failed · 2", failed: 2, action: { key: "failed", label: "See what failed" } });
    expect(s.sentence).toBe("Failed: “The badge shows the queue count” (+1 more).");
  });

  it("behaviors left for you after a run: Needs you · n, Answer", () => {
    const s = behaviorSummary(ALL.yours);
    expect(s).toMatchObject({ tone: "you", status: "Needs you", short: "Needs you · 1", yours: 1, verified: 1, action: { key: "answer", label: "Answer" } });
    expect(s.sentence).toBe("1 behavior needs you — an agent can't judge it.");
  });

  it("Verified live: every behavior passed, nothing to press", () => {
    const s = behaviorSummary(ALL.verifiedLive);
    expect(s).toMatchObject({ tone: "ok", status: "Verified live", short: "Verified live", verified: 2, total: 2, action: null });
    expect(s.sentence).toBe("Every behavior passed — checked by the agent.");
  });

  it("Verified on the branch says the honest timing line", () => {
    const s = behaviorSummary(ALL.verifiedBranch);
    expect(s).toMatchObject({ tone: "ok", status: "Verified" });
    expect(s.sentence).toContain("Checked before merge · checks again once live.");
    expect(timingLine(ALL.verifiedBranch)).toBe("Checked before merge · checks again once live");
  });

  it("shipped after a pre-merge check: Needs you, and names what that check found", () => {
    const s = behaviorSummary(ALL.shippedAfterBranch);
    expect(s).toMatchObject({ tone: "you", action: { key: "check" } });
    expect(s.sentence).toContain("Checked before merge (every behavior passed) · checks again once live.");
    expect(timingLine(ALL.verifiedLive)).toBe("");
  });

  it("a check that stopped early: n of m verified, Rerun", () => {
    const s = behaviorSummary(ALL.stoppedEarly);
    expect(s).toMatchObject({ tone: "quiet", status: "Not checked", short: "1 of 2 verified", action: { key: "rerun" } });
    expect(s.sentence).toBe("1 of 2 verified · the check stopped early — 1 with no answer.");
  });

  it("your Can't check is an answer, not a pass", () => {
    const s = behaviorSummary(ALL.cantCheck);
    expect(s).toMatchObject({ tone: "quiet", status: "Not checked", short: "1 of 2 verified" });
    expect(s.sentence).toBe("1 of 2 verified · you couldn't check 1.");
  });

  it("a rewrite that failed keeps the old behaviors and says so", () => {
    const s = behaviorSummary(ALL.rewriteFailed);
    expect(s.tone).toBe("ok");
    expect(s.sentence).toMatch(/^Rewriting didn't work — these are unchanged\. Every behavior passed/);
  });

  it("uses only the contract's status words and never the old vocabulary", () => {
    const WORDS = ["Writing…", "Not checked", "Checking…", "Needs you", "Failed", "Verified", "Verified live"];
    for (const [name, p] of Object.entries(ALL)) {
      const s = behaviorSummary(p);
      expect(WORDS, name).toContain(s.status);
      for (const text of [s.status, s.short, s.sentence, s.action?.label || ""]) {
        expect(text, name).not.toMatch(/checklist|test plan|\bsteps?\b|not verified|in progress|partial/i);
      }
    }
  });

  it("the primary action is always one of the five", () => {
    for (const p of Object.values(ALL)) {
      const a = behaviorSummary(p).action;
      if (a) expect(["Check now", "Rerun", "See what failed", "Write them", "Answer"]).toContain(a.label);
    }
  });
});

describe("the seam's lookups", () => {
  const mine = plan({ id: "sc-1", branch: "feature/a", repo_root: "/r/shop" });
  const other = plan({ id: "sc-2", branch: "feature/b", repo_root: "/r/shop" });

  it("planForSession: by title, then repo + branch; a verify run owns none", () => {
    expect(planForSession([mine, other], { title: "sc-1", branch: "x", repo: "y" })).toBe(mine);
    expect(planForSession([mine, other], { title: "sc-1-copy", branch: "feature/b", repo: "shop" })).toBe(other);
    expect(planForSession([mine, other], { title: "sc-9", branch: "feature/b", repo: "web" })).toBe(null);
    expect(planForSession([mine], { title: "verify-sc-1", branch: "feature/a", repo: "shop" })).toBe(null);
    expect(planForSession(null, { title: "sc-1", branch: "", repo: "" })).toBe(null);
  });

  it("orphanPlans: the plans no frame in the Sky owns", () => {
    expect(orphanPlans([mine, other], [{ title: "sc-1", branch: "feature/a", repo: "shop" }])).toEqual([other]);
    expect(orphanPlans([mine, other], [])).toEqual([mine, other]);
    expect(orphanPlans(undefined, [])).toEqual([]);
  });

  it("nextNeedingYou: needs you first, then failed, else null", () => {
    expect(nextNeedingYou([ALL.failed, ALL.yours])).toBe(ALL.yours);
    expect(nextNeedingYou([ALL.verifiedLive, ALL.failed])).toBe(ALL.failed);
    expect(nextNeedingYou([ALL.verifiedLive, ALL.writing])).toBe(null);
    expect(nextNeedingYou(null)).toBe(null);
  });
});
