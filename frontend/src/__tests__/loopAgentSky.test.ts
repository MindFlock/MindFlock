/** Round 21 — loop back to the agent, and easier loop wires
 * (docs/flights-loop-agent-sky.md). Pinned here: a Loop with `back_to:
 * "agent"` draws its return wire from the end of what it repeats into the
 * session's AGENT tile (the graph model, with parallel groups and sub-sessions
 * hung under the agent — never through a tile); a wire dropped on the own
 * agent tile is a Loop with `back_to: "agent"` right after the stop it left
 * (`wireIntent` → `runWireIntent`); the agent tile is a drop target; the
 * drag's hint; a stop's ⋯ "Loop back to…" (`loopChoices`, `withLoopItem`,
 * `loopMenuItems`) — Claude → Wait → ⋯ → Loop back to → The agent end to end;
 * and the Loop's dialog ("Back to: the agent", its prompt field). */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Instance } from "../api/types";
import type { Flight, FlightStep, StepKindDef, StepRunState } from "../lib/flights";
import type { Diagram, DiagramNode } from "../lib/flightDiagram";

const routes = vi.hoisted(() => ({
  tackOn: vi.fn(async (..._a: unknown[]) => true),
  patchStop: vi.fn(async (..._a: unknown[]) => true),
}));
const said = vi.hoisted(() => vi.fn());
vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})), instApi: vi.fn(async () => ({})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: said }));
vi.mock("../lib/flightsApi", async (orig) => ({ ...(await orig<typeof import("../lib/flightsApi")>()), ...routes }));

const D = await import("../lib/flightDiagram");
const S = await import("../lib/skyGraph");
const W = await import("../components/flights/useSkyWiring");
const M = await import("../components/flights/useChainMenu");
const SD = await import("../components/flights/StopDialog");
const { SkyWires } = await import("../components/flights/SkyWires");

afterEach(() => vi.clearAllMocks());

// --- Fixtures ------------------------------------------------------------------------

const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}, over: Partial<FlightStep> = {}): FlightStep => ({ id, kind, title, config, on_fail: "halt", ...over });

const LOOP_KIND: StepKindDef = {
  kind: "loop",
  label: "Loop",
  actor: "robot",
  summary: "Repeat from an earlier stop",
  family: "work",
  default_title: "Loop",
  fields: [
    { name: "back_to", type: "select", label: "Repeat from", options: [], dynamic: "stops_before", default: "" },
    { name: "times", type: "int", label: "Runs in all (0 = until stopped)", default: 3 },
    { name: "every_seconds", type: "duration", label: "At most every", default: 0 },
    { name: "until", type: "text", label: "Until", default: "" },
    { name: "prompt", type: "text", label: "Prompt to repeat", default: "", placeholder: "empty = the session's last prompt" },
  ],
};
const KINDS: StepKindDef[] = [
  { kind: "agent", label: "Agent", actor: "robot", summary: "", fields: [], default_title: "Agent works" },
  { kind: "wait", label: "Wait", actor: "robot", summary: "", fields: [], default_title: "Wait" },
  { kind: "command", label: "Command", actor: "robot", summary: "", fields: [], default_title: "Run" },
  LOOP_KIND,
];

const rec = (state: StepRunState["state"]): StepRunState => ({ state, since: 1, note: "", detail: {}, rounds: 0 });

function flight(steps: FlightStep[], cursor: number, step_states: Record<string, StepRunState> = {}, over: Partial<Flight> = {}): Flight {
  return {
    id: "f_1",
    plan_id: "p",
    plan_name: "Loop it",
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
  } as Flight;
}

const rowsOf = (f: Flight): Instance[] => [
  {
    title: "lead",
    status: "running",
    activity: "working",
    stage: "agent",
    task_headline: "",
    parent: "",
    branch: "lead",
    autopilot: null,
    flight: { id: f.id, plan_name: f.plan_name, cursor: f.cursor, total: f.steps.length, state: f.state, step: { id: f.steps[f.cursor]?.id, kind: f.steps[f.cursor]?.kind, title: f.steps[f.cursor]?.title, state: "running", actor: "robot" } },
  } as unknown as Instance,
];

