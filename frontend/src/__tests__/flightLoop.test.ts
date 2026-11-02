/** The Loop on the line (docs/flights.md "Round 16", redrawn after it): a
 * Loop draws NO box — it is a return wire from the last stop it repeats back
 * under the line into the first one, a flowchart's loop-back. Pinned here: the
 * wire's edge (`loopEdges` / `liftLoops` through flywayDiagram / flightDiagram
 * / sessionDiagram, re-aimed by a collapsed group and the flown pill), its
 * layout (under the line), the pill (count, idle text, it opens the Loop),
 * the bird while the flight is at the Loop, and the dialog's dynamic "Repeat
 * from" options (`stopsBefore`). */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Instance } from "../api/types";
import type { Flight, FlightStep, StepKindDef, StepRunState } from "../lib/flights";
import { stopsBefore } from "../lib/flights";
import {
  FLOWN_ID,
  collapseFlown,
  flightDiagram,
  flywayDiagram,
  layoutDiagram,
  loopEdges,
  loopLine,
  loopMark,
  loopPill,
  loopPillTitle,
  loopRise,
  minutesWord,
  sessionDiagram,
  stopSummary,
  type Diagram,
  LOOP_FROM,
  LOOP_TO,
  LOOP_DROP,
  LOOP_PILL_ROOM,
  birdPerch,
  lineWhere,
  liftLoops,
  loopIdlePill,
} from "../lib/flightDiagram";
import { FlywayCanvas } from "../components/flights/FlywayCanvas";

const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}, over: Partial<FlightStep> = {}): FlightStep => ({
  id,
  kind,
  title,
  config,
  on_fail: "halt",
  ...over,
});

/** The kinds dto's Loop row, as the backend contract writes it. */
export const LOOP_KIND: StepKindDef = {
  kind: "loop",
  label: "Loop",
  actor: "robot",
  summary: "Repeat from an earlier stop",
  family: "work",
  default_on_fail: "halt",
  default_title: "Loop ×3",
  fields: [
    { name: "back_to", type: "select", label: "Repeat from", options: [], dynamic: "stops_before", default: "" },
    { name: "times", type: "int", label: "Runs in all (0 = until stopped)", default: 3 },
    { name: "every_minutes", type: "int", label: "At most every (minutes)", default: 0 },
    { name: "wait_limits", type: "bool", label: "Wait out usage limits", default: true },
    { name: "fresh", type: "bool", label: "Clear the agent's context before each run", default: false },
    { name: "until", type: "text", label: "Until", placeholder: "e.g. checks green" },
  ],
};

const KINDS: StepKindDef[] = [
  { kind: "agent", label: "Agent", actor: "robot", summary: "", fields: [], default_title: "Agent works" },
  { kind: "ship", label: "Ship", actor: "robot", summary: "", fields: [], default_title: "Push" },
  { kind: "command", label: "Command", actor: "robot", summary: "", fields: [], default_title: "Run" },
  LOOP_KIND,
];

const LINE = (loop: Record<string, unknown> = {}): FlightStep[] => [
  step("s1", "agent", "Improve the sky"),
  step("s2", "command", "Screenshot", { command: "npm run shots" }),
  step("s3", "agent", "Judge it"),
  step("s4", "loop", "Loop ×3", { back_to: "", times: 3, ...loop }),
  step("s5", "ship", "Push", { lane: "push" }),
];

const rec = (state: StepRunState["state"], detail: Record<string, unknown> = {}, note = ""): StepRunState => ({ state, since: 1, note, detail, rounds: 0 });

function flight(steps: FlightStep[], cursor: number, step_states: Record<string, StepRunState> = {}, over: Partial<Flight> = {}): Flight {
  return {
    id: "f_1",
    plan_id: "p",
    plan_name: "Iterate",
    steps,
    subject: { kind: "manual", source: "", id: "", title: "", url: "" },
    session: "lead",
    repo_root: "/r",
    branch: "b",
    state: "running",
    cursor,
    step_states,
    reason: "",
    history: [],
    started: 1,
    updated: 1,
    finished: 0,
    by: "",
    ...over,
  };
}

