/** 2026-11-02 Sky polish: optimistic flight edits (lib/flightOptimism), the
 * waiting stop's countdown (`untilOf` / `leftWord`, its tile chip), and the
 * per-device spots of sub-sessions (`withNodePos`). */
import { describe, expect, it } from "vitest";
import type { Flight, FlightStep } from "../lib/flights";
import { leftWord, untilOf, type Diagram } from "../lib/flightDiagram";
import { withSteps, withoutSteps } from "../lib/flightOptimism";
import { withNodePos } from "../components/flights/useSkyWiring";

const step = (id: string): FlightStep => ({ id, kind: "agent", title: id, config: {}, on_fail: "halt" });
const flight = (cursor: number, state: Flight["state"] = "running"): Flight =>
  ({ id: "f", plan_id: "", plan_name: "", steps: ["s1", "s2", "s3", "s4"].map(step), state, cursor, step_states: {}, session: "x" }) as unknown as Flight;

describe("optimistic edits", () => {
  it("withoutSteps drops them and the cursor follows what left before it", () => {
    const f = withoutSteps(new Set(["s1", "s4"]))(flight(2));
    expect(f.steps.map((s) => s.id)).toEqual(["s2", "s3"]);
    expect(f.cursor).toBe(1);
  });
  it("withSteps inserts under temporary ids, pending, at `at` (end when undefined)", () => {
    const f = withSteps([step("n")], 3)(flight(1));
    expect(f.steps.map((s) => s.id.startsWith("s_tmp"))).toEqual([false, false, false, true, false]);
    expect(f.step_states[f.steps[3].id].state).toBe("pending");
    expect(f.cursor).toBe(1);
    expect(withSteps([step("n")])(flight(1)).steps).toHaveLength(5);
    // Before the cursor of a flying flight: the cursor moves with its stop.
    expect(withSteps([step("n")], 0)(flight(2)).cursor).toBe(3);
    expect(withSteps([step("n")], 0)(flight(0, "grounded")).cursor).toBe(0);
  });
});

describe("the countdown", () => {
  it("untilOf reads a Wait's until or a paced Loop's next_at", () => {
    expect(untilOf({ until: 1800000000 })).toBe(1800000000);
    expect(untilOf({ next_at: 1800000100 })).toBe(1800000100);
    expect(untilOf({})).toBe(0);
    expect(untilOf(null)).toBe(0);
  });
  it("leftWord says m:ss, h:mm:ss, and due", () => {
    expect(leftWord(754)).toBe("12:34 left");
    expect(leftWord(3723)).toBe("1:02:03 left");
    expect(leftWord(0.2)).toBe("0:01 left");
    expect(leftWord(0)).toBe("due");
  });
});

describe("sub-sessions keep their spot per device", () => {
  it("withNodePos puts a spot on a node that is not a step, never on a stop", () => {
    const d = { nodes: [{ id: "child:w1", kind: "child", row: "below" }, { id: "stop:s1", kind: "stop", row: "main", step: step("s1") }], edges: [] } as unknown as Diagram;
    const out = withNodePos(d, { "child:w1": { x: 10, y: 20 }, "stop:s1": { x: 1, y: 1 } });
    expect(out.nodes[0].pos).toEqual({ x: 10, y: 20 });
    expect(out.nodes[1].pos).toBeUndefined();
    expect(withNodePos(d, {})).toBe(d);
  });
});

describe("a stops-only chain's name", () => {
  it("is the one it was given, else its first stop and how many more — never the placeholder 'Stops'", async () => {
    const { groundedName } = await import("../components/flights/GroundedLine");
    expect(groundedName({ plan_name: "Iterate on the sky" })).toBe("Iterate on the sky");
    expect(groundedName({ plan_name: "Stops", steps: [{ title: "Commit" }] })).toBe("Commit");
    expect(groundedName({ plan_name: "", steps: [{ title: "Loop ×3" }, { title: "Commit" }, { title: "Push" }] })).toBe("Loop ×3 + 2 more");
    expect(groundedName({ plan_name: "" })).toBe("Stops");
  });
});

describe("a landed chain's blocks come off", () => {
  it("removing one clears the finished chain (Delete and the menu's Remove)", async () => {
    const src = (await import("../components/flights/useSessionLine.tsx?raw")).default;
    expect(src).toContain("if (!flight && landedId) return clearLanded();");
    expect(src).toContain("landedId ? () => void clearLanded() : undefined");
    const sky = (await import("../components/flights/SkyLine.tsx?raw")).default;
    expect(sky).toContain("useChainMenu(gc, line.flight?.id || line.landedId");
  });
});

describe("waits and loops in seconds, minutes or hours", () => {
  it("durationWord and the stop summaries", async () => {
    const { durationWord, stopSummary } = await import("../lib/flightDiagram");
    expect(durationWord(45)).toBe("45 s");
    expect(durationWord(600)).toBe("10 min");
    expect(durationWord(90)).toBe("1 min 30 s");
    expect(durationWord(7200)).toBe("2 h");
    expect(stopSummary({ kind: "wait", config: { seconds: 30 } } as never)).toBe("30 s");
    expect(stopSummary({ kind: "wait", config: { minutes: 5 } } as never)).toBe("5 min");
    expect(stopSummary({ kind: "loop", config: { times: 2, every_seconds: 3600 } } as never)).toBe("2 runs · every 1 h");
  });
  it("the duration field shows the amount in the unit that fits, with a unit switch", async () => {
    const { createElement } = await import("react");
    const { renderToStaticMarkup } = await import("react-dom/server");
    const { DurationField } = await import("../components/flights/controls");
    const html = renderToStaticMarkup(createElement(DurationField, { label: "How long", value: 7200, onChange: () => {} }));
    expect(html).toContain('value="2"');
    expect(html).toContain("hours");
    expect(renderToStaticMarkup(createElement(DurationField, { label: "How long", value: 45, onChange: () => {} }))).toContain('value="45"');
  });
});
