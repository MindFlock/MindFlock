/** Round 19 frontend: a ready flight (built, waiting for ▶ Run or the
 * session's next prompt) is drawn on its session's line with every stop
 * editable, and its session tile says how it starts. */
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Instance } from "../api/types";
import type { Flight, FlightStep } from "../lib/flights";
import { isLive, isShown } from "../lib/flights";
import { sessionDiagram } from "../lib/flightDiagram";
import { ReadyHead, startWord } from "../components/flights/ReadyHead";

const step = (id: string, kind = "agent"): FlightStep => ({ id, kind, title: id, config: {}, on_fail: "halt" });
const ready = (start_on: "prompt" | "run" = "prompt"): Flight =>
  ({ id: "f1", plan_id: "", plan_name: "Ship it", steps: [step("s1", "ship"), step("s2", "ship")], state: "ready", cursor: 0, step_states: {}, session: "me", start_on, ready_since: 1, history: [] }) as unknown as Flight;
const row = (f: Flight) =>
  ({ title: "me", status: "running", activity: "idle", stage: "agent", task_headline: "", parent: "", autopilot: null, flight: { id: f.id, plan_name: f.plan_name, cursor: 0, total: f.steps.length, state: "ready", step: { id: "s1", kind: "ship", title: "s1", state: "pending", actor: "robot" } } }) as unknown as Instance;

describe("a ready flight", () => {
  it("is shown but not live", () => {
    expect(isShown({ state: "ready" })).toBe(true);
    expect(isLive({ state: "ready" })).toBe(false);
  });
  it("its session's line draws every stop, each one editable, inserts allowed from the first", () => {
    const f = ready();
    const d = sessionDiagram([row(f)], "me", { flights: [f] });
    expect(d.mode).toBe("flight");
    expect(d.insertFrom).toBe(0);
    const stops = d.nodes.filter((n) => n.kind === "stop");
    expect(stops.map((n) => [n.id, n.state, n.editable])).toEqual([
      ["stop:s1", "pending", true],
      ["stop:s2", "pending", true],
    ]);
  });
  it("its session tile: Ready, ▶ Run, and how it starts", () => {
    const html = renderToStaticMarkup(createElement(ReadyHead, { flight: ready() }));
    expect(html).toContain(">Ready<");
    expect(html).toContain("▶ Run");
    expect(html).toContain("runs on your next prompt");
    expect(startWord({ start_on: "run" })).toBe("runs when you press ▶ Run");
  });
});
