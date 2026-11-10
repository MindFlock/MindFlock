/** Flow polish (fp-sky-live): at a glance every stop says what it is doing
 * right now or why it waits, and the way out is one click on its tile —
 * a running stop shows the server's live note, a failure handed to you says
 * why and offers Retry, a paused route offers Carry on · Skip (a halt point
 * also rings the bell), a Verify stop that couldn't finish offers Retry, the
 * Sky's Verify tile speaks the row's verify words, one verify is one bell
 * row, and the pane's route strip leads with why it is parked. */

/// <reference types="vite/client" />
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { Instance, SessionVerify, TestPlan } from "../api/types";
import type { Flight, FlightRow, FlightStep, StepRunState } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
  documentElement: { classList: { contains: () => false, add: () => {}, remove: () => {}, toggle: () => {} } },
};

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { flightDiagram, flightStops, lineWhere, liveNote } = await import("../lib/flightDiagram");
const { stopWhy, stopTileBoxes, behaviorsMarkOf, PAUSED_LINE, HALT_POINT_LINE } = await import("../components/flights/SkyChain");
const { YouStepActions, FlightCard, VerifyWalkHint, VERIFY_WALK_HERE, verifyStopWhy } = await import("../components/flights/FlightStrip");
const { stripLabel, haltedWhy: haltedWhyFlights } = await import("../lib/flights");
const { deriveSessionState, haltedWhy, verifyGlyph, verifyHint, verifyLine, verifyWord } = await import("../lib/sessionState");
const { needsYouRows, pausedAtLine } = await import("../lib/needsYou");

const NOW = Math.floor(Date.now() / 1000);
const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}, extra: Partial<FlightStep> = {}): FlightStep =>
  ({ id, kind, title, config, on_fail: "halt", ...extra }) as FlightStep;
const rec = (state: string, note = "", detail: Record<string, unknown> = {}): StepRunState =>
  ({ state, since: NOW - 120, note, detail, rounds: 0 }) as unknown as StepRunState;

const STEPS = [step("s1", "agent", "Work the ticket"), step("s2", "checks", "CI checks", { timeout_minutes: 60 }), step("s3", "ship", "Push", { lane: "push" })];

function flight(over: Partial<Flight> = {}, steps: FlightStep[] = STEPS): Flight {
  return {
    id: "f_1",
    plan_id: "adhoc",
    plan_name: "Ticket to merge",
    steps,
    subject: {},
    session: "lead",
    repo_root: "/r",
    branch: "b",
    state: "running",
    cursor: 1,
    step_states: { s1: rec("done"), s2: rec("running", "3 new review comments for the agent") },
    reason: "",
    history: [],
    started: NOW - 600,
    updated: NOW - 60,
    finished: 0,
    by: "user",
    ...over,
  } as unknown as Flight;
}

function withClient(el: ReactElement, prime: (c: InstanceType<typeof QueryClient>) => void = () => {}) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  prime(c);
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}

const stopAt = (f: Flight, i: number) => flightStops(f).find((n) => n.index === i)!;

describe("1 · a running stop says what it is doing right now", () => {
  it("the current stop's sub is the server's note; its settings move to `summary`", () => {
    const n = stopAt(flight(), 1);
    expect(n.state).toBe("current");
    expect(n.sub).toBe("3 new review comments for the agent");
    expect(n.summary).toBe("up to 60 min");
  });

  it("a stop ahead keeps its settings; a current stop with no note falls back to them", () => {
    expect(stopAt(flight(), 2).sub).toBe("");
    const quiet = flight({ step_states: { s1: rec("done"), s2: rec("running") } });
    expect(stopAt(quiet, 1).sub).toBe("up to 60 min");
    expect(stopAt(quiet, 1).summary).toBeUndefined();
  });

  it("a long note is clipped to one line", () => {
    const long = "the agent is verifying its work · 3 of 7 answered · quiet 6 min · and then some more words";
    expect(liveNote(long).length).toBeLessThanOrEqual(60);
    expect(liveNote(long).endsWith("…")).toBe(true);
    expect(liveNote("  the agent\n is working ")).toBe("the agent is working");
  });

  it("the line's bar says it too", () => {
    expect(lineWhere(flightDiagram(flight()))).toBe("at CI checks · 3 new review comments for the agent");
  });
});