const loops = (d: Diagram) => d.edges.filter((e) => e.kind === "loop");
const viaOf = (d: Diagram, id: string) => loops(d).find((e) => e.via?.id === id)!.via!;

describe("the return wire's edge", () => {
  it("a flyway: from the last repeated stop back to the first stop for back_to \"\"; the Loop is no box", () => {
    const d = flywayDiagram(LINE(), KINDS);
    expect(loops(d)).toEqual([
      { from: "stop:s3", to: "stop:s1", kind: "loop", loop: { runs: 0, times: 3, state: "pending" }, via: expect.objectContaining({ id: "stop:s4", index: 3, editable: true }) },
    ]);
    // The line runs straight past where the Loop is.
    expect(d.nodes.map((n) => n.id)).toEqual(["stop:s1", "stop:s2", "stop:s3", "stop:s5", "add"]);
    expect(d.edges.filter((e) => e.kind === "line").map((e) => e.to)).toEqual(["stop:s2", "stop:s3", "stop:s5"]);
  });

  it("liftLoops keeps a Loop with nothing to repeat on the line as a box", () => {
    const { line, loops: l } = liftLoops(flywayDiagram([step("l", "loop", "Loop", {}), step("a", "agent", "A")], KINDS).nodes);
    expect(l).toEqual([]);
    expect(line.map((n) => n.id)).toContain("stop:l");
  });

  it("a one-stop loop goes round that stop", () => {
    const d = flywayDiagram([step("a", "agent", "A"), step("l", "loop", "Loop", { back_to: "a" })], KINDS);
    expect(loops(d)).toEqual([expect.objectContaining({ from: "stop:a", to: "stop:a" })]);
  });

  it("back_to names a stop: the arc lands on it", () => {
    expect(loops(flywayDiagram(LINE({ back_to: "s3" }), KINDS))[0].to).toBe("stop:s3");
  });

  it("no arc when back_to is missing, or not before the loop", () => {
    expect(loops(flywayDiagram(LINE({ back_to: "gone" }), KINDS))).toEqual([]);
    expect(loops(flywayDiagram(LINE({ back_to: "s5" }), KINDS))).toEqual([]);
    // With no wire to stand for it, the Loop stays a box.
    expect(flywayDiagram(LINE({ back_to: "gone" }), KINDS).nodes.some((n) => n.id === "stop:s4")).toBe(true);
    // A Loop first on the line has nothing to repeat.
    expect(loopEdges(flywayDiagram([step("l", "loop", "Loop", {})], KINDS).nodes)).toEqual([]);
  });

  it("a live flight: the edge carries the loop's count", () => {
    const f = flight(LINE(), 1, { s1: rec("done"), s2: rec("running"), s4: rec("running", { runs: 1, last_start: 5 }) });
    const [e] = loops(flightDiagram(f, KINDS));
    expect(e).toMatchObject({ from: "stop:s3", to: "stop:s1", loop: { runs: 1, times: 3, state: "running" } });
  });

  it("inside a group, \"\" is the group's first sub-step", () => {
    const g = { id: "g1", name: "Polish", plan_id: "" };
    const steps = [step("s1", "agent", "Plan"), step("s2", "agent", "Edit", {}, { group: g }), step("s3", "command", "Shots", {}, { group: g }), step("s4", "loop", "Loop", { back_to: "" }, { group: g })];
    expect(loops(flywayDiagram(steps, KINDS))[0].to).toBe("stop:s2");
  });

  it("a collapsed group: the wire's ends fold into the group box; one inside it goes round the box", () => {
    const g = { id: "g1", name: "Polish", plan_id: "" };
    const steps = [step("s1", "agent", "Plan"), step("s2", "agent", "Edit", {}, { group: g }), step("s3", "command", "Shots", {}, { group: g }), step("s4", "loop", "Loop", { back_to: "s2" }), step("s5", "ship", "Push")];
    const f = flight(steps, 0, { s1: rec("running") });
    const rows = [{ title: "lead", status: "running", activity: "working", stage: "agent", task_headline: "", parent: "", branch: "lead", autopilot: null, flight: { id: "f_1", plan_name: "Iterate", cursor: 0, total: steps.length, state: "running", step: { id: "s1", kind: "agent", title: "Plan", state: "running", actor: "robot" } } } as unknown as Instance];
    const d = sessionDiagram(rows, "lead", { flights: [f], kinds: KINDS });
    // Its last repeated stop (s3) and its target (s2) both sit in the group.
    expect(loops(d)).toEqual([expect.objectContaining({ from: "group:g1", to: "group:g1" })]);
    // Open, the wire runs between the sub-steps themselves.
    const open = sessionDiagram(rows, "lead", { flights: [f], kinds: KINDS, expandedGroups: new Set(["g1"]) });
    expect(loops(open)[0]).toMatchObject({ from: "stop:s3", to: "stop:s2" });
    // A loop inside a group whose only other member is its target: the group
    // is one stop then (a lone member draws as itself) — round that stop.
    const inner = [step("s1", "agent", "Plan"), step("s2", "agent", "Edit", {}, { group: g }), step("s3", "loop", "Loop", { back_to: "s2" }, { group: g }), step("s4", "ship", "Push")];
    const d2 = sessionDiagram(rows, "lead", { flights: [flight(inner, 0, { s1: rec("running") })], kinds: KINDS });
    expect(loops(d2)).toEqual([expect.objectContaining({ from: "stop:s2", to: "stop:s2" })]);
  });

  it("a loop folded whole into the flown pill goes round the pill", () => {
    const steps = [step("s1", "agent", "A"), step("s2", "agent", "B"), step("s3", "agent", "C"), step("s4", "loop", "Loop", { back_to: "s1" })];
    const f = flight(steps, 3, { s1: rec("done"), s2: rec("done"), s3: rec("done"), s4: rec("running") });
    const d = collapseFlown(flightDiagram(f, KINDS));
    expect(d.nodes.some((n) => n.id === FLOWN_ID)).toBe(true);
    expect(loops(d)).toEqual([expect.objectContaining({ from: FLOWN_ID, to: FLOWN_ID })]);
  });
});