const sessionLine = (f: Flight): Diagram => D.sessionDiagram(rowsOf(f), "lead", { flights: [f], kinds: KINDS });

function chainOf(d: Diagram, key = "lead") {
  const node = d.nodes.find((n) => n.kind === "session" && n.row === "main");
  const head = { kind: "session" as const, title: "Claude", sub: "", session: "lead", color: S.chainColor("lead"), node };
  return S.chainGraph(d, { head, key, flightId: d.flightId });
}

/** The owner's chain: Claude → Wait 5 h → a Loop back to the agent. */
const WAIT_LINE = (loop: Record<string, unknown> = { back_to: "agent", times: 0, every_seconds: 0 }) => [step("w1", "wait", "Wait 5 h", { seconds: 18000 }), step("l1", "loop", "Loop", loop)];

/** Sub-sessions hung under the agent (what SkyChain's `hangFromAgent` makes). */
function withKids(d: Diagram, n: number): Diagram {
  const me = d.nodes.find((x) => x.kind === "session")!;
  const first = d.nodes.find((x) => x.kind === "stop")!;
  const kids: DiagramNode[] = Array.from({ length: n }, (_, k) => ({
    id: "child:k" + k,
    kind: "child",
    row: "below",
    title: "k" + k,
    sub: "",
    state: "session",
    icon: "session",
    session: "k" + k,
    chip: { label: "Working", tone: "working" },
    branch: { from: me.id, to: first.id, state: "working", inner: "" },
  }));
  return { ...d, nodes: [...d.nodes, ...kids], edges: [...d.edges, ...kids.map((k) => ({ from: me.id, to: k.id, kind: "branch" as const }))] };
}

const at = (g: ReturnType<typeof chainOf>, id: string, side: "in" | "out" = "out") => ({ chain: g, node: id === S.HEAD_ID ? g.headNode! : g.nodes.find((x) => x.id === id)!, side });

/** Every box a wire must not cross: the graph's nodes but its two ends. */
const crossed = (g: ReturnType<typeof chainOf>, w: { from: string; to: string; mid: { y: number } }) => {
  const all = [g.headNode!, ...g.nodes];
  const a = all.find((n) => n.id === w.from)!;
  const b = all.find((n) => n.id === w.to)!;
  return S.loopHits(S.outPortOf(a), S.inPortOf(b), w.mid.y, all.filter((n) => n.id !== a.id && n.id !== b.id));
};

// --- The graph model ----------------------------------------------------------------

