/** Sky and routes clarity, round 1 — reading a line at a glance: the agent
 * tile says what the AGENT does (never the route's wait), the frame's bar
 * says where the route is without repeating its chip, and for how long, a
 * stop that waits on you says what for, and a finished route says so. */

import { describe, expect, it } from "vitest";
import type { Instance } from "../api/types";
import type { Flight, FlightStep, StepRunState } from "../lib/flights";

const { flightDiagram, flightStops, lineWhere, reasonAfterTitle, sessionDiagram, whereBesideChip } = await import("../lib/flightDiagram");
const { routeClock, stateSince, withClock } = await import("../lib/lineTime");
const { stopWhy } = await import("../components/flights/SkyChain");
const { PAUSED_FLIGHT_LABEL } = await import("../lib/sessionState");

const NOW_MS = Date.UTC(2026, 10, 20, 15, 0, 0);
const NOW = NOW_MS / 1000;
const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}, extra: Partial<FlightStep> = {}): FlightStep =>
  ({ id, kind, title, config, on_fail: "halt", ...extra }) as FlightStep;
const rec = (state: string, note = "", since = NOW - 12 * 60, detail: Record<string, unknown> = {}): StepRunState =>
  ({ state, since, note, detail, rounds: 0 }) as unknown as StepRunState;

const STEPS = [step("s1", "agent", "Work the ticket"), step("s2", "verify", "Verify", { who: "both" }), step("s3", "checks", "CI checks", { timeout_minutes: 60 }), step("s4", "ship", "Open PR", { lane: "pr" })];

function flight(over: Partial<Flight> = {}): Flight {
  return {
    id: "f_1",
    plan_id: "adhoc",
    plan_name: "Ticket to merge",
    steps: STEPS,
    subject: {},
    session: "me",
    repo_root: "/r",
    branch: "b",
    state: "running",
    cursor: 1,
    step_states: { s1: rec("done"), s2: rec("running", "2 of 4 steps done") },
    reason: "",
    history: [],
    started: NOW - 3600,
    updated: NOW - 60,
    finished: 0,
    by: "user",
    ...over,
  } as unknown as Flight;
}

const rowOf = (over: Record<string, unknown> = {}) =>
  ({ title: "me", status: "running", activity: "idle", stage: "agent", task_headline: "Do me", parent: "", branch: "me", flight: null, autopilot: null, ...over }) as unknown as Instance;

describe("the agent tile is the agent", () => {
  it("a halted route does not paint the agent Halted: the agent is Idle, the stop is red", () => {
    const f = flight({ state: "halted", cursor: 2, reason: "CI checks failed: test_export_empty", step_states: { s1: rec("done"), s2: rec("done"), s3: rec("failed", "test_export_empty") } });
    const row = rowOf({ flight: { id: "f_1", plan_name: "Ticket to merge", cursor: 2, total: 4, state: "halted", step: { id: "s3", kind: "checks", title: "CI checks", state: "failed", actor: "robot" } } });
    const head = sessionDiagram([row], "me", { flights: [f] }).nodes.find((n) => n.kind === "session")!;
    expect(head.chip).toEqual({ label: "Idle", tone: "neutral" });
  });

  it("a route waiting on a Wait for me stop leaves the agent's own state on its tile", () => {
    const steps = [step("s1", "agent", "Work"), step("s2", "you", "Wait for me: Approve the merge", { text: "Look it over" })];
    const f = flight({ steps, state: "waiting_you", cursor: 1, step_states: { s1: rec("done"), s2: rec("waiting_you") } });
    const row = rowOf({ activity: "working", flight: { id: "f_1", plan_name: "P", cursor: 1, total: 2, state: "waiting_you", step: { id: "s2", kind: "you", title: "Wait for me: Approve the merge", state: "waiting_you", actor: "you" } } });
    const head = sessionDiagram([row], "me", { flights: [f] }).nodes.find((n) => n.kind === "session")!;
    expect(head.chip?.label).toBe("Working");
  });
});

describe("a stop that waits on you says what for", () => {
  it("a Verify stop handed to you shows the server's note, its settings move to the tooltip", () => {
    const f = flight({ state: "waiting_you", step_states: { s1: rec("done"), s2: rec("waiting_you", "2 steps for you to check, then confirm") } });
    const n = flightStops(f).find((x) => x.index === 1)!;
    expect(n.state).toBe("perched");
    expect(n.sub).toBe("2 steps for you to check, then confirm");
    expect(lineWhere(flightDiagram(f))).toBe("needs you at Verify · 2 steps for you to check, then confirm");
  });

  it("a Wait for me stop keeps its own text", () => {
    const steps = [step("s1", "agent", "Work"), step("s2", "you", "Approve", { text: "Look it over and press Done" })];
    const f = flight({ steps, state: "waiting_you", cursor: 1, step_states: { s1: rec("done"), s2: rec("waiting_you", "waits for your Done") } });
    const n = flightStops(f).find((x) => x.index === 1)!;
    expect(n.sub).not.toBe("waits for your Done");
  });
});