describe("the Loop's count, the pill and the box's line", () => {
  it("loopMark: runs from detail, times from config (default 3), the state from the record", () => {
    expect(loopMark({ config: {} })).toEqual({ runs: 0, times: 3, state: "pending" });
    expect(loopMark({ config: { times: 5 } }, rec("running", { runs: 2 }))).toEqual({ runs: 2, times: 5, state: "running" });
    expect(loopMark({ config: { times: 0 } }, rec("pending", { runs: 1 }))).toMatchObject({ times: 0, state: "running" });
    expect(loopMark({ config: { times: 3 } }, rec("done", { runs: 2 }))).toMatchObject({ state: "done" });
  });

  it("the pill: runs+1 / times, ∞ when it loops until stopped, nothing before it is reached", () => {
    expect(loopPill({ runs: 0, times: 3, state: "pending" })).toBe("");
    expect(loopPill({ runs: 0, times: 3, state: "running" })).toBe("1 / 3");
    expect(loopPill({ runs: 1, times: 3, state: "running" })).toBe("2 / 3");
    expect(loopPill({ runs: 1, times: 0, state: "running" })).toBe("2 / ∞");
    expect(loopPill({ runs: 2, times: 3, state: "done" })).toBe("3 / 3");
    expect(loopPillTitle({ runs: 1, times: 3, state: "running" })).toBe("Run 2 of 3");
    expect(loopPillTitle({ runs: 1, times: 0, state: "running" })).toBe("Run 2 — it loops until stopped");
  });

  it("before it runs the pill says what it will do", () => {
    expect(loopIdlePill({ runs: 0, times: 3, state: "pending" })).toBe("↻ ×3");
    expect(loopIdlePill({ runs: 0, times: 0, state: "pending" })).toBe("↻ until stopped");
    expect(loopIdlePill({ runs: 0, times: 0, state: "pending" }, { step: step("l", "loop", "L", { until: "checks green" }) })).toBe("↻ until checks green");
  });

  it("the box's second line: the judge's note, else the counter", () => {
    const s = { config: { times: 3 } };
    expect(loopLine(s, rec("running", { runs: 1 }))).toBe("run 2 of 3");
    expect(loopLine(s, rec("running", { runs: 1 }, "next run in 12 min"))).toBe("run 2 of 3 · next in 12 min");
    expect(loopLine(s, rec("running", { runs: 1 }, "waiting on the usage limit"))).toBe("waiting on the limit");
    expect(loopLine({ config: { times: 0, until: "checks green" } }, rec("running", { runs: 1 }))).toBe("run 2 — until checks green");
    expect(loopLine({ config: { times: 0 } }, rec("running", { runs: 4 }))).toBe("run 5");
  });

  it("a flight back at an earlier stop: the Loop waits (the bird is back there) and reads its run", () => {
    const f = flight(LINE(), 0, { s1: rec("running"), s4: rec("running", { runs: 1 }, "") });
    const d = flightDiagram(f, KINDS);
    const via = viaOf(d, "stop:s4");
    expect(via.state).toBe("pending");
    expect(via.sub).toBe("run 2 of 3");
    expect(birdPerch(d)).toEqual({ nodeId: "stop:s1", tone: "working" });
    expect(d.nodes.filter((n) => n.state === "current").map((n) => n.id)).toEqual(["stop:s1"]);
  });

  it("at the loop, waiting: the note in its line; still ahead: its config summary", () => {
    const at = flightDiagram(flight(LINE({ every_minutes: 30 }), 3, { s4: rec("running", { runs: 1 }, "next run in 12 min") }), KINDS);
    expect(viaOf(at, "stop:s4")).toMatchObject({ state: "current", sub: "run 2 of 3 · next in 12 min" });
    // The bird stands where the wire leaves (the Loop has no box); the bar names the Loop.
    expect(birdPerch(at)).toEqual({ nodeId: "stop:s3", tone: "working" });
    expect(lineWhere(at)).toBe("at Loop ×3 · run 2 of 3 · next in 12 min");
    const ahead = flightDiagram(flight(LINE({ every_minutes: 30 }), 1, { s2: rec("running") }), KINDS);
    expect(viaOf(ahead, "stop:s4").sub).toBe("3 runs · every 30 min");
  });

  it("stopSummary of a loop", () => {
    expect(stopSummary({ kind: "loop", config: {} })).toBe("3 runs");
    expect(stopSummary({ kind: "loop", config: { times: 0 } })).toBe("until stopped");
    expect(stopSummary({ kind: "loop", config: { times: 0, until: "checks green" } })).toBe("until checks green");
    expect(stopSummary({ kind: "loop", config: { times: 1, every_minutes: 90, fresh: true } })).toBe("1 run · every 1 h 30 min · fresh context");
    expect(minutesWord(30)).toBe("30 min");
    expect(minutesWord(120)).toBe("2 h");
    expect(minutesWord(2880)).toBe("2 d");
  });
});