describe("a Loop back to the agent: the graph", () => {
  it("back_to: \"agent\" is a loop edge from the end of what it repeats into the session box", () => {
    const d = sessionLine(flight(WAIT_LINE(), 0, { w1: rec("running") }));
    const loops = d.edges.filter((e) => e.kind === "loop");
    const me = d.nodes.find((n) => n.kind === "session")!;
    expect(loops).toEqual([expect.objectContaining({ from: "stop:w1", to: me.id, kind: "loop" })]);
    expect(loops[0].via?.step?.id).toBe("l1");
    // The Loop rides its wire: no box.
    expect(d.nodes.some((n) => n.step?.id === "l1")).toBe(false);
  });

  it("the chain: a loop wire from the Wait's tile to the agent tile (head), the Loop as its `via`", () => {
    const g = chainOf(sessionLine(flight(WAIT_LINE(), 0, { w1: rec("running") })));
    const w = g.wires.find((x) => x.kind === "loop")!;
    expect(w).toMatchObject({ from: "stop:w1", to: S.HEAD_ID, kind: "loop" });
    expect(w.via?.step?.config?.back_to).toBe("agent");
    // Under the line, clear of the agent's own extras (its chip, Ready).
    expect(w.mid.y).toBeGreaterThan(S.NODE_H + 60);
    expect(crossed(g, w)).toBe(0);
  });

  it("first on the chain (nothing before it): round the agent tile itself", () => {
    const d = sessionLine(flight([step("l1", "loop", "Loop", { back_to: "agent" })], 0, { l1: rec("running") }));
    const me = d.nodes.find((n) => n.kind === "session")!;
    expect(d.edges.filter((e) => e.kind === "loop")).toEqual([expect.objectContaining({ from: me.id, to: me.id })]);
  });

  it("a chain with no session: an agent loop draws no wire (it stays a box)", () => {
    const d = D.flightDiagram(flight(WAIT_LINE(), 0, {}, { state: "grounded", session: "" }), KINDS);
    expect(d.edges.some((e) => e.kind === "loop")).toBe(false);
    expect(d.nodes.some((n) => n.step?.id === "l1")).toBe(true);
  });

  it("sub-sessions hung under the agent: the wire goes OVER the tiles, never through one", () => {
    const g = chainOf(withKids(sessionLine(flight([step("a1", "agent", "Fix"), ...WAIT_LINE()], 0, { a1: rec("running") })), 3));
    const w = g.wires.find((x) => x.kind === "loop")!;
    expect(w).toMatchObject({ from: "stop:w1", to: S.HEAD_ID });
    expect(w.mid.y).toBeLessThan(0);
    expect(crossed(g, w)).toBe(0);
    // The chain's box takes the wire in (the frame grows up to it).
    expect(g.bbox.y).toBeLessThanOrEqual(w.mid.y - 12);
    // A stop→stop loop on the same chain still runs under the line.
    const g2 = chainOf(withKids(sessionLine(flight([step("a1", "agent", "Fix"), step("w1", "wait", "Wait"), step("l1", "loop", "Loop", { back_to: "a1" })], 0, { a1: rec("running") })), 3));
    const w2 = g2.wires.find((x) => x.kind === "loop")!;
    expect(w2.mid.y).toBeGreaterThan(S.NODE_H);
    expect(crossed(g2, w2)).toBe(0);
  });

  it("a parallel group before it: the wire leaves the group's last member, clear of every tile", () => {
    const par = { id: "p1" };
    const steps = [step("a1", "agent", "Fix"), step("c1", "command", "Lint", {}, { parallel: par }), step("c2", "command", "Test", {}, { parallel: par }), ...WAIT_LINE()];
    const g = chainOf(sessionLine(flight(steps, 0, { a1: rec("running") })));
    const w = g.wires.find((x) => x.kind === "loop")!;
    expect(w).toMatchObject({ from: "stop:w1", to: S.HEAD_ID });
    expect(crossed(g, w)).toBe(0);
    // The Loop after the group itself: from its last (lowest) member.
    const g2 = chainOf(sessionLine(flight([steps[0], steps[1], steps[2], step("l1", "loop", "Loop", { back_to: "agent" })], 0, { a1: rec("running") })));
    const w2 = g2.wires.find((x) => x.kind === "loop")!;
    expect(w2).toMatchObject({ from: "stop:c2", to: S.HEAD_ID });
    expect(crossed(g2, w2)).toBe(0);
  });

  it("moved tiles re-route the agent wire too (moveNode)", () => {
    const g = chainOf(sessionLine(flight([step("a1", "agent", "Fix"), ...WAIT_LINE()], 0, { a1: rec("running") })));
    const moved = S.moveNode(g, "stop:w1", { x: 600, y: 40 });
    const w = moved.wires.find((x) => x.kind === "loop")!;
    expect(w).toMatchObject({ from: "stop:w1", to: S.HEAD_ID });
    expect(w.d.startsWith("M " + S.outPortOf(moved.nodes.find((n) => n.id === "stop:w1")!).x)).toBe(true);
  });
});

// --- Wiring --------------------------------------------------------------------------