describe("2 · a failure handed to you says why, and Retry runs it again", () => {
  const handed = flight({
    state: "waiting_you",
    step_states: { s1: rec("done"), s2: rec("waiting_you", "CI checks failed: 2 of 14 red", { failed_reason: "2 of 14 checks red" }) },
  });

  it("the tile's line is the reason, not a blank amber box", () => {
    const n = stopAt(handed, 1);
    expect(n.state).toBe("perched");
    expect(n.reason).toBe("2 of 14 checks red");
    expect(stopWhy(n)).toBe("Failed: 2 of 14 checks red");
    // A reason that already says so is not doubled.
    expect(stopWhy({ ...n, reason: "failed to start" })).toBe("Failed to start");
    // A Wait for me stop still says what it asks.
    expect(stopWhy({ state: "perched", title: "Approve", reason: "", step: step("y", "you", "Approve", { text: "Look at the PR" }) })).toBe("Look at the PR");
  });

  it("its tile gets a halted stop's room for its three buttons", () => {
    const boxes = stopTileBoxes(flightDiagram(handed), handed);
    expect(Object.keys(boxes)).toHaveLength(1);
  });

  it("the buttons: Retry (primary), Let the agent fix it, Skip — never Done", () => {
    const html = withClient(createElement(YouStepActions, { flight: handed }));
    expect(html).toContain("Failed: 2 of 14 checks red");
    expect(html).toMatch(/class="btn-primary fl-btn"[^>]*>Retry</);
    expect(html).toContain(">Let the agent fix it<");
    expect(html).toContain(">Skip<");
    expect(html).not.toContain(">Done<");
  });

  it("a Verify stop whose failure was handed to you takes the same buttons", () => {
    const V = [step("s1", "agent", "Build"), step("s2", "verify", "Verify", { who: "agent" })];
    const f = flight({ state: "waiting_you", step_states: { s1: rec("done"), s2: rec("waiting_you", "", { failed_reason: "2 steps failed" }) } }, V);
    const html = withClient(createElement(YouStepActions, { flight: f }));
    expect(html).toContain(">Retry<");
    expect(html).not.toContain("fl-you-verify");
  });
});

describe("3 · a paused route has a one-click way on", () => {
  const paused = flight({ state: "paused", step_states: { s1: rec("done"), s2: rec("running", "the agent is working") } });
  const atHalt = flight({
    state: "paused",
    reason: "halt point",
    steps: [STEPS[0], { ...STEPS[1], halt: true }, STEPS[2]],
    step_states: { s1: rec("done"), s2: rec("pending") },
  });

  it("a paused stop says so on its tile and makes room for its buttons", () => {
    const n = stopAt(paused, 1);
    expect(n.state).toBe("paused");
    expect(stopWhy(n)).toBe(PAUSED_LINE);
    expect(stopWhy(stopAt(atHalt, 1))).toBe(HALT_POINT_LINE);
    expect(Object.keys(stopTileBoxes(flightDiagram(paused), paused))).toEqual([n.id]);
  });

  it("Carry on (primary) and Skip — on a manual pause too", () => {
    for (const f of [paused, atHalt]) {
      const html = withClient(createElement(YouStepActions, { flight: f }));
      expect(html).toMatch(/class="btn-primary fl-btn fl-carry"[^>]*>Carry on</);
      expect(html).toContain(">Skip<");
      expect(html).not.toContain(">Retry<");
    }
  });

  it("the rail's tooltip names Carry on, never a menu item that doesn't exist", () => {
    const row = { id: "f_1", plan_name: "Ticket to merge", cursor: 1, total: 3, state: "paused", reason: "", step: { id: "s2", kind: "checks", title: "CI checks", state: "running", actor: "robot" } };
    const ss = deriveSessionState({ title: "lead", status: "running", activity: "idle", flight: row } as unknown as Instance);
    expect(ss.hint).toContain("Carry on");
    expect(ss.hint).not.toContain("Resume it from");
    const halt = deriveSessionState({ title: "lead", status: "running", activity: "idle", flight: { ...row, reason: "halt point" } } as unknown as Instance);
    expect(halt.hint).toContain("reached its halt point at CI checks");
  });

  const inst = (f: Flight) =>
    ({
      title: "lead",
      status: "running",
      activity: "idle",
      stage: "agent",
      branch: "",
      flight: { id: f.id, plan_name: f.plan_name, cursor: f.cursor, total: f.steps.length, state: f.state, reason: f.reason, step: { id: "s2", kind: "checks", title: "CI checks", state: "pending", actor: "robot" } },
    }) as unknown as Instance;

  it("a route paused AT A HALT POINT rings the bell: Paused at <stop> — Carry on", () => {
    const rows = needsYouRows({ instances: [inst(atHalt)], waiting: [], flights: [atHalt] });
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe("wait_for_me");
    expect(rows[0].waiting?.reason).toBe("Paused at CI checks — Carry on");
    expect(rows[0].flight?.id).toBe("f_1");
    expect(pausedAtLine("")).toBe("Paused at its halt point — Carry on");
    // From the full flights alone (a route with no session row) too.
    expect(needsYouRows({ instances: [], waiting: [], flights: [atHalt] })).toHaveLength(1);
  });

  it("a pause a person made stays out of the bell", () => {
    expect(needsYouRows({ instances: [inst(paused)], waiting: [], flights: [paused] })).toEqual([]);
  });
});

