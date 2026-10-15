/** The Verify rules, tested against plain fixtures.
 *
 * These functions are the only place the Verify surface decides anything:
 * the top-bar badge, the first group's heading, each row's sentence and each
 * row's button are four renderers of one question, and they agree only because
 * they all call in here.
 * So the tests are written against the QUESTIONS a reader asks ("has anyone
 * checked this?", "is it waiting on me?") rather than against the shape of the
 * data, and every case that matters is a case where the naive implementation
 * would give a comfortable answer that happens to be a lie — an absent result
 * read as a pass, a stale run read as the current one, a plan that came back
 * with a list for you filed under "Done".
 */

import { describe, it, expect } from "vitest";
import {
  cantCheckCount,
  dueCount,
  failCount,
  failingCount,
  GENERATION_STALE_S,
  groupPlans,
  isGenerationStalled,
  isWaitingOnYou,
  latestRun,
  liveBranchOverridden,
  CHECK_MARK,
  checkTally,
  closedTargets,
  isYourAnswer,
  needsConfirmation,
  noTargetsReason,
  openHumanSteps,
  planForSession,
  planGroup,
  planShipped,
  planStatus,
  planTargets,
  planVerdict,
  rewriteBlockedReason,
  rewriteWarning,
  stepCheck,
  canRunNow,
  errorHeadline,
  HEADLINE_MAX,
  planMatches,
  noteDraftAfter,
  runElapsed,
  stepKeyAction,
  stepKeyAllowed,
  stepKeyIsUndo,
  tallyBits,
  tallySentence,
  stepResult,
  unansweredCount,
  verdictOf,
  beforeMergeRun,
  beforeMergeSummary,
  checklistTargets,
  isBeforeMerge,
  verifyDeepLinkPlan,
} from "../components/dialogs/verify";
import type {
  Instance,
  TestPlan,
  TestRun,
  TestStep,
  TestStepActor,
  TestStepResult,
  TestStepResultEntry,
} from "../api/types";

/** One step. The text and the expectation are never read by these rules — only
 * the id and the actor are — so they are filled with something recognisable and
 * otherwise ignored. */
const step = (id: string, actor: TestStepActor = "agent"): TestStep => ({
  id,
  text: "do " + id,
  expect: id + " worked",
  actor,
});

/** A run's results map, built from the only part a test cares about: which step
 * got which answer. The store always writes the full entry (note/at/by), and
 * spelling that out per step would bury the one field under test. */
const results = (
  answers: Record<string, TestStepResult>,
  note = "",
): Record<string, TestStepResultEntry> => {
  const map: Record<string, TestStepResultEntry> = {};
  for (const [id, result] of Object.entries(answers)) {
    map[id] = { result, note, at: 100, by: "agent" };
  }
  return map;
};

const run = (over: Partial<TestRun> = {}): TestRun => ({
  at: 100,
  by: "agent",
  session: "verify-p1",
  results: {},
  // Deliberately "pass" in the default, and deliberately not what the rules
  // read: every verdict test below sets step results that contradict this
  // stamp, which is how they prove the verdict is recomputed rather than
  // trusted. See the note atop verify.ts.
  verdict: "pass",
  ...over,
});