describe("a wire dropped on the agent tile", () => {
  const g = () => chainOf(sessionLine(flight([step("a1", "agent", "Fix"), step("w1", "wait", "Wait 5 h")], 0, { a1: rec("running") })));

  it("wireIntent: a Loop after the stop the wire left, back to the agent", () => {
    const c = g();
    expect(S.wireIntent(at(c, "stop:w1"), at(c, S.HEAD_ID))).toEqual({ op: "loop", chain: "lead", from: c.nodes.find((n) => n.id === "stop:w1")!.node, to: c.headNode!.node, agent: true });
    // The head's own out port on itself: nothing.
    expect(S.wireIntent(at(c, S.HEAD_ID), at(c, S.HEAD_ID)).op).toBe("none");
  });

  it("after a parallel group: right after the whole group", () => {
    const par = { id: "p1" };
    const c = chainOf(sessionLine(flight([step("a1", "agent", "Fix"), step("c1", "command", "Lint", {}, { parallel: par }), step("c2", "command", "Test", {}, { parallel: par })], 0, { a1: rec("running") })));
    const it = S.wireIntent(at(c, "stop:c1"), at(c, S.HEAD_ID));
    expect(it).toMatchObject({ op: "loop", agent: true });
    expect(it.op === "loop" && it.from.step?.id).toBe("c2");
  });

  it("the agent tile is lit as a drop target (wireTargetsOf), alongside every earlier stop", () => {
    const c = g();
    const lit = W.wireTargetsOf(at(c, "stop:w1"), [{ graph: c }]);
    expect([...lit.get("lead")!]).toContain(S.HEAD_ID);
    expect([...lit.get("lead")!]).toEqual(expect.arrayContaining(["stop:a1", "stop:w1"]));
  });

  it("runWireIntent: tacks on a Loop with back_to \"agent\" right after that stop", async () => {
    const c = g();
    const f = flight([step("a1", "agent", "Fix"), step("w1", "wait", "Wait 5 h"), step("p1", "command", "Push")], 1);
    const intent = S.wireIntent(at(c, "stop:a1"), at(c, S.HEAD_ID));
    await W.runWireIntent(intent, { src: at(c, "stop:a1"), at: { x: 0, y: 0 } }, { flightOf: () => f, flights: [f], kinds: KINDS, chain: () => null, merge: vi.fn(), pinAt: vi.fn(), labelH: 30 });
    const [fid, st, where] = routes.tackOn.mock.calls[0] as [string, FlightStep, number | undefined];
    expect(fid).toBe("f_1");
    expect(st).toMatchObject({ kind: "loop", config: { back_to: "agent" } });
    expect(where).toBe(1);
    expect(said.mock.calls[0][0]).toMatch(/back to the agent/);
  });

  it("loopStepTo / loopBackTo speak the contract's literal", () => {
    expect(D.LOOP_AGENT).toBe("agent");
    expect(D.loopStepTo(LOOP_KIND, [], D.LOOP_AGENT).config.back_to).toBe("agent");
    expect(D.loopBackTo({ kind: "session" })).toBe("agent");
    expect(D.loopBackTo({ kind: "stop", step: step("x", "agent", "X") })).toBe("x");
    expect(D.loopsToAgent(step("l", "loop", "L", { back_to: "agent" }))).toBe(true);
    expect(D.loopsToAgent(step("l", "loop", "L", { back_to: "x" }))).toBe(false);
  });

  it("the hint by the pointer: where a loop can go, then what the drop does", () => {
    const c = g();
    const src = at(c, "stop:w1");
    expect(W.dragHint(src, W.wireTargetsOf(src, [{ graph: c }]))).toBe("Drop on an earlier stop or the agent to loop");
    expect(W.intentHint(S.wireIntent(src, at(c, S.HEAD_ID)))).toBe("↺ Loop back to the agent");
    expect(W.intentHint(S.wireIntent(src, at(c, "stop:a1")))).toBe("↺ Loop back to “Fix”");
    // An in port: a split.
    expect(W.dragHint(at(c, "stop:w1", "in"), new Map())).toMatch(/split/);
    // The band draws it by the pointer.
    const html = renderToStaticMarkup(createElement(SkyWires, { band: { from: { x: 0, y: 0 }, to: { x: 40, y: 50 }, side: "out", over: true, hint: "↺ Loop back to the agent" }, wires: [] }));
    expect(html).toContain("sky-band-hint");
    expect(html).toContain("↺ Loop back to the agent");
  });
});