describe("4 · a Verify stop that couldn't finish: why, Retry, Skip", () => {
  const V = [step("s1", "agent", "Build"), step("s2", "verify", "Verify", { who: "agent" })];
  const ROW = { title: "lead", branch: "b", repo: "r" } as Instance;
  const parked = (ask: string, note: string) =>
    flight({ state: "waiting_you", step_states: { s1: rec("done"), s2: rec("waiting_you", note, { ask, items: ["x — press Retry to verify again, or Skip"] }) } }, V);
  const render = (f: Flight, compact = false) => withClient(createElement(YouStepActions, { flight: f, compact }), (c) => c.setQueryData(["instances"], [ROW]));

  it("blocked: the reason, Retry (primary) and Skip", () => {
    const html = render(parked("blocked", "the check couldn't finish: the window closed"));
    expect(html).toContain("The check couldn&#x27;t finish: the window closed");
    expect(html).toMatch(/class="btn-primary fl-btn fl-retry"[^>]*>Retry</);
    expect(html).toContain(">Skip<");
    expect(html).not.toContain("I did it");
  });

  it("memory: Retry too; the dead `needs_you` ask is gone", () => {
    expect(render(parked("memory", "couldn't start the check: memory is low"))).toContain(">Retry<");
    const old = render(parked("needs_you", "do this"));
    expect(old).not.toContain("I did it");
    expect(verifyStopWhy("", "memory")).toBe("Couldn't start the check: memory is low");
    expect(verifyStopWhy("", "blocked")).toBe("The check couldn't finish");
  });

  it("the compact strip keeps Retry and Skip", () => {
    const html = render(parked("blocked", "the check couldn't finish: x"), true);
    expect(html).toContain(">Retry<");
    expect(html).toContain(">Skip<");
  });

  it("the pane's walk button speaks Verify, not walking", () => {
    const here = renderToStaticMarkup(createElement(VerifyWalkHint, { session: "lead", here: true, onOpen: () => {} }));
    expect(here).toContain(VERIFY_WALK_HERE);
    expect(VERIFY_WALK_HERE).toBe("Its agent is verifying its work — open the session to talk to it");
    expect(here).not.toMatch(/Walking|behaviors/);
  });
});

describe("5 · one Verify vocabulary on the canvas", () => {
  const plan = { id: "lead", title: "lead", state: "ready", steps: [{ id: "1", result: "fail" }], verdict: "fail" } as unknown as TestPlan;
  const v = (state: SessionVerify["state"], over: Partial<SessionVerify> = {}): SessionVerify => ({
    state,
    plan: "lead",
    steps: { total: 3, agent_done: 3, human: 0 },
    human_steps: [],
    failed_steps: [],
    detail: "",
    at: NOW,
    ...over,
  });

  it("the row's verify wins over the plan's summary, in the pill's words", () => {
    expect(behaviorsMarkOf(plan, v("needs_you"))).toEqual({ planId: "lead", short: "Confirm verified", status: "Confirm verified", tone: "you" });
    expect(behaviorsMarkOf(plan, v("verifying"))?.short).toBe("Verifying");
    expect(behaviorsMarkOf(plan, v("failed"))?.tone).toBe("bad");
    expect(behaviorsMarkOf(plan, v("blocked"))?.short).toBe("Verify blocked");
    expect(behaviorsMarkOf(plan, v("passed"))?.tone).toBe("ok");
    // Another plan's verify, or none: the plan's own summary.
    expect(behaviorsMarkOf(plan, v("needs_you", { plan: "other" }))?.short).not.toBe("Confirm verified");
    expect(behaviorsMarkOf(plan)?.short).not.toBe("Confirm verified");
    expect(behaviorsMarkOf(null, v("needs_you"))).toBeNull();
  });

  it("verifyWord is the row pill's label for each state", () => {
    expect(["verifying", "needs_you", "failed", "blocked", "passed", "?"].map(verifyWord)).toEqual(["Verifying", "Confirm verified", "Verify failed", "Verify blocked", "Verified", ""]);
  });

  it("a sign-off with no person steps says the backend's detail, not 'do yours'", () => {
    const so = v("needs_you", { detail: "every step passed with evidence" });
    expect(verifyLine(so)).toBe("Confirm verified — every step passed with evidence");
    expect(verifyHint(so)).toContain("every step passed with evidence");
    expect(verifyHint(so)).not.toContain("Do these steps");
    expect(verifyLine(v("needs_you"))).not.toContain("do yours");
    const mine = v("needs_you", { steps: { total: 3, agent_done: 2, human: 1 }, human_steps: [{ id: "h", text: "Open it", expect: "it opens" }] });
    expect(verifyLine(mine)).toContain("do yours, then confirm");
  });

  it("a glyph per state — ✓ only once verified", () => {
    expect(verifyGlyph("passed")).toBe("✓");
    for (const s of ["verifying", "needs_you", "failed", "blocked"]) expect(verifyGlyph(s)).not.toBe("✓");
  });
});