describe("the wire's layout", () => {
  it("horizontal: down out of the last repeated box, back under the line, up into the first (arrowhead on it)", () => {
    const L = layoutDiagram(flywayDiagram(LINE(), KINDS));
    const w = L.wires.find((x) => x.kind === "loop")!;
    const a = L.boxes.find((b) => b.node.id === "stop:s3")!;
    const b = L.boxes.find((b) => b.node.id === "stop:s1")!;
    expect(w.d.startsWith(`M ${a.x + a.w * LOOP_FROM} ${a.y + a.h} V `)).toBe(true);
    expect(w.d.endsWith(`V ${b.y + b.h + 2}`)).toBe(true);
    expect(w.d).toContain(` H ${b.x + b.w * LOOP_TO + 10} `);
    // Its pill sits in the middle of the run under the line.
    expect(w.apex!.x).toBeCloseTo((a.x + a.w * LOOP_FROM + b.x + b.w * LOOP_TO) / 2);
    expect(w.apex!.y).toBe(Math.max(...L.boxes.map((p) => p.y + p.h)) + LOOP_DROP);
    expect(w.loop).toEqual({ runs: 0, times: 3, state: "pending" });
    expect(w.via?.id).toBe("stop:s4");
    // The stage grows down for it, not up.
    expect(L.height).toBe(w.apex!.y + LOOP_PILL_ROOM);
    expect(L.rise).toBe(0);
  });

  it("the phone's reach grows with the span, clamped 30–84", () => {
    expect(loopRise(10)).toBe(30);
    expect(loopRise(300)).toBe(66);
    expect(loopRise(3000)).toBe(84);
  });

  it("no loop: no rise", () => {
    expect(layoutDiagram(flywayDiagram(LINE().filter((s) => s.kind !== "loop"), KINDS)).rise).toBe(0);
  });

  it("vertical: out of the last repeated box's right edge and round the column", () => {
    const L = layoutDiagram(flywayDiagram(LINE(), KINDS), { vertical: true });
    const w = L.wires.find((x) => x.kind === "loop")!;
    const a = L.boxes.find((b) => b.node.id === "stop:s3")!;
    expect(w.d.startsWith(`M ${a.x + a.w} `)).toBe(true);
    expect(w.apex!.x).toBeGreaterThan(a.x + a.w);
    expect(L.width).toBeGreaterThanOrEqual(w.apex!.x);
  });
});