// --- The ⋯ menu ----------------------------------------------------------------------

describe("a stop's ⋯ → Loop back to…", () => {
  it("Claude → Wait → ⋯: the agent first, then every stop at or before it", () => {
    const f = flight(WAIT_LINE().slice(0, 1), 0, { w1: rec("running") });
    const d = sessionLine(f);
    const wait = d.nodes.find((n) => n.step?.id === "w1")!;
    const me = d.nodes.find((n) => n.kind === "session")!;
    const choices = D.loopChoices(wait, d.nodes.filter((n) => n.row === "main"), { session: me, insertFrom: d.insertFrom });
    expect(choices.map((c) => c.label)).toEqual(["The agent (run it again)", "This stop (“Wait 5 h”)"]);
    expect(choices[0]).toMatchObject({ agent: true, to: me, from: wait });
    // The menu's line and its submenu.
    const items = M.withLoopItem([{ key: "edit", label: "Edit", title: "" }, { key: "remove", label: "Remove", title: "", danger: true }], choices);
    expect(items.map((i) => i.label)).toEqual(["Edit", "Loop back to…", "Remove"]);
    const sub = M.loopMenuItems(choices);
    expect(sub.map((i) => i.label)).toEqual(["Loop back to…", "The agent (run it again)", "This stop (“Wait 5 h”)", "← Back"]);
    expect(sub[0].note).toBe(true);
  });

  it("end to end: picking The agent tacks on a Loop with back_to \"agent\" right after the Wait", async () => {
    // What useSessionLine's onLoopFrom does with the choice (the line's own
    // handler, shared with the wire): the same as runWireIntent's loop.
    const f = flight([step("w1", "wait", "Wait 5 h")], 0, { w1: rec("running") });
    const d = sessionLine(f);
    const wait = d.nodes.find((n) => n.step?.id === "w1")!;
    const me = d.nodes.find((n) => n.kind === "session")!;
    const [agent] = D.loopChoices(wait, d.nodes, { session: me, insertFrom: d.insertFrom });
    const c = chainOf(d);
    await W.runWireIntent({ op: "loop", chain: "lead", from: agent.from, to: agent.to, agent: agent.agent }, { src: at(c, "stop:w1"), at: { x: 0, y: 0 } }, { flightOf: () => f, flights: [f], kinds: KINDS, chain: () => null, merge: vi.fn(), pinAt: vi.fn(), labelH: 30 });
    const [, st, where] = routes.tackOn.mock.calls[0] as [string, FlightStep, number | undefined];
    expect(st.config.back_to).toBe("agent");
    expect(where).toBeUndefined(); // the end of the chain (after the Wait)
    // …and once the flight has it, the chain draws the wire into the agent.
    const after = chainOf(sessionLine(flight([step("w1", "wait", "Wait 5 h"), { ...st, id: "l1" }], 0, { w1: rec("running") })));
    expect(after.wires.find((w) => w.kind === "loop")).toMatchObject({ from: "stop:w1", to: S.HEAD_ID });
  });

  it("nothing to offer: a Loop itself, a stop behind the flight, a line with no session", () => {
    const f = flight([step("a1", "agent", "Fix"), step("w1", "wait", "Wait")], 1, { a1: rec("done"), w1: rec("running") });
    const d = sessionLine(f);
    const fix = d.nodes.find((n) => n.step?.id === "a1")!;
    // A Loop after the flown "Fix" would sit behind the flight.
    expect(D.loopChoices(fix, d.nodes, { insertFrom: d.insertFrom })).toEqual([]);
    expect(D.loopChoices({ ...fix, step: step("l", "loop", "L") }, d.nodes)).toEqual([]);
    // No session box: only stops.
    const wait = d.nodes.find((n) => n.step?.id === "w1")!;
    expect(D.loopChoices(wait, d.nodes, { insertFrom: d.insertFrom }).some((c) => c.agent)).toBe(false);
    expect(M.withLoopItem([{ key: "edit", label: "Edit", title: "" }], [])).toHaveLength(1);
  });

  it("a stops-only chain's menu (no line hook): earlier stops from the steps, no agent", () => {
    const steps = [step("a1", "agent", "Fix"), step("c1", "command", "Test")];
    const n = { id: "stop:c1", kind: "stop", row: "main", title: "Test", sub: "", state: "edit", icon: "command", step: steps[1], index: 1, editable: true } as DiagramNode;
    expect(M.stepLoopChoices(n, steps).map((c) => c.label)).toEqual(["“Fix”", "This stop (“Test”)"]);
    expect(M.stepLoopChoices({ ...n, editable: false }, steps)).toEqual([]);
  });

  it("the pill re-aim and the menu are wired in the source (agent tile as a target)", async () => {
    const { code } = await import("./sourceText");
    const menu = code("components/flights/useChainMenu.tsx");
    expect(menu).toMatch(/graph\.head\.kind === "session" \? graph\.headNode : null/);
    const line = code("components/flights/useSessionLine.tsx");
    expect(line).toMatch(/to\.kind === "session" \? LOOP_AGENT/);
    expect(line).toMatch(/graphCanvas: \{ \.\.\.raw, loopChoicesOf \}/);
  });
});