const plan = (over: Partial<TestPlan> = {}): TestPlan => ({
  id: "p1",
  title: "Queue tab badges",
  repo_root: "/repo",
  branch: "feature/p1",
  sha: "a1b2c3d4",
  live_branch: "main",
  effective_live_branch: "main",
  state: "due",
  error: "",
  generated_at: 10,
  // NOW, not 10 like the others: this stamp is read as an age rather than as an
  // ordering key, and a fixed one would make every `generating` plan in this
  // file permanently stalled — which is a real state, but not the one those
  // tests are about. The stall tests set an old stamp explicitly.
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

describe("latestRun", () => {
  it("has no run to show for a plan that has never been run", () => {
    // The normal state of a plan between generation and going live. It is not
    // an error and must not read as an empty run, which would make every
    // downstream rule treat "nobody has started" as "everything unchecked".
    expect(latestRun(plan())).toBe(null);
  });

  it("returns the only run there is", () => {
    const only = run({ session: "verify-p1" });
    expect(latestRun(plan({ runs: [only] }))).toBe(only);
  });

  it("picks the newest by timestamp, not the last element", () => {
    // finish_run appends, so position and time normally agree. They stop
    // agreeing after a hand-edited test_plans.json or a re-run recorded out of
    // order, and showing a stale verdict there is silent and unexplainable.
    const older = run({ at: 100, session: "verify-old" });
    const newer = run({ at: 300, session: "verify-new" });
    expect(latestRun(plan({ runs: [newer, older] }))?.session).toBe("verify-new");
  });

  it("breaks a same-second tie in favour of the later append", () => {
    // Two runs can land in the same epoch second; append order is then the only
    // remaining evidence of which one happened last.
    const first = run({ at: 200, session: "verify-first" });
    const second = run({ at: 200, session: "verify-second" });
    expect(latestRun(plan({ runs: [first, second] }))?.session).toBe(
      "verify-second",
    );
  });
});

describe("stepResult", () => {
  const ran = plan({
    steps: [step("s1"), step("s2", "human")],
    runs: [run({ results: results({ s1: "pass" }) })],
  });

  it("finds what the latest run recorded for a step", () => {
    expect(stepResult(ran, "s1")?.result).toBe("pass");
  });

  it("returns null for a step the run never recorded", () => {
    // The results map is sparse on purpose — a run that gives up half way
    // settles only what it reached — so a miss is routine, not a bug, and the
    // row renders as unchecked rather than blowing up on undefined.result.
    expect(stepResult(ran, "s2")).toBe(null);
    expect(stepResult(ran, "no-such-step")).toBe(null);
  });

  it("returns null when there is no run at all", () => {
    expect(stepResult(plan({ steps: [step("s1")] }), "s1")).toBe(null);
  });

  it("keeps an unsettled entry instead of nulling it out", () => {
    // "blocked" with a reason is the most useful thing the agent produces: the
    // row wants to render "needs a real browser" next to the step. Only the
    // rules that ask "is this done?" care that the result isn't final.
    const blocked = plan({
      steps: [step("s1", "human")],
      runs: [run({ results: results({ s1: "blocked" }, "needs a real browser") })],
    });
    expect(stepResult(blocked, "s1")).toEqual({
      result: "blocked",
      note: "needs a real browser",
      at: 100,
      by: "agent",
    });
  });
});

describe("verdictOf", () => {
  it("says none — not partial — before anything has run", () => {
    // The two mean opposite things to a reader: "none" is the normal state of a
    // plan that just went live, "partial" is a to-do left behind by a run.
    expect(verdictOf(plan({ steps: [step("s1")] }))).toBe("none");
  });

  it("passes only when every step settled as a pass", () => {
    const done = plan({
      steps: [step("s1"), step("s2")],
      runs: [run({ results: results({ s1: "pass", s2: "pass" }) })],
    });
    expect(verdictOf(done)).toBe("pass");
  });

  it("fails as soon as one step failed, whatever the rest did", () => {
    const broken = plan({
      steps: [step("s1"), step("s2"), step("s3")],
      runs: [run({ results: results({ s1: "pass", s2: "fail", s3: "blocked" }) })],
    });
    expect(verdictOf(broken)).toBe("fail");
  });

  it("is partial while a step is blocked", () => {
    const waiting = plan({
      steps: [step("s1"), step("s2", "human")],
      runs: [run({ results: results({ s1: "pass", s2: "blocked" }) })],
    });
    expect(verdictOf(waiting)).toBe("partial");
  });

  it("is partial when the run simply never mentioned a step", () => {
    // The step list is the authority for what had to be checked. Reading only
    // the results map would see one pass and no failures and call this plan
    // verified, with two of its three steps never attempted — the exact lie the
    // whole surface exists to prevent.
    const halfRun = plan({
      steps: [step("s1"), step("s2"), step("s3")],
      runs: [run({ results: results({ s1: "pass" }) })],
    });
    expect(verdictOf(halfRun)).toBe("partial");
  });

  it("treats an empty result the same as a missing one", () => {
    // "" is the store's not-yet placeholder; it is a row that exists, not an
    // answer that was given.
    const placeheld = plan({
      steps: [step("s1"), step("s2")],
      runs: [run({ results: results({ s1: "pass", s2: "" }) })],
    });
    expect(verdictOf(placeheld)).toBe("partial");
  });

  it("recomputes rather than believing the run's stamped verdict", () => {
    // The stamp is a snapshot of the moment the agent finished; a person then
    // confirms the blocked steps one at a time. Trusting it would leave a
    // fully-confirmed plan reading "partial" forever.
    const confirmed = plan({
      steps: [step("s1"), step("s2", "human")],
      runs: [
        run({
          verdict: "partial",
          results: results({ s1: "pass", s2: "pass" }),
        }),
      ],
    });
    expect(verdictOf(confirmed)).toBe("pass");
  });

  it("falls back to the results map when the plan has no steps", () => {
    // A regeneration can empty the steps out from under a recorded run. The map
    // is then all the evidence there is, and it is better than nothing.
    const stepless = plan({
      steps: [],
      runs: [run({ results: results({ s1: "pass", s2: "pass" }) })],
    });
    expect(verdictOf(stepless)).toBe("pass");
    expect(
      verdictOf(plan({ steps: [], runs: [run({ results: results({ s1: "fail" }) })] })),
    ).toBe("fail");
  });

  it("is partial when a run recorded nothing at all", () => {
    // No steps and no results: nothing was verified, so a "pass" here would be
    // invented out of an absence of evidence.
    expect(verdictOf(plan({ steps: [], runs: [run()] }))).toBe("partial");
  });
});

describe("needsConfirmation", () => {
  it("is true while a human step is unresolved", () => {
    const handedBack = plan({
      state: "done",
      steps: [step("s1"), step("s2", "human")],
      runs: [run({ results: results({ s1: "pass", s2: "blocked" }) })],
    });
    expect(needsConfirmation(handedBack)).toBe(true);
  });

  it("counts a human step the run never reached, not just a blocked one", () => {
    // The run prompt tells the agent to mark human steps blocked, but an agent
    // that dies half way leaves them simply absent. Both mean unchecked.
    const abandoned = plan({
      steps: [step("s1"), step("s2", "human")],
      runs: [run({ results: results({ s1: "pass" }) })],
    });
    expect(needsConfirmation(abandoned)).toBe(true);
  });

  it("is false once every human step has a real result", () => {
    const settled = plan({
      state: "done",
      steps: [step("s1"), step("s2", "human"), step("s3", "human")],
      runs: [
        run({ results: results({ s1: "pass", s2: "pass", s3: "fail" }) }),
      ],
    });
    // A human "fail" is an answer, not an outstanding question — it belongs in
    // the verdict, not in your to-do list.
    expect(needsConfirmation(settled)).toBe(false);
  });

  it("counts an agent step the agent handed back — it is yours now", () => {
    // `blocked` from an agent is the agent saying the checklist guessed wrong
    // about who could settle this: it went, it tried, it can't. That step needs
    // a person, so it is one of yours from that moment — and the row's sentence
    // has to say so, because every mark inside the plan already did. It used to
    // be read as "the agent's unfinished business" and left out of the count,
    // which is how a plan whose run handed back six of its eight steps came up
    // as "2 steps need your eyes" over a body showing eight.
    const agentStuck = plan({
      steps: [step("s1"), step("s2")],
      runs: [run({ results: results({ s1: "pass", s2: "blocked" }) })],
    });
    expect(needsConfirmation(agentStuck)).toBe(true);
  });

  it("does not count YOUR own can't-check as still open", () => {
    // Same wire value, opposite event: `blocked` recorded by a person is the
    // answer the legend teaches them to give, and re-asking for it is what the
    // handover rule above must never do.
    const youAnswered = plan({
      steps: [step("s1"), step("s2")],
      runs: [
        run({
          results: {
            s1: { result: "pass", note: "", at: 100, by: "agent" },
            s2: { result: "blocked", note: "", at: 100, by: "human" },
          },
        }),
      ],
    });
    expect(needsConfirmation(youAnswered)).toBe(false);
  });

  it("is false before anything has run, however many human steps there are", () => {
    // Nobody has started; the plan is waiting on the agent or on going live.
    // Calling it "awaiting your confirmation" would put unstarted work in your
    // lap.
    expect(
      needsConfirmation(plan({ steps: [step("s1", "human"), step("s2", "human")] })),
    ).toBe(false);
  });

  it("reads only the latest run", () => {
    // An older run that left a step blocked has been superseded; re-raising it
    // would make a confirmed plan impossible to clear.
    const rerun = plan({
      steps: [step("s1", "human")],
      runs: [
        run({ at: 100, results: results({ s1: "blocked" }) }),
        run({ at: 200, results: results({ s1: "pass" }) }),
      ],
    });
    expect(needsConfirmation(rerun)).toBe(false);
  });
});

describe("dueCount", () => {
  const humanStep = [step("s1", "human")];
  const unconfirmed = [run({ results: results({ s1: "blocked" }) })];
  const one = [step("s1")];

  it("counts a shipped plan nobody has checked, but not one an agent is checking", () => {
    // Round 4, one axis: a running plan is "In progress" — its row says
    // "Agent checking" — so it is not on the badge, which counts Needs you +
    // Failed.
    expect(
      dueCount([
        plan({ id: "a", state: "due", steps: one }),
        plan({ id: "b", state: "running", steps: one, run_session: "verify-b" }),
      ]),
    ).toBe(1);
  });

  it("counts a plan whose human steps are still open, whatever its state", () => {
    // The done-with-a-list case: the agent finished, and what came back is a
    // short list for you. This is the badge's most important entry.
    expect(
      dueCount([plan({ id: "a", state: "done", steps: humanStep, runs: unconfirmed })]),
    ).toBe(1);
  });

  it("ignores plans that are not yet the reader's problem", () => {
    // generating is still being written; generated is waiting for the branch to
    // reach the live branch. Nothing to check until it ships.
    expect(
      dueCount([
        plan({ id: "a", state: "generating", steps: one }),
        plan({ id: "b", state: "generated", steps: one }),
      ]),
    ).toBe(0);
  });

  it("drops a shipped plan from the badge while a rewrite runs — an agent is on it", () => {
    expect(
      dueCount([plan({ id: "a", state: "generating", live_at: 500, steps: one })]),
    ).toBe(0);
  });

  it("counts a failed step — and NOT a checklist that couldn't be written", () => {
    // Round 4: failures are loud, and the badge is where a lead looks first.
    // Round 5 (M4): "Not written yet" is grey and off the badge — it says
    // nothing about whether the change works.
    expect(
      dueCount([
        plan({
          id: "a",
          state: "done",
          steps: one,
          runs: [run({ results: results({ s1: "fail" }) })],
        }),
        plan({ id: "b", state: "failed", error: "no <testplan> block" }),
      ]),
    ).toBe(1);
    expect(
      failingCount([
        plan({ id: "b", state: "failed", error: "no <testplan> block" }),
        plan({ id: "c", state: "due", steps: one }),
      ]),
    ).toBe(0);
  });

  it("ignores a verified plan", () => {
    expect(
      dueCount([
        plan({
          id: "a",
          state: "done",
          steps: [step("s1", "human")],
          runs: [run({ results: results({ s1: "pass" }) })],
        }),
      ]),
    ).toBe(0);
  });

  it("counts a plan once even when both rules apply", () => {
    // A due plan whose earlier run left human steps open matches the state test
    // AND the confirmation test; counting it twice would make the badge exceed
    // the number of rows the dialog can show.
    expect(
      dueCount([plan({ id: "a", state: "due", steps: humanStep, runs: unconfirmed })]),
    ).toBe(1);
  });

  it("is zero, not broken, before any plan exists", () => {
    expect(dueCount([])).toBe(0);
  });
});

describe("the badge and the first two groups are one set", () => {
  // The whole point of dueCount. One number used to be printed in four places
  // and no two of them were ever equal, which taught the reader that the badge
  // was approximate. This is that promise, machine-checked: the badge counts
  // exactly the rows under "Failed" and "Needs you".
  const one = [step("s1")];
  const everything = [
    plan({ id: "due", state: "due", steps: one }),
    plan({ id: "running", state: "running", steps: one, run_session: "verify-r" }),
    plan({ id: "generating", state: "generating", steps: one }),
    plan({ id: "generated", state: "generated", steps: one }),
    plan({
      id: "rewriting-shipped",
      state: "generating",
      live_at: 500,
      steps: one,
    }),
    plan({
      id: "rewriting-checked",
      state: "generating",
      live_at: 500,
      steps: one,
      runs: [run({ results: results({ s1: "pass" }) })],
    }),
    plan({ id: "nogen", state: "failed", error: "no <testplan> block" }),
    plan({
      id: "handed-back",
      state: "done",
      steps: [step("s1", "human")],
      runs: [run({ results: results({ s1: "blocked" }) })],
    }),
    plan({
      id: "flunked",
      state: "done",
      steps: one,
      runs: [run({ results: results({ s1: "fail" }) })],
    }),
    plan({
      id: "clean",
      state: "done",
      steps: one,
      runs: [run({ results: results({ s1: "pass" }) })],
    }),
  ];

  it("counts exactly the plans the first two groups render", () => {
    const top = groupPlans(everything).filter((g) => g.key === "due" || g.key === "fail");
    expect(dueCount(everything)).toBe(top.reduce((n, g) => n + g.plans.length, 0));
  });

  it("agrees plan by plan, not just in total", () => {
    const due = groupPlans(everything).find((g) => g.key === "due");
    expect(due?.plans.map((p) => p.id)).toEqual(["due", "handed-back"]);
    const fail = groupPlans(everything).find((g) => g.key === "fail");
    expect(fail?.plans.map((p) => p.id)).toEqual(["flunked"]);
    expect(groupPlans(everything).find((g) => g.key === "failed")?.plans.map((p) => p.id)).toEqual([
      "nogen",
    ]);
    expect(everything.filter(isWaitingOnYou).map((p) => p.id)).toEqual(["due", "handed-back"]);
  });

  it("puts every plan in exactly one group", () => {
    const filed = groupPlans(everything).flatMap((g) => g.plans.map((p) => p.id));
    expect(filed.slice().sort()).toEqual(everything.map((p) => p.id).sort());
  });
});

describe("failCount / unansweredCount / openHumanSteps", () => {
  it("are all zero before anything has run", () => {
    // An unchecked step is not a failed one, and a plan nobody has run is not a
    // plan that stopped early. Confusing either would put red on a row that has
    // simply not been looked at yet.
    const fresh = plan({ steps: [step("s1"), step("s2", "human")] });
    expect(failCount(fresh)).toBe(0);
    expect(unansweredCount(fresh)).toBe(0);
    expect(openHumanSteps(fresh)).toEqual([]);
  });

  it("are all zero for a withdrawn run", () => {
    // The phantom: every answer taken back. Nothing was checked, so nothing
    // failed and nothing is outstanding.
    const withdrawn = plan({
      steps: [step("s1"), step("s2", "human")],
      runs: [run({ by: "human", session: "", results: results({ s1: "", s2: "" }) })],
    });
    expect(failCount(withdrawn)).toBe(0);
    expect(unansweredCount(withdrawn)).toBe(0);
    expect(openHumanSteps(withdrawn)).toEqual([]);
  });

  it("counts a step the run never mentioned as unanswered", () => {
    // The step list is the authority. Reading only the results map would see one
    // pass, no failures, and call a plan verified with two of three steps never
    // attempted.
    const halfRun = plan({
      steps: [step("s1"), step("s2"), step("s3")],
      runs: [run({ results: results({ s1: "pass" }) })],
    });
    expect(unansweredCount(halfRun)).toBe(2);
    expect(failCount(halfRun)).toBe(0);
  });

  it("counts blocked as unanswered, never as an outcome", () => {
    const blocked = plan({
      steps: [step("s1"), step("s2")],
      runs: [run({ results: results({ s1: "pass", s2: "blocked" }) })],
    });
    expect(unansweredCount(blocked)).toBe(1);
    expect(failCount(blocked)).toBe(0);
  });

  it("never disagrees with the verdict about a failure", () => {
    const broken = plan({
      steps: [step("s1"), step("s2"), step("s3")],
      runs: [run({ results: results({ s1: "pass", s2: "fail", s3: "fail" }) })],
    });
    expect(failCount(broken)).toBe(2);
    expect(verdictOf(broken)).toBe("fail");
  });

  it("hands back the steps themselves, in plan order", () => {
    // The row needs the list, not just the size: the count goes on the button
    // and the first of them is what pressing it scrolls to.
    const handedBack = plan({
      steps: [step("s1"), step("s2", "human"), step("s3", "human")],
      runs: [run({ results: results({ s1: "pass", s2: "blocked" }) })],
    });
    expect(openHumanSteps(handedBack).map((s) => s.id)).toEqual(["s2", "s3"]);
  });
});

describe("planStatus (the one sentence and the one button)", () => {
  it("never says it works while a step is waiting on you", () => {
    // The precedence that matters most. A done plan with eleven passes and one
    // blocked human step must read as an ask, not as a success.
    const handedBack = plan({
      state: "done",
      steps: [step("s1"), step("s2", "human")],
      runs: [run({ results: results({ s1: "pass", s2: "blocked" }) })],
    });
    const status = planStatus(handedBack, "main");
    expect(status.group).toBe("due");
    expect(status.tone).toBe("you");
    expect(status.line).toBe("1 step needs your eyes — an agent can't judge it.");
    expect(status.actionLabel).toBe("Answer 1 step");
  });

  it("never says it works while a step failed", () => {
    const flunked = plan({
      state: "done",
      steps: [step("s1"), step("s2")],
      runs: [run({ results: results({ s1: "fail", s2: "fail" }) })],
    });
    const status = planStatus(flunked, "main");
    expect(status.group).toBe("fail");
    expect(status.tone).toBe("bad");
    // Named, not counted: the first failing step, and how many more.
    expect(status.line).toBe("Failed: step 1 — do s1 (+1 more).");
  });

  it("leads a failed row with the failure, even when steps are still yours", () => {
    // It files under Failed; leading with "1 step needs your eyes" in the
    // your-turn tone read as an ask. The open step is still said, after it.
    const mixed = plan({
      state: "done",
      steps: [step("s1"), step("s2", "human")],
      runs: [run({ results: results({ s1: "fail", s2: "blocked" }) })],
    });
    const status = planStatus(mixed, "main");
    expect(status.tone).toBe("bad");
    expect(status.line).toBe("Failed: step 1 — do s1. 1 step still needs you.");
    expect(status.actionLabel).toBe("See what failed");
  });

  it("never says it works when the run stopped short", () => {
    // No failures and no human steps, but three agent steps were never reached.
    // Calling that a pass is the exact lie the surface exists to prevent.
    const short = plan({
      state: "done",
      steps: [step("s1"), step("s2"), step("s3")],
      runs: [run({ results: results({ s1: "pass" }) })],
    });
    const status = planStatus(short, "main");
    expect(status.tone).toBe("warn");
    expect(status.group).toBe("partial");
    expect(status.line).toBe("Not verified — 2 steps with no answer.");
    // One name for the agent's run, everywhere.
    expect(status.actionLabel).toBe("Check now");
  });

  it("says it works only when every step has a real answer", () => {
    const clean = plan({
      state: "done",
      steps: [step("s1"), step("s2", "human")],
      runs: [run({ results: results({ s1: "pass", s2: "pass" }) })],
    });
    const status = planStatus(clean, "main");
    expect(status.group).toBe("done");
    expect(status.tone).toBe("ok");
    // M7: "Every step passed", never "it works" — the legend's own rule.
    expect(status.line).toBe("Every step passed.");
  });

  it("asks you to run a shipped plan nobody has touched", () => {
    const shipped = plan({ state: "due", live_at: 500, steps: [step("s1")] });
    const status = planStatus(shipped, "main");
    expect(status.tone).toBe("you");
    expect(status.line).toBe(
      "Shipped — nobody has checked it yet. An agent can check all 1 step.",
    );
    expect(status.action).toBe("run");
    // The label names the actor, because "Run" reads as "run the tests" —
    // seconds, local, free — and this provisions a workspace and minutes of a
    // billed session.
    expect(status.actionLabel).toBe("Check now");
  });

  it("offers Check now on a plan that has not merged", () => {
    // Checking before merge is first-class now: the server keeps the checklist
    // open, so it comes back to be checked again after merge. It used to be a
    // ⋯ item behind a confirm warning that it closed the checklist for good.
    const early = plan({ state: "generated", live_at: 0, steps: [step("s1")] });
    const status = planStatus(early, "main");
    expect(status.group).toBe("generated");
    expect(status.action).toBe("early");
    expect(status.actionLabel).toBe("Check now");
    expect(status.line).toBe(
      "Not merged yet — check the branch now, or it comes back here after it reaches main.",
    );
  });

  it("says Show steps, not Check now, when every step is a person's", () => {
    // "Check now" means one thing — start the agent's steps. With none, the
    // button only opens the steps, and says so.
    const early = plan({ state: "generated", live_at: 0, steps: [step("s1", "human")] });
    const status = planStatus(early, "main");
    expect(status.action).toBe("answer");
    expect(status.actionLabel).toBe("Show steps");
  });

  it("says a branch that passed before merge is checked again after it", () => {
    const early = plan({
      state: "generated",
      live_at: 0,
      steps: [step("s1")],
      runs: [run({ results: results({ s1: "pass" }), prelive: true } as Partial<TestRun>)],
    });
    const status = planStatus(early, "main");
    expect(status.tone).toBe("ok");
    expect(status.line).toBe(
      "Every step passed on the branch — it comes back to be checked again after merge.",
    );
  });

  it("names the plan's own live branch, not the flock-wide one", () => {
    // "What counts as shipped" is a per-repo fact, and a row that named the
    // wrong branch would be an assertion the reader cannot check.
    const early = plan({ state: "generated", live_branch: "staging", steps: [step("s1")] });
    expect(planStatus(early, "main").line).toBe(
      "Not merged yet — check the branch now, or it comes back here after it reaches staging.",
    );
  });

  it("says an agent is on it while a run is going", () => {
    const running = plan({ state: "running", steps: [step("s1")], run_session: "verify-a" });
    const status = planStatus(running, "main");
    expect(status.group).toBe("generated");
    expect(status.action).toBe("watch");
    expect(status.actionLabel).toBe("Watch");
  });

  it("distinguishes a plan being written from one that came back empty", () => {
    // They look identical — no steps, nothing to do — and only one of them is
    // going to fix itself.
    expect(planStatus(plan({ state: "generating" })).line).toBe(
      "Writing the checklist from the diff — up to three minutes.",
    );
    expect(planStatus(plan({ state: "due", steps: [] })).line).toBe(
      "Not written yet — it came back with no steps.",
    );
    expect(planStatus(plan({ state: "due", steps: [] })).action).toBe("rewrite");
  });

  it("stops claiming a plan is being written once nothing is writing it", () => {
    // The app was closed mid-generation: the thread that would have finished
    // this plan died with the process, so "up to three minutes" became forever
    // — with no button on the row, because `generating` deliberately offers
    // none. Past the server's own stale window the row has to say what happened
    // and hand the rewrite back.
    const stuck = plan({
      state: "generating",
      gen_started: Date.now() / 1000 - (GENERATION_STALE_S + 1),
    });
    const status = planStatus(stuck, "main");
    // Grey since round 5 (M4): not written is not a failure.
    expect(status.tone).toBe("wait");
    expect(status.action).toBe("rewrite");
    expect(status.actionLabel).toBe("Rewrite the checklist");
    expect(status.line).toContain("stopped part-way");
  });

  it("leaves a generation that is merely slow alone", () => {
    // The other half, and the one that must not be lost: the window is the
    // model call's own budget, so a plan a minute into a three-minute answer is
    // working, not stuck, and offering a rewrite would invite a second model
    // call that races the first.
    const busy = plan({
      state: "generating",
      gen_started: Date.now() / 1000 - (GENERATION_STALE_S - 30),
    });
    expect(planStatus(busy).action).toBe("none");
    expect(isGenerationStalled(busy)).toBe(false);
  });

  it("treats a plan with no start stamp as stalled", () => {
    // Exactly the plans that are stuck TODAY: written by a build that never
    // recorded when generation began. An unstamped plan cannot be shown to be
    // in flight, and the recoverable reading is the useful one.
    expect(isGenerationStalled(plan({ state: "generating", gen_started: 0 }))).toBe(
      true,
    );
    // …but only in the one state where a stamp means anything.
    expect(isGenerationStalled(plan({ state: "due", gen_started: 0 }))).toBe(false);
  });

  it("hands a failed generation the only button that helps", () => {
    const broken = plan({ state: "failed", error: "no <testplan> block" });
    const status = planStatus(broken, "main");
    expect(status.group).toBe("failed");
    expect(status.tone).toBe("wait");
    expect(status.actionLabel).toBe("Rewrite the checklist");
    // M1: the reason is in the headline, not only in the expanded body.
    expect(status.line).toBe("Not written yet — no <testplan> block.");
  });

  it("offers nothing to press when the branch simply has no commits yet (M1)", () => {
    const empty = plan({
      state: "failed",
      error: "nothing to check yet — this branch has no commits",
    });
    const status = planStatus(empty, "main");
    expect(status.action).toBe("none");
    expect(status.line).toBe(
      "Not written yet — this branch has no commits. Comes back automatically when the session commits.",
    );
  });

  it("says a write past three minutes is taking longer than usual (m11)", () => {
    const slow = plan({ state: "generating", gen_started: Date.now() / 1000 - 200 });
    expect(planStatus(slow).line).toBe("Writing the checklist — taking longer than usual…");
    expect(planStatus(slow).action).toBe("none");
  });

  it("takes its group from planGroup rather than deciding twice", () => {
    // The property the whole design rests on: the heading, the sentence and the
    // button cannot contradict each other because there is one decision behind
    // all three.
    const cases = [
      plan({ id: "a", state: "due" }),
      plan({ id: "b", state: "generated" }),
      plan({ id: "c", state: "failed" }),
      plan({
        id: "d",
        state: "done",
        steps: [step("s1")],
        runs: [run({ results: results({ s1: "fail" }) })],
      }),
      plan({
        id: "e",
        state: "done",
        steps: [step("s1")],
        runs: [run({ results: results({ s1: "pass" }) })],
      }),
    ];
    for (const p of cases) expect(planStatus(p, "main").group).toBe(planGroup(p));
  });
});

describe("groupPlans", () => {
  const one = [step("s1")];

  it("groups by ONE axis, loudest first", () => {
    // Round 4: the outcome and whose move it is — never "merged?" mixed in.
    // The input is deliberately shuffled: nothing about the caller's order may
    // leak into it.
    const groups = groupPlans([
      plan({ id: "e", state: "failed" }),
      plan({
        id: "d",
        state: "done",
        steps: one,
        runs: [run({ results: results({ s1: "pass" }) })],
      }),
      plan({ id: "c", state: "generated", steps: one }),
      plan({
        id: "f",
        state: "done",
        steps: one,
        runs: [run({ results: results({ s1: "fail" }) })],
      }),
      plan({
        id: "g",
        state: "done",
        steps: [step("s1"), step("s2")],
        runs: [run({ results: results({ s1: "pass" }) })],
      }),
      plan({
        id: "b",
        state: "done",
        steps: [step("s1", "human")],
        runs: [run({ results: results({ s1: "blocked" }) })],
      }),
      plan({ id: "a", state: "due", steps: one }),
    ]);
    expect(groups.map((g) => g.key)).toEqual(["fail", "due", "generated", "failed", "partial", "done"]);
    expect(groups.map((g) => g.label)).toEqual([
      "Failed",
      "Needs you",
      "In progress",
      "Not written yet",
      "Not verified",
      "Verified",
    ]);
    expect(groups[0].plans.map((p) => p.id)).toEqual(["f"]);
    expect(groups[3].plans.map((p) => p.id)).toEqual(["e"]);
  });

  it("files a branch that passed before merge under Verified, not 'not shipped'", () => {
    // The judge's core complaint: every step passed on an unmerged branch,
    // and the row jumped into "Not shipped yet" — never "Checked".
    const groups = groupPlans([
      plan({
        id: "a",
        state: "generated",
        live_at: 0,
        steps: one,
        runs: [run({ results: results({ s1: "pass" }), prelive: true } as Partial<TestRun>)],
      }),
    ]);
    expect(groups.map((g) => g.key)).toEqual(["done"]);
    expect(groups[0].label).toBe("Verified");
  });

  it("gives a shipped thing that flunked its own plan a group of its own", () => {
    // finish_run writes state="done" whatever the results said, so without this
    // the single most valuable output of the whole feature — it shipped and it
    // is broken — filed among the successes.
    const groups = groupPlans([
      plan({
        id: "a",
        state: "done",
        steps: [step("s1"), step("s2")],
        runs: [run({ results: results({ s1: "pass", s2: "fail" }) })],
      }),
    ]);
    expect(groups.map((g) => g.key)).toEqual(["fail"]);
  });

  it("counts a fail on the badge — failures are loud", () => {
    const flunked = plan({
      id: "a",
      state: "done",
      steps: one,
      runs: [run({ results: results({ s1: "fail" }) })],
    });
    expect(dueCount([flunked])).toBe(1);
    expect(planGroup(flunked)).toBe("fail");
  });

  it("files a run that stopped short under Not verified, apart from the verified", () => {
    const groups = groupPlans([
      plan({
        id: "clean",
        state: "done",
        steps: one,
        runs: [run({ results: results({ s1: "pass" }) })],
      }),
      plan({
        id: "short",
        state: "done",
        steps: [step("s1"), step("s2")],
        runs: [run({ results: results({ s1: "pass" }) })],
      }),
    ]);
    expect(groups.map((g) => g.key)).toEqual(["partial", "done"]);
    expect(groups[0].plans.map((p) => p.id)).toEqual(["short"]);
    expect(groups[1].detail).toBe(undefined);
  });

  it("omits groups that would render empty", () => {
    // The steady state is one group with one item in it; four empty headings
    // around it would bury the only thing worth reading.
    const groups = groupPlans([plan({ id: "a", state: "due", steps: one })]);
    expect(groups.map((g) => g.key)).toEqual(["due"]);
    expect(groups[0].plans.map((p) => p.id)).toEqual(["a"]);
  });

  it("has nothing to group when there are no plans", () => {
    expect(groupPlans([])).toEqual([]);
  });

  it("files a plan awaiting confirmation with the rest of what needs you", () => {
    const groups = groupPlans([
      plan({
        id: "a",
        state: "due",
        steps: [step("s1", "human")],
        runs: [run({ results: results({ s1: "blocked" }) })],
      }),
    ]);
    expect(groups.map((g) => g.key)).toEqual(["due"]);
  });

  it("lets a failure outrank an open step for you", () => {
    // A plan can be both; the verdict is "Failed", and the red group is where a
    // lead about to merge looks first. Its open step is still counted on the
    // heading and answerable inside.
    const groups = groupPlans([
      plan({
        id: "a",
        state: "done",
        steps: [step("s1"), step("s2", "human")],
        runs: [run({ results: results({ s1: "fail", s2: "blocked" }) })],
      }),
    ]);
    expect(groups.map((g) => g.key)).toEqual(["fail"]);
    expect(groups[0].detail).toBe("1 step failed · 1 step needs you");
  });

  it("files an agent at work under In progress", () => {
    const groups = groupPlans([
      plan({ id: "a", state: "due", steps: one }),
      plan({ id: "b", state: "running", steps: one, run_session: "verify-b" }),
      plan({ id: "c", state: "generating" }),
      plan({ id: "d", state: "generating", live_at: 500, steps: one }),
    ]);
    expect(groups.map((g) => g.key)).toEqual(["due", "generated"]);
    expect(groups[1].plans.map((p) => p.id)).toEqual(["b", "c", "d"]);
  });

  it("files a write that stalled under Not written yet, with no detail line", () => {
    const groups = groupPlans([
      plan({
        id: "a",
        state: "generating",
        gen_started: Date.now() / 1000 - (GENERATION_STALE_S + 1),
      }),
    ]);
    expect(groups.map((g) => g.key)).toEqual(["failed"]);
    expect(groups[0].detail).toBeUndefined();
  });

  it("preserves the caller's order inside a group", () => {
    // GET /api/test-plans returns newest generated_at first; re-sorting here
    // would fight whatever the caller chose.
    const groups = groupPlans([
      plan({ id: "new", state: "due", steps: one, generated_at: 300 }),
      plan({ id: "mid", state: "due", steps: one, generated_at: 200 }),
      plan({ id: "old", state: "due", steps: one, generated_at: 100 }),
    ]);
    expect(groups[0].plans.map((p) => p.id)).toEqual(["new", "mid", "old"]);
  });

  it("shows a state this bundle has never heard of rather than dropping it", () => {
    // A newer server can write a state name this frontend predates. Filing it
    // somewhere harmless keeps it visible; silently dropping it would make a
    // plan simply not exist.
    const groups = groupPlans([
      // Double assertion because the union is exactly what is being stepped
      // outside of; the server is under no obligation to stay inside it.
      plan({ id: "a", state: "quarantined" as unknown as TestPlan["state"], steps: one }),
    ]);
    expect(groups.map((g) => g.key)).toEqual(["generated"]);
    expect(groups[0].plans.map((p) => p.id)).toEqual(["a"]);
  });
});

describe("planVerdict (the yes-or-no on every row)", () => {
  const one = [step("s1")];
  const ran = (r: Record<string, TestStepResult>, over: Partial<TestRun> = {}) => [
    run({ results: results(r), ...over }),
  ];

  it("says Verified on branch for an unmerged branch whose every step passed", () => {
    const v = planVerdict(
      plan({ state: "generated", steps: one, runs: ran({ s1: "pass" }, { prelive: true }) }),
    );
    // M7: who checked it is on the pill; the sidebar's short stays one word.
    expect(v.label).toBe("Verified on branch · by the agent");
    expect(v.short).toBe("Verified");
    expect(v.tone).toBe("ok");
  });

  it("says Verified live once it shipped and passed", () => {
    const v = planVerdict(
      plan({ state: "done", live_at: 500, steps: one, runs: ran({ s1: "pass" }) }),
    );
    expect(v.label).toBe("Verified live · by the agent");
    expect(v.short).toBe("Verified live");
  });

  it("says who passed it: you, the agent, or both", () => {
    const by = (who: Record<string, string>) =>
      planVerdict(
        plan({
          state: "done",
          live_at: 500,
          steps: [step("s1"), step("s2")],
          runs: [
            run({
              results: Object.fromEntries(
                Object.entries(who).map(([id, b]) => [id, { result: "pass", note: "", at: 1, by: b }]),
              ),
            }),
          ],
        }),
      ).label;
    expect(by({ s1: "human", s2: "human" })).toBe("Verified live · by you");
    expect(by({ s1: "agent", s2: "human" })).toBe("Verified live · by you and the agent");
  });

  it("says Failed, Agent checking, Being written, Not written yet", () => {
    expect(planVerdict(plan({ state: "done", steps: one, runs: ran({ s1: "fail" }) })).label).toBe(
      "Failed",
    );
    expect(
      planVerdict(plan({ state: "running", steps: one, run_session: "verify-a" })).label,
    ).toBe("Agent checking");
    expect(planVerdict(plan({ state: "generating" })).label).toBe("Being written");
    expect(planVerdict(plan({ state: "failed", error: "x" })).label).toBe("Not written yet");
  });

  it("says Waiting to ship while a merged branch waits out its deploy", () => {
    expect(planVerdict(plan({ state: "generated", steps: one, merged_at: 400 })).label).toBe(
      "Waiting to ship",
    );
  });

  it("says n of m checked for anything in between — can't-check is not a pass", () => {
    const v = planVerdict(
      plan({
        state: "done",
        steps: [step("s1"), step("s2", "human")],
        runs: [
          run({
            results: {
              s1: { result: "pass", note: "", at: 1, by: "agent" },
              s2: { result: "blocked", note: "staging down", at: 1, by: "human" },
            },
          }),
        ],
      }),
    );
    expect(v.label).toBe("1 of 2 checked");
    expect(v.short).toBe("1/2 checked");
    expect(v.tone).toBe("warn");
  });

  it("agrees with the group it files under", () => {
    const cases: Array<[TestPlan, string]> = [
      [plan({ state: "done", steps: one, runs: ran({ s1: "fail" }) }), "fail"],
      [plan({ state: "failed" }), "failed"],
      [plan({ state: "done", steps: one, runs: ran({ s1: "pass" }) }), "done"],
      [plan({ state: "running", steps: one, run_session: "v" }), "generated"],
    ];
    const verdictGroup: Record<string, string> = {
      failed: "fail",
      unwritten: "failed",
      branch: "done",
      live: "done",
      checking: "generated",
      writing: "generated",
    };
    for (const [p, g] of cases) {
      expect(planGroup(p)).toBe(g);
      expect(verdictGroup[planVerdict(p).key]).toBe(g);
    }
  });
});

describe("planForSession (which checklist a sidebar row's chip shows)", () => {
  const plans = [
    plan({ id: "alpha", branch: "feat/a", repo_root: "/code/app" }),
    plan({ id: "beta", branch: "feat/b", repo_root: "/code/app" }),
  ];

  it("matches by session title first", () => {
    expect(planForSession(plans, { title: "beta", branch: "x", repo: "y" })?.id).toBe("beta");
  });

  it("falls back to repo + branch — a duplicated window shares the checklist", () => {
    expect(planForSession(plans, { title: "alpha-copy", branch: "feat/a", repo: "app" })?.id).toBe(
      "alpha",
    );
  });

  it("never matches a branch name across repos", () => {
    expect(planForSession(plans, { title: "z", branch: "feat/a", repo: "other" })).toBe(null);
    expect(planForSession(plans, { title: "z", branch: "feat/a" })).toBe(null);
  });
});

describe("withdrawn answers (a phantom run)", () => {
  // record_result opens a run to have somewhere to put the first answer, so
  // answering a step and then clicking that answer OFF again — which the step
  // buttons allow on purpose — leaves a run whose every result is "". Nothing
  // was checked, but the record existed, so the plan wore a "partial" chip,
  // claimed a "last run", and sat in "Awaiting your confirmation" over work
  // that had been withdrawn.
  const withdrawn = plan({
    state: "generated",
    steps: [step("s1"), step("s2", "human")],
    runs: [run({ by: "human", session: "", results: results({ s1: "", s2: "" }) })],
  });

  it("does not count as a run", () => {
    expect(latestRun(withdrawn)).toBe(null);
  });

  it("reads as never-run rather than partial", () => {
    expect(verdictOf(withdrawn)).toBe("none");
  });

  it("is not waiting on you", () => {
    expect(needsConfirmation(withdrawn)).toBe(false);
  });

  it("does not put the plan in the badge", () => {
    expect(dueCount([withdrawn])).toBe(0);
  });

  it("comes back the moment one real answer lands", () => {
    const answered = plan({
      ...withdrawn,
      runs: [run({ by: "human", session: "", results: results({ s1: "pass", s2: "" }) })],
    });
    expect(latestRun(answered)).not.toBe(null);
    expect(verdictOf(answered)).toBe("partial");
  });

  it("still counts an agent run that has not written its answers yet", () => {
    // A run WITH a session is an agent working right now; its empty results are
    // the ones it has not got to, not answers taken back.
    const inflight = plan({
      state: "running",
      steps: [step("s1")],
      runs: [run({ session: "verify-p1", results: {} })],
    });
    expect(latestRun(inflight)).not.toBe(null);
  });
});

describe("liveBranchOverridden (is the header's one branch the whole story?)", () => {
  it("says no when every plan ships from the flock-wide branch", () => {
    expect(liveBranchOverridden("main", [plan(), plan({ id: "p2" })], [], {})).toBe(false);
  });

  it("says yes when a plan was stamped with a different branch", () => {
    // A plan outlives its repo's membership in the list, so the plans are a
    // source the settings cannot replace.
    const shipped = plan({ id: "p2", effective_live_branch: "staging" });
    expect(liveBranchOverridden("main", [shipped], [], {})).toBe(true);
  });

  it("says yes for a configured repo that has no plans yet", () => {
    // The normal state right after adding a repo: a plan costs a model call and
    // is written on push, so there is nothing in `plans` to notice. This is
    // exactly when the header would otherwise state "main" as fact six lines
    // above a card reading "staging".
    expect(
      liveBranchOverridden("main", [], ["Acme/App"], {
        "Acme/App": { live_branch: "staging" },
      }),
    ).toBe(true);
  });

  it("ignores a block whose repo is no longer tracked", () => {
    // Remove keeps the block on purpose (it is text the user wrote, and a
    // re-add hands it straight back), and an untracked block does nothing at
    // all — so it must not put a "+ per-repo" on a header describing repos that
    // have all been removed.
    expect(
      liveBranchOverridden("main", [], [], { "Acme/App": { live_branch: "staging" } }),
    ).toBe(false);
  });

  it("matches the tracked slug case-insensitively", () => {
    // GitHub slugs are case-preserving but not case-sensitive, and the backend
    // compares lowercased; a header that disagreed with the backend about which
    // repos exist would be a second, quieter bug.
    expect(
      liveBranchOverridden("main", [], ["acme/app"], {
        "Acme/App": { live_branch: "staging" },
      }),
    ).toBe(true);
  });

  it("ignores a block that only repeats the flock-wide branch", () => {
    expect(
      liveBranchOverridden("main", [], ["Acme/App"], {
        "Acme/App": { live_branch: "main" },
      }),
    ).toBe(false);
  });

  it("ignores a blank or whitespace-only override", () => {
    expect(
      liveBranchOverridden("main", [], ["Acme/App"], { "Acme/App": { live_branch: " " } }),
    ).toBe(false);
  });
});

/** A session, cut down to the three fields these rules read. The full
 * `Instance` is two dozen fields of grid/stage state, and spelling them out
 * would bury the branch — the one that decides most of what happens here. */
const session = (title: string, branch: string, repo = ""): Instance =>
  ({ title, branch, repo }) as Instance;

describe("planTargets (what the Write-plan bar may offer)", () => {
  it("offers a started session with no plan", () => {
    expect(planTargets([session("feature-a", "feature/a")], [])).toEqual(["feature-a"]);
  });

  it("skips a session that has not started, so has no branch to diff", () => {
    // `Instance.branch` is "" until the session starts, and there is nothing to
    // write a plan FROM before that.
    expect(planTargets([session("fresh", "")], [])).toEqual([]);
  });

  it("skips a session that already has a plan", () => {
    // Not an error to explain: its plan is in the list below, which is a better
    // answer than a disabled row with a tooltip.
    const targets = planTargets(
      [session("feature-a", "feature/a"), session("feature-b", "feature/b")],
      [plan({ id: "feature-a" })],
    );
    expect(targets).toEqual(["feature-b"]);
  });

  it("answers empty for a flock with no sessions at all", () => {
    // The fresh install. The bar renders nothing here, which is why the empty
    // state has to ask this same question before telling anyone to press it.
    expect(planTargets([], [])).toEqual([]);
  });

  it("skips the second window on a branch another window's plan covers", () => {
    // Two windows on one branch (`bot7` and a duplicate) are ONE thing to
    // check. The server refuses to write a second checklist for the branch, so
    // offering the copy would promise something that cannot happen.
    const targets = planTargets(
      [
        session("bot7", "feature/sc-1", "bot7-repo"),
        session("bot7-copy", "feature/sc-1", "bot7-repo"),
      ],
      [plan({ id: "bot7", branch: "feature/sc-1", repo_root: "/src/bot7-repo" })],
    );
    expect(targets).toEqual([]);
  });

  it("still offers the same branch name in a DIFFERENT repo", () => {
    // Two clones sit on different commits and can push to different remotes:
    // one branch name, two real diffs.
    const targets = planTargets(
      [session("other", "feature/sc-1", "app")],
      [plan({ id: "bot7", branch: "feature/sc-1", repo_root: "/src/bot7-repo" })],
    );
    expect(targets).toEqual(["other"]);
  });

  it("offers a session whose repo is unknown rather than guessing on the branch", () => {
    // `Instance.repo` is "" before the worktree resolves. Matching on the
    // branch alone there would hide the button for whoever happens to share a
    // branch name with a plan in another repo.
    const targets = planTargets(
      [session("starting", "feature/sc-1")],
      [plan({ id: "bot7", branch: "feature/sc-1", repo_root: "/src/bot7-repo" })],
    );
    expect(targets).toEqual(["starting"]);
  });
});


/** A results map whose entries were recorded by a PERSON. `results()` above
 * stamps `by: "agent"`, which is right for everything it is used for and is
 * exactly the wrong thing for the rules below — the whole question here is who
 * said "blocked". */
const mine = (
  answers: Record<string, TestStepResult>,
  note = "",
): Record<string, TestStepResultEntry> => {
  const map: Record<string, TestStepResultEntry> = {};
  for (const [id, result] of Object.entries(answers)) {
    map[id] = { result, note, at: 100, by: "human" };
  }
  return map;
};

describe("a person's \"Can't check\" is an answer; an agent's \"blocked\" is not", () => {
  // The rule the whole surface was missing. "Blocked" is two different
  // sentences depending on who said it: the agent handing a step over, and a
  // person reporting that they went and looked and could not get to it. Reading
  // them as one thing meant the honest answer never cleared anything, so the
  // only exits were a pass nobody had observed or deleting the plan.
  const humanStep = [step("s1", "agent"), step("s2", "human")];

  it("knows the difference on a bare result", () => {
    expect(isYourAnswer("pass")).toBe(true);
    expect(isYourAnswer("fail")).toBe(true);
    expect(isYourAnswer("blocked", "human")).toBe(true);
    expect(isYourAnswer("blocked", "agent")).toBe(false);
    expect(isYourAnswer("blocked")).toBe(false);
    expect(isYourAnswer("")).toBe(false);
  });

  it("stops asking once you have said you couldn't check it", () => {
    const answered = plan({
      state: "done",
      live_at: 500,
      steps: humanStep,
      runs: [
        run({
          results: {
            ...results({ s1: "pass" }),
            ...mine({ s2: "blocked" }, "staging was down"),
          },
        }),
      ],
    });
    expect(openHumanSteps(answered)).toEqual([]);
    expect(needsConfirmation(answered)).toBe(false);
    expect(isWaitingOnYou(answered)).toBe(false);
    expect(dueCount([answered])).toBe(0);
  });

  it("keeps asking while the AGENT is the one who said blocked", () => {
    const handed = plan({
      state: "done",
      live_at: 500,
      steps: humanStep,
      runs: [run({ results: results({ s1: "pass", s2: "blocked" }) })],
    });
    expect(openHumanSteps(handed).map((st) => st.id)).toEqual(["s2"]);
    expect(isWaitingOnYou(handed)).toBe(true);
  });

  it("still refuses to call it a pass", () => {
    // The guard that keeps this from being a way to make a broken thing green.
    const answered = plan({
      state: "done",
      steps: humanStep,
      runs: [run({ results: { ...results({ s1: "pass" }), ...mine({ s2: "blocked" }) } })],
    });
    expect(verdictOf(answered)).toBe("partial");
    expect(unansweredCount(answered)).toBe(1);
    expect(cantCheckCount(answered)).toBe(1);
  });

  it("counts nothing before anything has run", () => {
    expect(cantCheckCount(plan({ steps: humanStep }))).toBe(0);
  });

  it("says both numbers rather than averaging them", () => {
    const status = planStatus(
      plan({
        state: "done",
        live_at: 500,
        steps: [step("s1"), step("s2"), step("s3", "human")],
        runs: [
          run({
            results: {
              ...results({ s1: "pass", s2: "pass" }),
              ...mine({ s3: "blocked" }),
            },
          }),
        ],
      }),
    );
    expect(status.line).toBe("2 passed · Couldn't check 1 step — not verified.");
    // Never `ok`: a can't-check is a known unknown, and the tone is the loudest
    // claim on the row.
    expect(status.tone).toBe("warn");
  });

  it("does not claim anything passed when nothing did", () => {
    const status = planStatus(
      plan({
        state: "done",
        live_at: 500,
        steps: [step("s1", "human")],
        runs: [run({ results: mine({ s1: "blocked" }) })],
      }),
    );
    expect(status.line).toBe("Couldn't check 1 step — not verified.");
    expect(status.tone).toBe("warn");
    // Every step has an answer, so it does not ask "Answer 1 step".
    expect(status.actionLabel).toBe("Show steps");
    expect(status.group).toBe("partial");
  });

  it("files both kinds of gap under Not verified, and never under Needs you", () => {
    // A person's "Can't check" is an answer: it must not pin the row on the
    // badge forever — and nothing about it is verified either.
    const yours = plan({
      id: "yours",
      state: "done",
      steps: [step("s1", "human")],
      runs: [run({ results: mine({ s1: "blocked" }) })],
    });
    const stopped = plan({
      id: "stopped",
      state: "done",
      steps: [step("s1"), step("s2")],
      runs: [run({ results: results({ s1: "pass" }) })],
    });
    expect(groupPlans([yours]).map((g) => g.key)).toEqual(["partial"]);
    expect(groupPlans([stopped]).map((g) => g.key)).toEqual(["partial"]);
    expect(dueCount([yours, stopped])).toBe(0);
  });
});

describe("the row says what a run will and won't cover", () => {
  it("offers Answer, not Run, when every step is yours", () => {
    // Running one provisions a workspace and a billed session for an agent that
    // is forbidden to settle a single step — and `parse_plan` defaults an
    // unrecognised actor to "human", so this is a plan a model produces by
    // omission rather than an exotic case.
    const status = planStatus(
      plan({
        state: "due",
        live_at: 500,
        steps: [step("s1", "human"), step("s2", "human")],
      }),
    );
    expect(status.line).toBe("Shipped — all 2 steps are yours to check.");
    expect(status.action).toBe("answer");
    expect(status.actionLabel).toBe("Answer 2 steps");
  });

  it("counts the steps the run handed back into the button", () => {
    // THE REAL SHAPE OF A BAD AFTERNOON, and the bug this rule was written for.
    // A plan of 8 (2 written as yours, 6 as the agent's) whose run could not
    // observe the change at all — not deployed where it could look — comes back
    // with all 8 blocked. Every mark in the body said "yours" and the tally
    // said 8; the sentence and the button said 2, because they asked whose step
    // it was WRITTEN to be instead of who owes it now.
    const steps = [
      step("s1", "human"),
      step("s2"),
      step("s3"),
      step("s4"),
      step("s5"),
      step("s6"),
      step("s7"),
      step("s8", "human"),
    ];
    const handedBackAll = plan({
      state: "due",
      live_at: 500,
      steps,
      runs: [
        run({
          results: results(
            Object.fromEntries(steps.map((st) => [st.id, "blocked" as const])),
          ),
        }),
      ],
    });
    const status = planStatus(handedBackAll);
    expect(status.line).toBe("8 steps need your eyes — an agent can't judge them.");
    expect(status.actionLabel).toBe("Answer 8 steps");
    // ...and it agrees with the tally beside it, which is the whole point.
    expect(tallyBits(handedBackAll).find((b) => b.state === "yours")?.count).toBe(8);
  });

  it("says the split when an agent can do some of it", () => {
    const status = planStatus(
      plan({
        state: "due",
        live_at: 500,
        steps: [step("s1"), step("s2"), step("s3", "human")],
      }),
    );
    expect(status.line).toBe(
      "Shipped — nobody has checked it yet. An agent can check 2 of 3; the rest are yours.",
    );
    expect(status.action).toBe("run");
  });

  it("points a failure at the evidence rather than at another run", () => {
    // Re-running was also destructive: the new run becomes `latestRun` the
    // moment it starts, so one press replaced the failure being read with an
    // empty in-flight run.
    const status = planStatus(
      plan({
        state: "done",
        live_at: 500,
        steps: [step("s1")],
        runs: [run({ results: results({ s1: "fail" }) })],
      }),
    );
    expect(status.action).toBe("answer");
    expect(status.actionLabel).toBe("See what failed");
    expect(status.tone).toBe("bad");
  });

  it("counts your steps while an agent is running, without stealing the Watch button", () => {
    // `latestRun` returns the IN-FLIGHT run, so every human step is already
    // "open" from the instant the run starts. Promoting Answer here would delete
    // the busy state — and the only route to the running session — entirely.
    const running = plan({
      state: "running",
      live_at: 500,
      run_session: "verify-p1",
      steps: [step("s1"), step("s2", "human"), step("s3", "human")],
      runs: [run({ session: "verify-p1", results: {} })],
    });
    const status = planStatus(running);
    expect(status.line).toBe("An agent is checking the rest — 2 steps need your eyes.");
    expect(status.action).toBe("watch");

    // ...and the sentence moves as you answer, which is what was missing.
    const one = planStatus({
      ...running,
      runs: [run({ session: "verify-p1", results: mine({ s2: "pass" }) })],
    });
    expect(one.line).toBe("An agent is checking the rest — 1 step needs your eyes.");
  });

  it("falls back to the plain sentence when none of the steps are yours", () => {
    const status = planStatus(
      plan({
        state: "running",
        run_session: "verify-p1",
        steps: [step("s1")],
        runs: [run({ session: "verify-p1", results: {} })],
      }),
    );
    expect(status.line).toBe("An agent is checking the steps it can.");
  });
});

describe("noTargetsReason", () => {
  // One sentence used to cover three unlike situations, and it was simply false
  // for the commonest: telling somebody with five open sessions to start one.
  const inst = (title: string, branch: string): Instance =>
    ({ title, branch }) as Instance;

  it("says nothing at all while there is something to offer", () => {
    expect(noTargetsReason([inst("a", "feature/a")], [])).toBe("");
  });

  it("tells a user with no sessions to start one", () => {
    expect(noTargetsReason([], [])).toBe(
      "Start a session first — a checklist is written from a branch's diff.",
    );
  });

  it("tells a user whose sessions have no branch yet what is missing", () => {
    expect(noTargetsReason([inst("a", "")], [])).toBe(
      "No session has a branch yet — a checklist is written from a branch's diff.",
    );
  });

  it("points at the list when everything already has one", () => {
    expect(noTargetsReason([inst("a", "feature/a")], [plan({ id: "a" })])).toBe(
      "Every session's branch already has a checklist.",
    );
  });
});


describe("the gaps are named apart, and a run nobody ran is not \"a run\"", () => {
  it("does not call the steps you couldn't check unanswered", () => {
    // The row used to sum the two kinds of gap, which told somebody who had
    // answered every step of theirs that those steps "never got an answer" —
    // directly under a heading that had just counted them correctly.
    const status = planStatus(
      plan({
        state: "done",
        live_at: 500,
        steps: [step("s1"), step("s2"), step("s3", "human")],
        runs: [
          run({
            results: { ...results({ s1: "pass" }), ...mine({ s3: "blocked" }) },
          }),
        ],
      }),
    );
    expect(status.line).toBe("Not verified — 1 step with no answer · 1 you couldn't check.");
  });

  it("does not claim a run stopped early when no agent ever started one", () => {
    // record_result opens a session-less run for the first hand-recorded answer,
    // so this is an ordinary state — you answered your own steps and have not
    // pressed Run yet.
    const status = planStatus(
      plan({
        state: "due",
        live_at: 500,
        steps: [step("s1"), step("s2", "human")],
        runs: [run({ session: "", by: "human", results: mine({ s2: "pass" }) })],
      }),
    );
    expect(status.line).toBe("Part-answered — 1 step with no answer.");
    // ...and it offers to run it, not to run it AGAIN.
    expect(status.actionLabel).toBe("Check now");
  });

  it("never offers a run on a checklist the run route would refuse", () => {
    // Every step is a person's own, so there is nothing an agent may settle and
    // POST /run 409s. The way back in is the list itself.
    const allYours = plan({
      state: "done",
      live_at: 500,
      steps: [step("s1", "human"), step("s2", "human")],
      runs: [run({ results: mine({ s1: "pass", s2: "pass" }) })],
    });
    // Still never a run — that is what this test is about. The label is now
    // "See the answers" because everything here IS answered: offering to "check
    // again" a checklist with nothing left to check was the row saying the job
    // was done while its button invited the job again.
    expect(planStatus(allYours).actionLabel).toBe("See the answers");
    expect(planStatus(allYours).action).toBe("answer");
  });

  it("does not invent a share for you when an agent can do the lot", () => {
    expect(
      planStatus(plan({ state: "due", live_at: 500, steps: [step("s1"), step("s2")] })).line,
    ).toBe("Shipped — nobody has checked it yet. An agent can check all 2 steps.");
  });

  it("reads naturally for a one-step checklist that is all yours", () => {
    expect(
      planStatus(plan({ state: "due", live_at: 500, steps: [step("s1", "human")] })).line,
    ).toBe("Shipped — its one step is yours to check.");
  });
});

describe("a running plan with no session left to watch", () => {
  it("stops offering Watch, which could not do anything", () => {
    // `prune` clears `run_session` when a verify session dies, and the state can
    // lag a tick behind. The row went on offering Watch — and `watch()` returns
    // silently without a title, while a disabled `.btn-primary` is painted
    // exactly like a live one. So the press produced no pane, no toast and no
    // change: the surface's one genuinely dead control.
    const orphaned = plan({
      state: "running",
      live_at: 500,
      run_session: "",
      steps: [step("s1"), step("s2", "human")],
      runs: [run({ session: "", results: {} })],
    });
    const status = planStatus(orphaned);
    expect(status.action).not.toBe("watch");
    expect(status.line).toBe(
      "The agent that was checking this is gone — nothing is running now.",
    );
    // ...and it offers to start one, not to start one AGAIN.
    expect(status.actionLabel).toBe("Check now");
  });

  it("still offers Watch while the session is actually there", () => {
    const live = plan({
      state: "running",
      live_at: 500,
      run_session: "verify-p1",
      steps: [step("s1")],
      runs: [run({ session: "verify-p1", results: {} })],
    });
    expect(planStatus(live).action).toBe("watch");
  });
});


/* ---------------------------------------------------------------------------
 * The checks view — the tally, the per-step mark, and the fact that they are
 * two renderings of ONE state rather than two opinions about it.
 * ------------------------------------------------------------------------ */
describe("checkTally / stepCheck", () => {
  const shipped = (over: Partial<TestPlan> = {}) =>
    plan({
      state: "due",
      live_at: 500,
      steps: [step("s1"), step("s2"), step("s3", "human"), step("s4", "human")],
      ...over,
    });

  it("counts every state apart, and never folds a can't-check into a pass", () => {
    const p = shipped({
      runs: [
        run({
          results: {
            s1: { result: "pass", note: "", at: 1, by: "agent" },
            s2: { result: "fail", note: "", at: 1, by: "agent" },
            s3: { result: "blocked", note: "", at: 1, by: "human" },
            s4: { result: "blocked", note: "", at: 1, by: "agent" },
          },
        }),
      ],
    });
    const t = checkTally(p);
    expect(t.total).toBe(4);
    expect(t.passed).toBe(1);
    expect(t.failed).toBe(1);
    // Yours, because the AGENT's blocked means "not mine to answer" — the step
    // is still open, and it is open for you.
    expect(t.yours).toBe(1);
    // Yours only in the sense that you already answered it. A known unknown,
    // and the one number that must never be added to `passed`.
    expect(t.cant).toBe(1);
    expect(t.pending).toBe(0);
  });

  it("counts a shipped checklist nobody has touched as waiting, not pending", () => {
    const t = checkTally(shipped());
    // The two human steps are asking; the agent's two are simply unrun.
    expect(t.yours).toBe(2);
    expect(t.pending).toBe(2);
  });

  it("the mark on a row is the state the tally counted", () => {
    const p = shipped({
      runs: [run({ results: { s1: { result: "pass", note: "", at: 1, by: "agent" } } })],
    });
    expect(stepCheck(p, "s1")).toBe("pass");
    expect(stepCheck(p, "s2")).toBe("pending");
    expect(stepCheck(p, "s3")).toBe("yours");
    expect(CHECK_MARK[stepCheck(p, "s1")]).toBe("✓");
  });

  it("keeps asking while a shipped checklist is being rewritten", () => {
    // A rewrite puts a LIVE plan in `generating`. `isWaitingOnYou` keeps it in
    // the badge and under "Not checked yet" for those three minutes, so a tally
    // that said "not checked yet" contradicted the heading directly above it —
    // over a press that changed nothing about who owes what.
    const rewriting = shipped({ state: "generating", live_at: 500 });
    expect(stepCheck(rewriting, "s3")).toBe("yours");
    expect(checkTally(rewriting).yours).toBe(2);
    expect(planShipped(rewriting)).toBe(true);
  });

  it("does not ask a person for anything until the work has shipped", () => {
    // The whole premise: a checklist for a branch nobody has merged is not
    // waiting on you, and a row of accent-coloured "needs you" marks on one
    // would be the surface crying wolf about every open branch in the flock.
    const notYet = shipped({ state: "generated", live_at: 0 });
    expect(stepCheck(notYet, "s3")).toBe("pending");
    expect(checkTally(notYet).yours).toBe(0);
  });
});

describe("finding one checklist among many", () => {
  const tokens = (q: string) => q.toLowerCase().split(/\s+/).filter(Boolean);
  const p = plan({
    id: "sc-21255",
    title: "sc-21255",
    summary: "Parked pages stop alerting",
    branch: "feature/sc-21255/classifier-collage",
    sha: "78fd955aaa",
    repo_root: "/home/me/workspaces/_base_sitecheck-bot",
    state: "due",
    live_at: 500,
    steps: [step("s1"), { ...step("s2", "human"), text: "Open the Grafana board 06" }],
  });

  it("takes every token, in any order, across any field", () => {
    // The way people actually type: bits of the ticket, the repo and the thing
    // they remember the checklist being about.
    expect(planMatches(p, tokens("21255"))).toBe(true);
    expect(planMatches(p, tokens("sitecheck collage"))).toBe(true);
    expect(planMatches(p, tokens("parked alerting"))).toBe(true);
    expect(planMatches(p, tokens("78fd955"))).toBe(true);
    expect(planMatches(p, tokens("sitecheck 99999"))).toBe(false);
  });

  it("searches what the STEPS say, which is how a checklist is remembered", () => {
    // Long after the ticket number stops meaning anything, "the one about the
    // Grafana board" is the only handle a person has — and those words appear
    // nowhere else on the row.
    expect(planMatches(p, tokens("grafana"))).toBe(true);
    expect(planMatches(p, tokens("grafana board 06"))).toBe(true);
  });

  it("narrows to a heading a reader can already see", () => {
    expect(planMatches(p, tokens("needs you"))).toBe(true);
    expect(planMatches(p, tokens("couldn't be written"))).toBe(false);
  });

  it("matches everything when nothing is typed", () => {
    expect(planMatches(p, [])).toBe(true);
    expect(planMatches(plan({ steps: [] }), [])).toBe(true);
  });
});

describe("what a bulk Run may actually start", () => {
  it("takes the ones a run would take, and no others", () => {
    const shipped = (over: Partial<TestPlan> = {}) =>
      plan({ state: "due", live_at: 500, steps: [step("s1"), step("s2", "human")], ...over });

    expect(canRunNow(shipped())).toBe(true);
    // An agent is already working it — the route 409s a second run.
    expect(canRunNow(shipped({ state: "running", run_session: "verify-x" }))).toBe(false);
    // Still being written.
    expect(canRunNow(plan({ state: "generating", steps: [] }))).toBe(false);
    // Every step is a person's own: an agent may not settle one, so the session
    // would provision a workspace and hand the whole list straight back.
    expect(canRunNow(shipped({ steps: [step("s1", "human"), step("s2", "human")] }))).toBe(
      false,
    );
    // Not merged yet: Check now checks the branch, so it can be run too.
    expect(canRunNow(plan({ state: "generated", steps: [step("s1")] }))).toBe(true);
  });
});

describe("how long a run has been going", () => {
  const now = 1_000_000;

  it("tells a three-second-old run apart from a wedged one", () => {
    const running = (at: number) =>
      planStatus(
        plan({
          state: "running",
          live_at: 500,
          steps: [step("s1")],
          run_session: "verify-sc-1",
          runs: [run({ at, session: "verify-sc-1" })],
        }),
      ).line;
    // The one sentence used to stand for everything from "you just pressed it"
    // to "this is ten minutes from being given up on", which is the difference
    // between waiting and going to look.
    expect(runElapsed(run({ at: now - 20 }), now)).toBe("");
    expect(runElapsed(run({ at: now - 4 * 60 }), now)).toBe(" (4m so far)");
    expect(runElapsed(run({ at: now - 100 * 60 }), now)).toBe(" (2h so far)");
    expect(running(Date.now() / 1000 - 300)).toContain("(5m so far)");
  });

  it("says nothing rather than something absurd", () => {
    // A missing or nonsensical stamp is far likelier than a two-month run, and
    // "(20690d so far)" makes the row look broken rather than informative.
    expect(runElapsed(null, now)).toBe("");
    expect(runElapsed(run({ at: 0 }), now)).toBe("");
    expect(runElapsed(run({ at: 1 }), now)).toBe("");
    expect(runElapsed(run({ at: now + 500 }), now)).toBe("");
  });
});

describe("the note composer after an answer", () => {
  it("never re-attaches a sentence you discarded to the next answer", () => {
    // The old path cleared the field and then re-saved the OLD text from a
    // stale closure, so a discarded note came back on the next paint and was
    // posted, by blur, as the reason for the answer after it. A note here is
    // evidence; putting one against the wrong answer is the worst thing this
    // surface can do quietly.
    expect(noteDraftAfter("fail", false, "left over from last time")).toEqual({
      open: true,
      text: "",
    });
  });

  it("sheds the sentence when the answer is taken back", () => {
    expect(noteDraftAfter("", true, "because the modal never opened")).toEqual({
      open: false,
      text: "",
    });
  });

  it("stops holding a note that already rode along with the answer", () => {
    expect(noteDraftAfter("fail", true, "the modal never opened")).toEqual({
      open: false,
      text: "",
    });
  });

  it("leaves an empty open composer exactly where it is", () => {
    expect(noteDraftAfter("pass", true, "   ")).toEqual({ open: true, text: "   " });
    expect(noteDraftAfter("pass", false, "")).toEqual({ open: false, text: "" });
  });

  it("will not let a held key fire one POST per repeat", () => {
    expect(stepKeyAllowed(false, false)).toBe(true);
    expect(stepKeyAllowed(false, true)).toBe(false); // key-repeat
    expect(stepKeyAllowed(true, false)).toBe(false); // a POST already in flight
  });
});

describe("what a collapsed group heading adds up to", () => {
  it("says how much is being asked of you, not just how many plans", () => {
    const shipped = (id: string, over: Partial<TestPlan> = {}) =>
      plan({
        id,
        title: id,
        state: "due",
        live_at: 500,
        steps: [step("s1"), step("s2", "human")],
        ...over,
      });
    const groups = groupPlans([shipped("a"), shipped("b")]);
    const due = groups.find((g) => g.key === "due");
    expect(due?.plans).toHaveLength(2);
    // Two plans, one human step each — the number that matters is 2 steps, not
    // 2 checklists.
    expect(due?.detail).toBe("2 steps need you");
  });

  it("leads with the red ones where a group has both", () => {
    // A failure AND a step still open for you: `planGroup` files this under
    // Failed, and the heading has to carry both numbers.
    const mixed = plan({
      id: "a",
      title: "a",
      state: "due",
      live_at: 500,
      steps: [step("s1"), step("s2", "human")],
      runs: [run({ results: { s1: { result: "fail", note: "", at: 1, by: "agent" } } })],
    });
    expect(groupPlans([mixed]).find((g) => g.key === "fail")?.detail).toBe(
      "1 step failed · 1 step needs you",
    );
  });

  it("counts the steps in a plan that is entirely red", () => {
    const red = plan({
      id: "a",
      title: "a",
      state: "done",
      live_at: 500,
      steps: [step("s1"), step("s2")],
      runs: [
        run({
          results: {
            s1: { result: "fail", note: "", at: 1, by: "agent" },
            s2: { result: "pass", note: "", at: 1, by: "agent" },
          },
        }),
      ],
    });
    expect(groupPlans([red]).find((g) => g.key === "fail")?.detail).toBe("1 step failed");
  });

  it("says nothing when there is nothing outstanding to say", () => {
    const clean = plan({
      state: "generated",
      steps: [step("s1")],
    });
    expect(groupPlans([clean]).find((g) => g.key === "generated")?.detail).toBeUndefined();
  });
});

describe("a run that died says so on the row", () => {
  it("keeps whose turn it is first, then names the failure", () => {
    // `fail_run` puts the plan back to `due`, so without this the row reverts to
    // "nobody has checked it yet" and the failure lives only inside the plan.
    const p = plan({
      state: "due",
      live_at: 500,
      steps: [step("s1")],
      error:
        "The verify session couldn't start. failed to start new session: tmux " +
        "session already exists: mindflock_verify-sc-1-a804a3d",
    });
    const line = planStatus(p).line;
    expect(line.startsWith("Shipped")).toBe(true);
    expect(line).toContain("The verify session couldn't start.");
    // The raw tmux line stays in the body, where there is room for it.
    expect(line).not.toContain("mindflock_verify");
  });

  it("says nothing twice when the sentence is already about the failure", () => {
    const failed = plan({ state: "failed", steps: [], error: "boom. detail" });
    // The reason IS the sentence now (M1) — said once, not appended again.
    expect(planStatus(failed).line).toBe("Not written yet — boom.");
    // A rewrite in flight owns the row; the stored error is the attempt it is
    // replacing.
    const rewriting = plan({
      state: "generating",
      gen_started: Date.now() / 1000,
      steps: [step("s1")],
      error: "Rewriting the checklist failed. timed out",
    });
    expect(planStatus(rewriting).line).not.toContain("Rewriting the checklist failed");
  });

  it("lifts one sentence, and never half of one", () => {
    expect(errorHeadline(plan({ error: "" }))).toBe("");
    expect(errorHeadline(plan({ error: "It broke. because of a thing" }))).toBe("It broke.");
    // No sentence boundary: short enough to BE one, so it is shown whole —
    // sentence-cased and terminated, because half the writers store a lowercase
    // clause and it is appended to a sentence that has just ended.
    expect(errorHeadline(plan({ error: "fatal: could not read Username" }))).toBe(
      "Fatal: could not read Username.",
    );
    // ...and a wall of git output is CLAMPED at a word boundary rather than
    // dropped: dropping it is what made the two commonest run failures — whose
    // first sentences are long — say nothing at all on the row.
    const wall = errorHeadline(
      plan({ error: "fatal: " + "detail ".repeat(40) + "and the remedy" }),
    );
    expect(wall.length).toBeLessThanOrEqual(HEADLINE_MAX + 1);
    expect(wall.endsWith("…")).toBe(true);
    expect(wall).not.toContain("detai…"); // never mid-word
  });
});

describe("the tally on the collapsed row", () => {
  const shipped = (over: Partial<TestPlan> = {}) =>
    plan({
      state: "due",
      live_at: 500,
      steps: [step("s1"), step("s2"), step("s3", "human"), step("s4", "human")],
      ...over,
    });

  it("says where the work has got to before anyone opens the plan", () => {
    const bits = tallyBits(
      shipped({
        runs: [
          run({
            results: {
              s1: { result: "pass", note: "", at: 1, by: "agent" },
              s2: { result: "fail", note: "", at: 1, by: "agent" },
            },
          }),
        ],
      }),
    );
    expect(bits.map((b) => [b.state, b.count])).toEqual([
      ["pass", 1],
      ["fail", 1],
      ["yours", 2],
    ]);
  });

  it("drops the zeroes rather than printing '0 failed' on a clean row", () => {
    const clean = shipped({
      steps: [step("s1")],
      runs: [run({ results: { s1: { result: "pass", note: "", at: 1, by: "agent" } } })],
    });
    expect(tallyBits(clean)).toHaveLength(1);
    expect(tallyBits(clean)[0].state).toBe("pass");
  });

  it("renders nothing at all for a plan that has no steps yet", () => {
    // A plan still being written: the row is already saying so, and a tally of
    // nothing is furniture.
    expect(tallyBits(plan({ state: "generating", steps: [] }))).toEqual([]);
    expect(tallySentence(plan({ state: "generating", steps: [] }))).toBe("");
  });

  it("counts exactly what the roll-up inside the plan counts", () => {
    // The two are one sentence said twice; if they can disagree, one of them is
    // lying to somebody triaging a list.
    const p = shipped({
      runs: [
        run({
          results: {
            s1: { result: "pass", note: "", at: 1, by: "agent" },
            s3: { result: "blocked", note: "", at: 1, by: "human" },
          },
        }),
      ],
    });
    const t = checkTally(p);
    const summed = tallyBits(p).reduce((n, b) => n + b.count, 0);
    expect(summed).toBe(t.total);
    const byState = Object.fromEntries(tallyBits(p).map((b) => [b.state, b.count]));
    expect(byState.pass).toBe(t.passed);
    expect(byState.cant).toBe(t.cant);
    expect(byState.yours).toBe(t.yours);
  });

  it("announces the glyphs as words, since a screen reader gets no colour", () => {
    const p = shipped({
      runs: [
        run({
          results: {
            s1: { result: "pass", note: "", at: 1, by: "agent" },
            s2: { result: "fail", note: "", at: 1, by: "agent" },
          },
        }),
      ],
    });
    expect(tallySentence(p)).toBe("4 steps: 1 passed, 1 failed, 2 need you");
    expect(tallySentence(plan({ steps: [step("s1")] }))).toBe("1 step: 1 not checked yet");
  });
});

describe("the rewrite box", () => {
  it("warns only when there is something to lose", () => {
    expect(rewriteWarning(plan())).toBe("");
    expect(rewriteWarning(plan({ runs: [run()] }))).toContain("Answers on steps that change");
  });

  it("refuses to rewrite out from under a running agent", () => {
    // The server 409s this; the button must not offer it. Rewriting mid-run
    // orphans a real, billed session — the poller only looks at plans in
    // `running`, so its result file is never read and its give-up clock never
    // starts.
    expect(rewriteBlockedReason(plan({ state: "running" }))).toContain("cancel the run");
    expect(rewriteBlockedReason(plan({ state: "generating" }))).toBeTruthy();
    expect(rewriteBlockedReason(plan({ state: "due" }))).toBe("");
  });
});

describe("answering from the keyboard", () => {
  it("maps both spellings of each answer", () => {
    expect(stepKeyAction("1")).toBe("pass");
    expect(stepKeyAction("p")).toBe("pass");
    expect(stepKeyAction("2")).toBe("fail");
    expect(stepKeyAction("f")).toBe("fail");
    expect(stepKeyAction("3")).toBe("blocked");
    expect(stepKeyAction("b")).toBe("blocked");
    expect(stepKeyAction("n")).toBe("note");
  });

  it("leaves every other key alone", () => {
    // What stops these eating a keystroke meant for the note box beside them —
    // the row handler returns early on "" rather than preventing the default.
    for (const key of ["a", "Enter", "Tab", "Escape", "ArrowDown", " ", "4"])
      expect(stepKeyAction(key)).toBe("");
  });

  it("tells undo apart from a key it does not handle", () => {
    // Both are "" in the map above — "no answer" and "not mine" are the same
    // value there — so the caller needs a second question to distinguish them.
    expect(stepKeyIsUndo("u")).toBe(true);
    expect(stepKeyIsUndo("U")).toBe(true);
    expect(stepKeyIsUndo("a")).toBe(false);
  });
});

/* ---------------------------------------------------------------------------
 * Writing a checklist for a session you already closed.
 *
 * Every other part of this feature is built on a checklist outliving its
 * session. Creation was the one half that still demanded a live window — so
 * "write me a checklist for that" stopped being possible at exactly the moment
 * people reach for it, which is after the work is done and the window is gone.
 * ------------------------------------------------------------------------ */
describe("planTargets with closed sessions", () => {
  const open = (title: string, branch = "feature/" + title) =>
    ({ title, branch }) as unknown as Instance;

  it("offers a closed session that has a branch", () => {
    expect(planTargets([], [], [{ title: "sc-9", branch: "feature/sc-9" }])).toEqual([
      "sc-9",
    ]);
  });

  it("puts the sessions on screen first", () => {
    const got = planTargets(
      [open("live-1")],
      [],
      [{ title: "closed-1", branch: "feature/closed-1" }],
    );
    expect(got).toEqual(["live-1", "closed-1"]);
  });

  it("names a session once, however many lists it is in", () => {
    // A session reopened under the same name is in both. The thing being named
    // is the session, not the window.
    const got = planTargets([open("both")], [], [{ title: "both", branch: "x" }]);
    expect(got).toEqual(["both"]);
  });

  it("still skips anything that already has a checklist, or has no branch", () => {
    const plans = [plan({ id: "done-already" })];
    const got = planTargets(
      [open("done-already")],
      plans,
      [
        { title: "done-already", branch: "b" },
        { title: "no-branch" },
        { title: "fresh", branch: "feature/fresh" },
      ],
    );
    expect(got).toEqual(["fresh"]);
  });

  it("labels which ones are closed, so the picker can say so", () => {
    const marks = closedTargets(
      [open("live-1")],
      [{ title: "live-1", branch: "b" }, { title: "gone", branch: "b" }],
    );
    // "live-1" is in the closed store AND on screen — it was reopened, so it is
    // not a closed target.
    expect(marks.has("live-1")).toBe(false);
    expect(marks.has("gone")).toBe(true);
  });

  it("stops telling you to start a session when closed ones are available", () => {
    expect(noTargetsReason([], [], [{ title: "sc-9", branch: "b" }])).toBe("");
    expect(noTargetsReason([], [], [{ title: "sc-9" }])).toContain("has a branch yet");
    expect(noTargetsReason([], [], [])).toContain("Start a session first");
  });
});

/* ---------------------------------------------------------------------------
 * A wait that cannot end has to say so.
 *
 * `git fetch origin <branch>` only writes `refs/remotes/origin/<branch>` when
 * the remote's refspec covers it — MindFlock's own provisioned base clones are
 * cloned narrow, so `origin/main` never existed locally and the ancestry test
 * failed forever. One real checklist had been merged and released for days while
 * its row said "it turns up here to check when it ships".
 * ------------------------------------------------------------------------ */
describe("a plan waiting on a branch that isn't there", () => {
  it("says why, instead of promising it will turn up", () => {
    const stuck = plan({
      state: "generated",
      live_at: 0,
      branch: "feature/x",
      live_branch: "main",
      live_problem: "origin has no branch called main, so this can never come due.",
      steps: [step("s1")],
    });
    const status = planStatus(stuck);
    expect(status.line).toContain("no branch called main");
    // The WAIT is fixed on the repo's card; checking the branch before merge
    // still works, so Check now stays.
    expect(status.action).toBe("early");
    expect(status.tone).toBe("warn");
  });

  it("goes back to the ordinary waiting sentence once the branch shows up", () => {
    const waiting = plan({
      state: "generated",
      live_at: 0,
      branch: "feature/x",
      live_branch: "main",
      live_problem: "",
      steps: [step("s1")],
    });
    expect(planStatus(waiting).line).toContain("comes back here after it reaches main");
  });

  it("does not shout about it once the work has actually shipped", () => {
    // A stale diagnosis must never outrank the real state. The server clears it,
    // but the row must not depend on that having happened yet.
    const shipped = plan({
      state: "due",
      live_at: 500,
      live_problem: "origin has no branch called main, so this can never come due.",
      steps: [step("s1")],
    });
    expect(planStatus(shipped).line).not.toContain("can never come due");
  });
});


describe("a check before merge, and the check after it", () => {
  const before = (answers: Record<string, TestStepResult>) =>
    run({ results: results(answers), prelive: true } as Partial<TestRun>);

  it("is not the current answer once the work has shipped", () => {
    const shipped = plan({
      state: "due",
      live_at: 500,
      steps: [step("s1")],
      runs: [before({ s1: "pass" })],
    });
    expect(latestRun(shipped)).toBeNull();
    expect(verdictOf(shipped)).toBe("none");
    const status = planStatus(shipped);
    expect(status.action).toBe("run");
    expect(status.line).toContain("nobody has checked it yet");
  });

  it("is still the answer while the work is only on its branch", () => {
    const early = plan({
      state: "generated",
      live_at: 0,
      steps: [step("s1")],
      runs: [before({ s1: "fail" })],
    });
    expect(latestRun(early)).not.toBeNull();
    expect(planGroup(early)).toBe("fail");
  });

  it("is named, with what it found, after merge", () => {
    const shipped = plan({
      state: "due",
      live_at: 500,
      steps: [step("s1"), step("s2")],
      runs: [before({ s1: "pass", s2: "fail" })],
    });
    const earlier = beforeMergeRun(shipped);
    expect(earlier).not.toBeNull();
    expect(beforeMergeSummary(shipped, earlier!)).toBe("1 step failed");
    expect(isBeforeMerge(earlier)).toBe(true);
    expect(beforeMergeRun(plan({ state: "generated", live_at: 0, runs: [before({})] }))).toBeNull();
  });

  it("a run without the flag is an ordinary run", () => {
    expect(isBeforeMerge(run())).toBe(false);
  });
});

describe("checklistTargets (Ask the agent for a checklist)", () => {
  const inst = (title: string, branch: string): Instance =>
    ({ title, branch, repo: "", folder: "", folder_label: "" }) as unknown as Instance;
  it("lists closed sessions, and says why one can't be picked instead of hiding it", () => {
    const targets = checklistTargets(
      [inst("open-a", "feature/a"), inst("on-main", "main")],
      [plan({ id: "closed-b", branch: "feature/b" })],
      [
        { title: "closed-b", branch: "feature/b" },
        { title: "closed-c", branch: "feature/c" },
      ],
      "main",
    );
    expect(targets.map((t) => [t.title, t.closed, t.reason])).toEqual([
      ["open-a", false, ""],
      ["closed-c", true, ""],
      ["on-main", false, "on main — nothing to compare"],
      ["closed-b", true, "has a checklist"],
    ]);
    expect(targets.find((t) => t.title === "closed-b")?.planId).toBe("closed-b");
  });

  it("skips sessions with no branch", () => {
    expect(checklistTargets([inst("x", "")], [], [])).toEqual([]);
  });
});

describe("the Verify deep link (openDialogFor(\"verify\", <branch>))", () => {
  it("finds a checklist by its branch, or by its session title", () => {
    const a = plan({ id: "sess-a", branch: "feature/a" });
    const b = plan({ id: "sess-b", branch: "feature/b" });
    expect(verifyDeepLinkPlan([a, b], "feature/b")?.id).toBe("sess-b");
    expect(verifyDeepLinkPlan([a, b], "sess-a")?.id).toBe("sess-a");
    expect(verifyDeepLinkPlan([a, b], "feature/zzz")).toBeNull();
    expect(verifyDeepLinkPlan([a, b], "")).toBeNull();
  });
});
