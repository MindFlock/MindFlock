/** Verify stops being a place and becomes a stop on the route
 * (docs/behaviors.md §4–§5, docs/vision/verify.md): every way in flies the
 * Sky (`openBehaviors`), the Verify dialog / sidebar bar / top-bar button are
 * gone, and a Verify stop whose behaviors failed HALTS the route with
 * Retry · Skip · Let the agent fix it on its card. */

import { describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";
import type { Flight, FlightStep, StepRunState } from "../lib/flights";

const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => storage.get(k) ?? null,
  setItem: (k: string, v: string) => void storage.set(k, v),
  removeItem: (k: string) => void storage.delete(k),
});
vi.mock("../api/client", () => ({ api: vi.fn(), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { useUi } = await import("../state/store");
const { useBehaviorsFocus, verifyDialogTarget } = await import("../lib/behaviorsFocus");
const { openChecklist } = await import("../lib/checklistLink");
const { verifyStopStand, haltedLine } = await import("../components/behaviors/cardModel");
const { verifyStopFace } = await import("../lib/behaviors");

const step = (id: string, kind = "verify"): FlightStep => ({ id, kind, title: kind === "verify" ? "Verify" : "Build", config: {}, on_fail: "halt" }) as unknown as FlightStep;
const rec = (state: StepRunState["state"], detail: Record<string, unknown> = {}): StepRunState => ({ state, since: 1, note: "", detail, rounds: 0 });
const flight = (state: Flight["state"], states: Record<string, StepRunState>, cursor = 1): Flight =>
  ({
    id: "f1",
    plan_id: "p",
    plan_name: "",
    steps: [step("s1", "agent"), step("s2")],
    subject: { kind: "manual", source: "", id: "", title: "", url: "" },
    session: "lead",
    repo_root: "/r",
    branch: "b",
    state,
    cursor,
    reason: "",
    step_states: states,
    history: [],
    created: 0,
    updated: 0,
  }) as unknown as Flight;

describe("a Verify stop on the route", () => {
  it("halts the route when its behaviors failed — its card offers Retry · Skip · Let the agent fix it", () => {
    expect(verifyStopStand(flight("halted", { s1: rec("done"), s2: rec("failed") }), "s2")).toBe("halted");
    expect(haltedLine(2)).toBe("Failed · 2 — halted. The route waits here until you Retry, Skip or let the agent fix it.");
    expect(verifyStopFace("Failed · 2", "halted")).toBe("Failed · 2 — halted");
    expect(verifyStopFace("Needs you · 1", "you")).toBe("Needs you · 1");
    const card = code("components/behaviors/BehaviorsCard.tsx");
    expect(card).toContain('{stand === "halted" ? haltedLine(s.failed) : s.sentence}');
    for (const label of ['"Retry"', '"Skip"', '"Let the agent fix it"']) expect(card).toContain(label);
    expect(card).toContain('"/steps/" + encodeURIComponent(flightStep.stepId) + "/op/fix"');
    // No blanket Done: behaviors are answered, or the stop is skipped.
    expect(card).not.toMatch(/stepDone\(/);
  });

  it("puts the route on Needs you while behaviors wait on a person", () => {
    const f = flight("waiting_you", { s1: rec("done"), s2: rec("waiting_you") });
    expect(verifyStopStand(f, "s2")).toBe("you");
  });

  it("says nothing for a stop the route is not at, or while the agent fixes it", () => {
    expect(verifyStopStand(flight("halted", { s1: rec("failed"), s2: rec("pending") }, 0), "s2")).toBe("");
    expect(verifyStopStand(flight("running", { s1: rec("done"), s2: rec("running", { phase: "fix" }) }), "s2")).toBe("fixing");
    expect(verifyStopStand(null, "s2")).toBe("");
  });
});

describe("every way in flies the Sky", () => {
  it("the session menu's Behaviors and the verdict chip name the plan, the title and the branch", () => {
    useUi.getState().setView("grid");
    openChecklist("feature/x");
    expect(useUi.getState().view).toBe("sky");
    expect(useBehaviorsFocus.getState().request).toMatchObject({ branch: "feature/x" });
    expect(code("components/dialogs/VerdictChip.tsx")).toContain("openBehaviors({ planId: verify?.plan || plan?.id, title: inst.title, branch });");
  });

  it("an old Verify-dialog target is tried as a plan id, a title and a branch", () => {
    expect(verifyDialogTarget("feature/x")).toEqual({ planId: "feature/x", title: "feature/x", branch: "feature/x" });
    expect(verifyDialogTarget("")).toEqual({ next: true });
    expect(verifyDialogTarget(null)).toEqual({ next: true });
  });

  it("the watch pane's back button flies to the behaviors it is checking", () => {
    const pane = code("components/grid/SpecialPane.tsx");
    expect(pane).toContain("openBehaviors(planId ? { planId } : { next: true });");
    expect(pane).not.toContain('openDialogFor("verify")');
  });

  it("the sidebar Verify bar and the Verify dialog are gone", () => {
    expect(code("components/sidebar/SidebarBars.tsx")).not.toContain("VerifyBar");
    expect(() => code("components/sidebar/VerifyBar.tsx")).toThrow();
    for (const f of ["VerifyDialog", "VerifyPlan", "VerifyStep", "VerifyChrome"]) expect(() => code("components/dialogs/" + f + ".tsx"), f).toThrow();
  });

  it("no file in this task's reach opens the Verify dialog any more", () => {
    for (const f of [
      "App.tsx",
      "components/TopBar.tsx",
      "components/NotificationsBell.tsx",
      "components/palette/CommandPalette.tsx",
      "components/grid/SpecialPane.tsx",
      "components/dialogs/VerdictChip.tsx",
      "lib/keymap.ts",
      "lib/stage.ts",
      "lib/checklistLink.ts",
    ])
      expect(code(f), f).not.toContain('openDialogFor("verify"');
  });
});
