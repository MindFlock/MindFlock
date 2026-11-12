/** Stability sweep (2026-11-12): route buttons against the state the server
 * will find them in.
 * - "Let the agent fix it" on a route with no session (a free run): the
 *   server's fix op always refuses it (409 `no_session`, "this route has no
 *   session whose agent could fix it"), so it is not offered.
 * - Skip on a paused route carries on too — but not when the skipped stop
 *   was the last one left: the skip finished the route, and the extra Carry
 *   on popped "Carry on failed — this route has already finished".
 * - A stop put in AT the current one (a paused route's Undo, a ready
 *   route's first place) becomes the current stop: the server never moves
 *   the cursor for it, so the screen's guess must not either (it drew the
 *   new stop as already flown).
 * - A stop picked from the palette opens in its dialog once the SERVER's
 *   flight has it: it opened on the screen's guess (a temporary `s_tmp…`
 *   id), which the refetch then dropped — the dialog closed under the
 *   person, and a quick Save sent a PATCH for an id the server never had. */
import { describe, expect, it, vi } from "vitest";
import type { Flight, FlightStep } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), instApi: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { canFixStep, skipCarriesOn } = await import("../components/flights/FlightStrip");
const { freeStopRemedies } = await import("../components/flights/useGroundedLine");
const { withFixItem } = await import("../components/flights/useChainMenu");
const { stopToOpen, withSteps } = await import("../lib/flightOptimism");

const step = (id: string, over: Partial<FlightStep> = {}): FlightStep => ({ id, kind: "agent", title: id, config: {}, on_fail: "halt", ...over });
const rec = (state: string, over: Record<string, unknown> = {}) => ({ state, since: 0, note: "", detail: {}, rounds: 0, ...over }) as Flight["step_states"][string];
const flight = (over: Partial<Flight> = {}): Flight =>
  ({ id: "f", plan_id: "", plan_name: "", steps: ["s1", "s2", "s3"].map((s) => step(s)), state: "running", cursor: 0, step_states: {}, session: "claude", ...over }) as unknown as Flight;

describe("Let the agent fix it needs a session's agent", () => {
  const failed = (session: string) => flight({ state: "halted", cursor: 1, session, step_states: { s1: rec("done"), s2: rec("failed") } });

  it("a failed stop with a session: offered; a free run (no session): not", () => {
    expect(canFixStep(failed("claude"))).toBe(true);
    expect(canFixStep(failed(""))).toBe(false);
  });

  it("the free run's stop dialog and right-click menu leave it out", () => {
    const f = failed("");
    expect(freeStopRemedies(f, { index: 1, state: "halted", step: f.steps[1] }).fix).toBe(false);
    const items = [
      { key: "retry", label: "Retry", title: "" },
      { key: "skip", label: "Skip", title: "" },
    ] as Parameters<typeof withFixItem>[0];
    expect(withFixItem(items, { state: "halted", title: "s2" }, false).map((i) => i.key)).toEqual(["retry", "skip"]);
    expect(withFixItem(items, { state: "halted", title: "s2" }).map((i) => i.key)).toEqual(["retry", "fix", "skip"]);
  });
});

describe("Skip on a paused route carries on only when there is somewhere to go", () => {
  it("a stop ahead: carries on", () => {
    expect(skipCarriesOn(flight({ state: "paused", cursor: 1, step_states: { s1: rec("done") } }))).toBe(true);
  });
  it("the last stop: the skip finishes the route, nothing to carry on", () => {
    expect(skipCarriesOn(flight({ state: "paused", cursor: 2, step_states: { s1: rec("done"), s2: rec("done") } }))).toBe(false);
    // Every stop after it already flown (skipped): the same.
    expect(skipCarriesOn(flight({ state: "paused", cursor: 1, step_states: { s1: rec("done"), s3: rec("skipped") } }))).toBe(false);
  });
  it("the next stop a halt point: it halts there, no Carry on", () => {
    const f = flight({ state: "paused", cursor: 0, steps: [step("s1"), step("s2", { halt: true }), step("s3")] });
    expect(skipCarriesOn(f)).toBe(false);
  });
  it("not paused: Skip alone", () => {
    expect(skipCarriesOn(flight({ state: "halted", cursor: 1 }))).toBe(false);
    expect(skipCarriesOn(flight({ state: "waiting_you", cursor: 1 }))).toBe(false);
  });
});

describe("the screen's guess keeps the cursor where the server keeps it", () => {
  it("put in AT the current stop of a paused or ready route: it is the current stop", () => {
    const paused = withSteps([step("n")], 1)(flight({ state: "paused", cursor: 1 }));
    expect(paused.cursor).toBe(1);
    expect(paused.steps[1].id.startsWith("s_tmp")).toBe(true);
    expect(withSteps([step("n")], 0)(flight({ state: "ready", cursor: 0 })).cursor).toBe(0);
  });
  it("put in before it: the cursor follows its stop", () => {
    expect(withSteps([step("n")], 0)(flight({ state: "paused", cursor: 2 })).cursor).toBe(3);
  });
});

describe("a stop just tacked on opens once the server's flight has it", () => {
  it("the guess at that place is passed over; the real stop opens", () => {
    const guess = withSteps([step("n")], 1)(flight({ state: "paused", cursor: 1 }));
    const nodes = (f: Flight) => f.steps.map((st, index) => ({ id: "stop:" + st.id, index, step: st }));
    expect(stopToOpen(nodes(guess), 1)).toBeNull();
    const server = flight({ state: "paused", cursor: 1, steps: [step("s1"), step("s7"), step("s2"), step("s3")] });
    expect(stopToOpen(nodes(server), 1)?.id).toBe("stop:s7");
    expect(stopToOpen(nodes(server), 9)).toBeNull();
  });
});