// --- The Loop's dialog ---------------------------------------------------------------

describe("the Loop's dialog when it goes back to the agent", () => {
  const node = (config: Record<string, unknown>): DiagramNode => ({ id: "stop:l1", kind: "stop", row: "main", title: "Loop", sub: "", state: "edit", icon: "loop", step: step("l1", "loop", "Loop", config), index: 1, editable: true });

  it("its form leaves back_to and prompt to the dialog", () => {
    expect(SD.loopFormDef(LOOP_KIND, { kind: "loop", config: { back_to: "agent" } })!.fields.map((f) => f.name)).toEqual(["times", "every_seconds", "until"]);
    expect(SD.loopFormDef(LOOP_KIND, { kind: "loop", config: { back_to: "" } })).toBe(LOOP_KIND);
  });

  it("says “Back to: the agent” and offers the prompt with its hint", () => {
    const html = renderToStaticMarkup(createElement(SD.StopDialog, { node: node({ back_to: "agent", prompt: "" }), def: LOOP_KIND, lineSteps: [step("w1", "wait", "Wait"), step("l1", "loop", "Loop")], portal: false, loopAgent: true, onClose: () => {}, onSave: () => {} }));
    expect(html).toContain("Back to");
    expect(html).toContain("the agent");
    expect(html).toContain("Empty = send the same prompt again");
    expect(html).toContain('aria-label="Prompt for the agent each run"');
    // The stops list ("Repeat from") is not drawn.
    expect(html).not.toContain('data-dynamic="stops_before"');
  });

  it("a Loop aimed at a stop offers the agent instead", () => {
    const html = renderToStaticMarkup(createElement(SD.StopDialog, { node: node({ back_to: "" }), def: LOOP_KIND, lineSteps: [step("w1", "wait", "Wait"), step("l1", "loop", "Loop")], portal: false, loopAgent: true, onClose: () => {}, onSave: () => {} }));
    expect(html).toContain("Loop back to the agent instead");
    expect(html).toContain('data-dynamic="stops_before"');
  });
});