describe("the canvas draws the wire", () => {
  const render = (d: Diagram) => renderToStaticMarkup(createElement(FlywayCanvas, { diagram: d, label: "Lead", vertical: false }));

  it("pending: a dashed wire under the line with an arrowhead, its pill saying what it will do, a button for the Loop", () => {
    const html = render(flywayDiagram(LINE(), KINDS));
    expect(html).toMatch(/<path class="fl-wire fl-wire-loop fl-edge-loop fl-loop-pending" d="M [^"]+ V [^"]+ Q [^"]+" marker-end="url\(#[^)]+-arrow\)"/);
    expect(html).toMatch(/<span class="fl-loop-pill fl-loop-pending fl-st-edit" style="[^"]+" data-node="stop:s4" title="Loop: 3 runs — click to change it" aria-label="[^"]+" role="button" tabindex="0">↻ ×3<\/span>/);
    // No Loop box.
    expect(html).not.toMatch(/class="fl-box[^"]*" data-node="stop:s4"/);
  });

  it("running: solid, the count on the pill, counter-scaled with --sky-z", () => {
    const f = flight(LINE(), 0, { s1: rec("running"), s4: rec("running", { runs: 1 }) });
    const html = render(flightDiagram(f, KINDS));
    expect(html).toContain("fl-wire-loop fl-edge-loop fl-loop-running");
    expect(html).toMatch(/<span class="fl-loop-pill fl-loop-running[^"]*" style="transform:translate\([-\d.]+px, [-\d.]+px\) translate\(-50%, -50%\) scale\(calc\(1 \/ var\(--sky-z, 1\)\)\)" data-node="stop:s4" title="Run 2 of 3 — click to change it"[^>]*>2 \/ 3<\/span>/);
  });

  it("the stage is tall enough for the wire under the line", () => {
    const d = flywayDiagram(LINE(), KINDS);
    const L = layoutDiagram(d);
    const html = render(d);
    const height = Number(/<div class="fl-stage" style="width:[\d.]+px;height:([\d.]+)px"/.exec(html)![1]);
    expect(height).toBeGreaterThanOrEqual(L.height);
  });
});

describe("the dialog's dynamic options (stopsBefore)", () => {
  it("\"the first stop\", then every stop before the loop in flight order, by title", () => {
    expect(stopsBefore(LINE(), { id: "s4" })).toEqual([
      { value: "", label: "the first stop" },
      { value: "s1", label: "Improve the sky" },
      { value: "s2", label: "Screenshot" },
      { value: "s3", label: "Judge it" },
    ]);
  });

  it("a group's sub-steps carry its name; inside a group \"\" is its first stop", () => {
    const g = { id: "g1", name: "Polish", plan_id: "" };
    const steps = [step("s1", "agent", "Plan"), step("s2", "agent", "Edit", {}, { group: g }), step("s3", "loop", "Loop", {}, { group: g })];
    expect(stopsBefore(steps, steps[2])).toEqual([
      { value: "", label: "the first stop of Polish" },
      { value: "s1", label: "Plan" },
      { value: "s2", label: "Edit", group: "Polish" },
    ]);
  });

  it("a stop not on the line yet sees the whole line; no line: only the first stop", () => {
    expect(stopsBefore(LINE(), { id: "new" }).length).toBe(6);
    expect(stopsBefore(null, { id: "x" })).toEqual([{ value: "", label: "the first stop" }]);
  });
});