describe("6 · one verify, one bell row", () => {
  // A plan whose latest run failed its one step (`isFailing`).
  const plan = {
    id: "lead",
    title: "lead",
    state: "ready",
    steps: [{ id: "1", text: "It logs in", who: "agent" }],
    runs: [{ id: "r1", at: NOW - 60, state: "done", results: { "1": { result: "fail", note: "it didn't" } } }],
  } as unknown as TestPlan;
  const base = { title: "lead", status: "running", activity: "idle", stage: "agent", branch: "" };

  it("the plan row stays out when the session's own verify is on that plan", () => {
    const withVerify = { ...base, verify: { state: "failed", plan: "lead", steps: { total: 1, agent_done: 1, human: 0 }, human_steps: [], failed_steps: [], detail: "", at: NOW } } as unknown as Instance;
    const rows = needsYouRows({ instances: [withVerify], waiting: [], flights: [], testPlans: [plan] });
    expect(rows.filter((r) => r.kind === "behaviors")).toEqual([]);
    expect(rows.map((r) => r.title)).toEqual(["lead"]);
  });

  it("…or when its route is parked on a Verify stop", () => {
    const V = [step("s1", "agent", "Build"), step("s2", "verify", "Verify")];
    const f = flight({ state: "waiting_you", step_states: { s1: rec("done"), s2: rec("waiting_you", "", { ask: "confirm" }) } }, V);
    const row: FlightRow = { id: f.id, plan_name: f.plan_name, cursor: 1, total: 2, state: "waiting_you", step: { id: "s2", kind: "verify", title: "Verify", state: "waiting_you", actor: "robot" } };
    const rows = needsYouRows({ instances: [{ ...base, flight: row } as unknown as Instance], waiting: [], flights: [f], testPlans: [plan] });
    expect(rows.map((r) => r.kind)).toEqual(["wait_for_me"]);
  });

  it("a plan nobody else speaks for still gets its row", () => {
    const rows = needsYouRows({ instances: [base as unknown as Instance], waiting: [], flights: [], testPlans: [plan] });
    expect(rows.map((r) => r.kind)).toEqual(["behaviors"]);
  });
});

describe("7 · the pane's route strip says why it is parked", () => {
  it("Halted · <what failed, without the stop's name> · <stop>", () => {
    const f = flight({ state: "halted", reason: "CI checks failed: 2 of 14 checks red", step_states: { s1: rec("done"), s2: rec("failed") } });
    expect(stripLabel(f, Date.now())).toMatch(/· Halted · Failed: 2 of 14 checks red · CI checks \(\d+m\)$/);
    // The session row carries the reason too.
    const row = { id: "f_1", plan_name: "Ticket to merge", cursor: 1, total: 3, state: "halted", reason: "CI checks failed: 2 red", step: { id: "s2", kind: "checks", title: "CI checks", state: "failed", actor: "robot" } } as unknown as FlightRow;
    expect(stripLabel(row)).toBe("Ticket to merge · ●✗○ · Halted · Failed: 2 red · CI checks");
  });

  it("Needs you · <what> · <stop>; Paused · <stop>", () => {
    const handed = flight({ state: "waiting_you", step_states: { s1: rec("done"), s2: rec("waiting_you", "", { failed_reason: "2 red" }) } });
    expect(stripLabel(handed)).toContain("· Needs you · Failed: 2 red · CI checks");
    expect(stripLabel(flight({ state: "paused" }))).toContain("· Paused · CI checks");
  });

  it("one helper with the canvas: sessionState's haltedWhy is lib/flights'", () => {
    expect(haltedWhy).toBe(haltedWhyFlights);
  });
});

describe("8 · naming", () => {
  it("a pause a person made says Paused by you, never Halted", () => {
    const f = flight({ state: "paused" });
    const html = withClient(createElement(FlightCard, { flight: f }), (c) => c.setQueryData(["flights"], [f]));
    expect(html).toMatch(/class="fl-now-paused" title="Paused by you[^"]*">Paused</);
    expect(html).not.toContain("Halted by you");
  });
});
