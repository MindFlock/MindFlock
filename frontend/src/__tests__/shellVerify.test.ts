/** Round 4, Verify leftovers on the shell: the bell's three checklist rows,
 * the top bar's Verify count (its tooltip and a red badge for failures), and
 * a checklist made empty on purpose reading as "add a step", never as one
 * that couldn't be written. */

import { describe, expect, it } from "vitest";
import { code, css } from "./sourceText";
import { notifFromEvent } from "../components/NotificationsBell";
import {
  isEmptyByChoice,
  isFailing,
  isWaitingOnYou,
  planStatus,
  planVerdict,
} from "../components/dialogs/verify";
import type { EventEnvelope } from "../state/queries";
import type { TestPlan } from "../api/types";

const env = (e: Partial<EventEnvelope>): EventEnvelope => ({
  event: "",
  session: "shop@feature/x",
  seq: 1,
  ts: 0,
  old: null,
  new: null,
  data: {},
  ...e,
});

describe("the bell's behaviors rows", () => {
  it("behaviors came due: its rule, its title, a click into the Sky", () => {
    const n = notifFromEvent(env({ event: "session.test_plan_due", data: { plan: "p1", title: "Fix login" } }))!;
    expect(n).toMatchObject({ rule: "verify_plan_due", verify: "p1", name: "Fix login", text: "ready to check" });
    expect(n.dedupe).toBe("plan-due:p1");
  });

  it("behaviors couldn't be written", () => {
    expect(notifFromEvent(env({ event: "session.test_plan_failed", data: { plan: "p2" } }))!.text).toBe(
      "couldn't write the behaviors"
    );
    const n = notifFromEvent(env({ event: "session.test_plan_failed", data: { plan: "p2", error: "x" } }))!;
    expect(n).toMatchObject({ rule: "verify_plan_failed", verify: "p2", cls: "n-warn" });
  });

  it("a run that found a failure is news; a clean run is not", () => {
    const bad = notifFromEvent(
      env({ event: "session.test_plan_checked", new: "failed", data: { plan: "p3", title: "T", failed: 2 } })
    )!;
    expect(bad).toMatchObject({ rule: "verify_run_failed", verify: "p3", text: "behaviors failed — 2 behaviors" });
    expect(notifFromEvent(env({ event: "session.test_plan_checked", new: "", data: { plan: "p3" } }))).toBeNull();
  });

  it("a person's Fail that turned the behaviors red is news too (round 5, M5)", () => {
    const n = notifFromEvent(
      env({
        event: "session.test_plan_step_failed",
        new: "failed",
        data: { plan: "p4", title: "add-health", step: 1, text: "GET /health on the dev server" },
      })
    )!;
    expect(n).toMatchObject({
      rule: "verify_run_failed",
      verify: "p4",
      name: "add-health",
      cls: "n-warn",
      text: "behavior failed — GET /health on the dev server",
    });
  });

  it("falls back to the event's session when the data names no plan", () => {
    expect(notifFromEvent(env({ event: "session.test_plan_failed" }))!.verify).toBe("shop@feature/x");
  });

  it("flies the Sky to those behaviors and is named for them", () => {
    const bell = code("components/NotificationsBell.tsx");
    expect(bell).toMatch(/else if \(n\.verify\) \{[\s\S]{0,200}openBehaviors\(\{ planId: n\.verify \}\)/);
    expect(bell).not.toContain('openDialogFor("verify"');
    expect(bell).toContain('"Behaviors · " + (n.name || windowName(n.session))');
  });
});

describe("Verify has no top-bar count", () => {
  it("the bar carries no Verify button or badge (docs/behaviors.md §4)", () => {
    const bar = code("components/TopBar.tsx");
    expect(bar).not.toContain('id="verify-btn"');
    expect(bar).not.toContain("verifyCountLine");
    // The failure red the bar's other counts still wear.
    expect(css("components/TopBar.css")).toContain("#topbar .tb-count.is-failing {");
  });
});

describe("a checklist made empty on purpose", () => {
  const plan = (o: Partial<TestPlan>): TestPlan =>
    ({ id: "p", title: "p", state: "generated", steps: [], runs: [], live_at: 0, merged_at: 0, ...o }) as unknown as TestPlan;

  it("is the person's to fill, not a failure", () => {
    const p = plan({ origin: "manual" });
    expect(isEmptyByChoice(p)).toBe(true);
    expect(planVerdict(p)).toMatchObject({ key: "empty", label: "Empty — add a step", tone: "you" });
    expect(isFailing(p)).toBe(false);
    expect(isWaitingOnYou(p)).toBe(true);
    expect(planStatus(p)).toMatchObject({ tone: "you", action: "answer", actionLabel: "Add a behavior" });
  });

  it("a generated checklist with no steps is Not written yet — grey, not a failure (round 5, M4)", () => {
    const p = plan({});
    expect(isEmptyByChoice(p)).toBe(false);
    expect(planVerdict(p)).toMatchObject({ key: "unwritten", label: "Not written yet", short: "Not written", tone: "wait" });
    expect(isFailing(p)).toBe(false);
  });

  it("a rewrite that failed reads as the failure again", () => {
    const p = plan({ origin: "manual", state: "failed" });
    expect(isEmptyByChoice(p)).toBe(false);
    expect(planVerdict(p).key).toBe("unwritten");
  });
});