describe("the bar never says the chip's word twice", () => {
  it("a failure that names its stop is said once", () => {
    expect(reasonAfterTitle("CI checks", "CI checks failed: test_export_empty")).toBe("failed: test_export_empty");
    expect(reasonAfterTitle("CI checks", "CI checks failed: x", true)).toBe("Failed: x");
    expect(reasonAfterTitle("Reviews settle", "Checks went red")).toBe("Checks went red");
    const f = flight({ state: "halted", cursor: 2, reason: "CI checks failed: test_export_empty", step_states: { s1: rec("done"), s2: rec("done"), s3: rec("failed") } });
    expect(lineWhere(flightDiagram(f))).toBe("halted at CI checks · failed: test_export_empty");
    expect(stopWhy({ state: "halted", title: "CI checks", reason: "CI checks failed: test_export_empty" })).toBe("Failed: test_export_empty");
  });

  it("beside Halted / Paused route chips the state word goes", () => {
    expect(whereBesideChip("halted at CI checks · failed: x", { reason: "flight_halted", label: "Halted" })).toBe("at CI checks · failed: x");
    expect(whereBesideChip("paused at Open PR", { label: PAUSED_FLIGHT_LABEL })).toBe("at Open PR");
    expect(whereBesideChip("paused before Open PR · your halt point", { label: PAUSED_FLIGHT_LABEL })).toBe("before Open PR · your halt point");
    // Any other chip keeps the line whole.
    expect(whereBesideChip("halted at CI checks", { reason: "question", label: "Question" })).toBe("halted at CI checks");
    expect(whereBesideChip("needs you at Review", { reason: "flight_you" })).toBe("");
  });

  it("a halt point stops the route BEFORE its stop", () => {
    const steps = STEPS.map((s) => (s.id === "s4" ? { ...s, halt: true } : s)) as FlightStep[];
    const f = flight({ steps, state: "paused", reason: "halt point", cursor: 3, step_states: { s1: rec("done"), s2: rec("done"), s3: rec("done"), s4: rec("pending", "", NOW - 60, { halt_hit: true }) } });
    expect(lineWhere(flightDiagram(f))).toBe("paused before Open PR · your halt point");
    const mine = flight({ state: "paused", cursor: 3, step_states: { s1: rec("done"), s2: rec("done"), s3: rec("done"), s4: rec("running") } });
    expect(lineWhere(flightDiagram(mine))).toMatch(/^paused at Open PR/);
  });
});

describe("the bar says for how long", () => {
  it("a stop at work or waiting on you: since its record", () => {
    expect(stateSince(flight())).toBe(NOW - 12 * 60);
    expect(routeClock(flight(), NOW_MS)?.text).toBe("for 12 min");
    expect(routeClock(flight(), NOW_MS)?.title).toMatch(/^At this stop since \d\d:\d\d$/);
    const you = flight({ state: "waiting_you", step_states: { s1: rec("done"), s2: rec("waiting_you", "", NOW - 2 * 3600 - 5 * 60) } });
    expect(routeClock(you, NOW_MS)?.text).toBe("for 2 h 05 min");
    expect(routeClock(you, NOW_MS)?.title).toMatch(/^Waiting on you since /);
  });

  it("halted and paused: since the failure / the pause, from the history", () => {
    const halted = flight({ state: "halted", history: [{ at: NOW - 3 * 3600, step: "s2", event: "failed", note: "x" }] });
    expect(routeClock(halted, NOW_MS)?.text).toBe("for 3 h");
    const paused = flight({ state: "paused", history: [{ at: NOW - 35 * 60, step: "s2", event: "paused", note: "paused by you" }] });
    expect(routeClock(paused, NOW_MS)?.text).toBe("for 35 min");
    expect(routeClock(flight({ state: "paused" }), NOW_MS)).toBeNull();
  });

  it("nothing under a minute, nothing for a placeholder time, nothing when ready", () => {
    expect(routeClock(flight({ step_states: { s1: rec("done"), s2: rec("running", "", NOW - 20) } }), NOW_MS)).toBeNull();
    expect(routeClock(flight({ step_states: { s1: rec("done"), s2: rec("running", "", 1) } }), NOW_MS)).toBeNull();
    expect(routeClock(flight({ state: "ready" }), NOW_MS)).toBeNull();
    expect(routeClock(null, NOW_MS)).toBeNull();
  });

  it("a finished route says when it finished", () => {
    expect(routeClock(flight({ state: "done", finished: NOW - 15 * 60 }), NOW_MS)?.text).toBe("route finished 15 min ago");
    expect(routeClock(flight({ state: "done", finished: NOW - 10 }), NOW_MS)?.text).toBe("route finished just now");
  });

  it("joins after the where-line", () => {
    expect(withClock("at Verify · 2 of 4 steps done", { text: "for 4 min" })).toBe("at Verify · 2 of 4 steps done · for 4 min");
    expect(withClock("", { text: "for 4 min" })).toBe("for 4 min");
    expect(withClock("at Verify", null)).toBe("at Verify");
  });
});
