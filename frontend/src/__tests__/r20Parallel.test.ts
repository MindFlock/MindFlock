/** Round 20 B — parallel stops, the graph model (docs/flights.md "Round 20 …
 * B. Parallel stops", docs/flights-r20-parallel.md): a step's `parallel`
 * group (lib/flights), the routes that make / leave / join one
 * (lib/flightsApi), the fork and join on the diagram (lib/flightDiagram),
 * the column, wires and + on the Sky's chain plus the `parallel` wire intent
 * (lib/skyGraph), and what a dropped `parallel` wire does (useSkyWiring). */

/// <reference types="vite/client" />
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Flight, FlightStep, StepKindDef, StepRunState } from "../lib/flights";
import type { DiagramNode } from "../lib/flightDiagram";
import type { ChainGraph, ChainHead, GraphNode } from "../lib/skyGraph";

const api = vi.fn(async (..._args: unknown[]) => ({}));
const said = vi.hoisted(() => vi.fn());
vi.mock("../api/client", () => ({ api: (...a: unknown[]) => api(...a), instApi: vi.fn(async () => ({})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: said }));
vi.mock("../lib/errorPop", () => ({ errorPop: vi.fn() }));

const F = await import("../lib/flights");
const A = await import("../lib/flightsApi");
const D = await import("../lib/flightDiagram");
const G = await import("../lib/skyGraph");
const W = await import("../components/flights/useSkyWiring");

afterEach(() => {
  vi.clearAllMocks();
});

// --- Fixtures ------------------------------------------------------------------------

const P = { id: "p_abc123" };
const step = (id: string, kind: string, title: string, more: Partial<FlightStep> = {}): FlightStep => ({ id, kind, title, config: {}, on_fail: "halt", ...more });
const par = (s: FlightStep, id = P.id): FlightStep => ({ ...s, parallel: { id } });
const rec = (state: StepRunState["state"]): StepRunState => ({ state, since: 1, note: "", detail: {}, rounds: 0 });
const flight = (steps: FlightStep[], cursor: number, states: Record<string, StepRunState> = {}, over: Partial<Flight> = {}): Flight =>
  ({
    id: "f_p",
    plan_id: "adhoc",
    plan_name: "Ship",
    steps,
    subject: { kind: "manual", source: "", id: "", title: "", url: "" },
    session: "lead",
    repo_root: "/r",
    branch: "b",
    state: "running",
    cursor,
    step_states: states,
    reason: "",
    history: [],
    started: 1,
    updated: 1,
    finished: 0,
    by: "",
    ...over,
  }) as Flight;

// Open PR, then two Notify stops side by side, then a Wait.
const PR = step("s1", "ship", "Open PR");
const N1 = par(step("s2", "notify", "Tell me"));
const N2 = par(step("s3", "notify", "Tell the team"));
const WAIT = step("s4", "wait", "Wait 5 min");
const STEPS = [PR, N1, N2, WAIT];

const KINDS = [
  { kind: "notify", parallel_safe: true },
  { kind: "wait", parallel_safe: true },
  { kind: "agent", parallel_safe: false },
  { kind: "ship", parallel_safe: false },
] as StepKindDef[];

const sessionHead: ChainHead = { kind: "session", title: "lead", sub: "", session: "lead", color: "x" };
const noHead: ChainHead = { kind: "none", title: "", sub: "", color: "x" };
const graphOf = (f: Flight, head = sessionHead): ChainGraph => G.chainGraph(D.flightDiagram(f), { head, key: "lead", flightId: f.id });
const nodeOf = (g: ChainGraph, id: string): GraphNode => g.nodes.find((n) => n.id === id)!;
const at = (g: ChainGraph, id: string) => ({ chain: g, node: nodeOf(g, id), side: "out" as const });
const sky = { sky: { x: 900, y: 400 } };

// --- lib/flights ---------------------------------------------------------------------

describe("lib/flights: the parallel group", () => {
  it("parallelSpan / parallelGroups read runs of one id", () => {
    expect(F.parallelSpan(STEPS, 1)).toEqual([1, 3]);
    expect(F.parallelSpan(STEPS, 2)).toEqual([1, 3]);
    expect(F.parallelSpan(STEPS, 0)).toEqual([0, 1]);
    expect(F.parallelGroups(STEPS)).toEqual([{ id: P.id, lo: 1, hi: 3 }]);
    // A run of one is no group; the same id later is a group of its own.
    const again = [N1, WAIT, N2, par(step("s5", "wait", "w"))];
    expect(F.parallelGroups(again)).toEqual([{ id: P.id, lo: 2, hi: 4 }]);
  });

  it("isParallelSafe: the kinds list when it says, else the client's own list", () => {
    expect(F.isParallelSafe("notify", KINDS)).toBe(true);
    expect(F.isParallelSafe("ship", KINDS)).toBe(false);
    for (const k of ["agent", "ship", "reviews", "verify", "split", "join", "land", "sync", "loop", "judge"]) expect(F.isParallelSafe(k)).toBe(false);
    for (const k of ["notify", "wait", "command", "you", "screenshot"]) expect(F.isParallelSafe(k)).toBe(true);
  });
});

// --- lib/flightsApi --------------------------------------------------------------------

describe("lib/flightsApi: making, leaving, joining a group", () => {
  it("wireStep keeps parallel", () => {
    expect(A.wireStep(N1).parallel).toEqual(P);
    expect("parallel" in A.wireStep(PR)).toBe(false);
  });

  it("tackOnParallel posts parallel_with (and at)", async () => {
    await A.tackOnParallel("f_p", step("", "notify", "Also"), "s2");
    expect(api).toHaveBeenCalledWith("/api/flights/f_p/steps", { json: { step: expect.objectContaining({ kind: "notify" }), parallel_with: "s2" } });
    await A.tackOn("f_p", step("", "wait", "W"), 2, { parallel_with: "s3" });
    expect(api).toHaveBeenLastCalledWith("/api/flights/f_p/steps", { json: { step: expect.objectContaining({ kind: "wait" }), at: 2, parallel_with: "s3" } });
    // Without it, the body is today's.
    await A.tackOn("f_p", step("", "wait", "W"));
    expect(api).toHaveBeenLastCalledWith("/api/flights/f_p/steps", { json: { step: expect.objectContaining({ kind: "wait" }) } });
  });

  it("leaveParallel / joinParallel PATCH the step", async () => {
    expect(await A.leaveParallel("f_p", "s3")).toBe(true);
    expect(api).toHaveBeenCalledWith("/api/flights/f_p/steps/s3", { method: "PATCH", json: { parallel: null } });
    expect(await A.joinParallel("f_p", "s4", "s2")).toBe(true);
    expect(api).toHaveBeenCalledWith("/api/flights/f_p/steps/s4", { method: "PATCH", json: { parallel_with: "s2" } });
  });
});

// --- lib/flightDiagram ---------------------------------------------------------------------

describe("lib/flightDiagram: a group is a fork and a join", () => {
  it("flywayDiagram marks the members and forks / joins the line", () => {
    const d = D.flywayDiagram(STEPS);
    const byId = (id: string) => d.nodes.find((n) => n.id === id)!;
    expect(byId("stop:s2").parallel).toEqual({ id: P.id, index: 0, size: 2 });
    expect(byId("stop:s3").parallel).toEqual({ id: P.id, index: 1, size: 2 });
    expect(byId("stop:s1").parallel).toBeUndefined();
    expect(d.edges.map((e) => [e.from, e.to, e.kind])).toEqual([
      ["stop:s1", "stop:s2", "fan"],
      ["stop:s1", "stop:s3", "fan"],
      ["stop:s2", "stop:s4", "join"],
      ["stop:s3", "stop:s4", "join"],
      ["stop:s4", "add", "add"],
    ]);
  });

  it("the + after a group trails its last member; two groups in a row join every member", () => {
    const d = D.flywayDiagram([PR, N1, N2]);
    expect(d.edges.filter((e) => e.to === "add").map((e) => e.from)).toEqual(["stop:s3"]);
    const Q = { id: "p_q" };
    const two = D.flywayDiagram([N1, N2, par(step("s5", "wait", "a"), Q.id), par(step("s6", "wait", "b"), Q.id)]);
    expect(two.edges.filter((e) => e.kind === "join").map((e) => e.from + ">" + e.to)).toEqual(["stop:s2>stop:s5", "stop:s2>stop:s6", "stop:s3>stop:s5", "stop:s3>stop:s6"]);
  });

  it("flightStops: every member of the group at the cursor flies; stops past it can change", () => {
    const f = flight(STEPS, 1, { s1: rec("done"), s2: rec("done"), s3: rec("running") });
    const d = D.flightDiagram(f);
    const byId = (id: string) => d.nodes.find((n) => n.id === id)!;
    expect(byId("stop:s2").state).toBe("done");
    expect(byId("stop:s3").state).toBe("current");
    expect(byId("stop:s3").editable).toBe(false);
    expect(byId("stop:s4").editable).toBe(true);
    // New stops go past the group, never inside it.
    expect(d.insertFrom).toBe(3);
    expect(D.insertFromOf(f)).toBe(3);
    // A member through draws its join done.
    expect(d.edges.find((e) => e.from === "stop:s2" && e.kind === "join")?.done).toBe(true);
    expect(d.edges.find((e) => e.from === "stop:s3" && e.kind === "join")?.done).toBeUndefined();
  });

  it("flightStops: a halted group marks the member that failed; a paused one its open members", () => {
    const halted = D.flightDiagram(flight(STEPS, 1, { s1: rec("done"), s2: rec("failed"), s3: rec("running") }, { state: "halted", reason: "no phone" }));
    expect(halted.nodes.find((n) => n.id === "stop:s2")!.state).toBe("halted");
    expect(halted.nodes.find((n) => n.id === "stop:s3")!.state).toBe("current");
    const paused = D.flightDiagram(flight(STEPS, 1, { s1: rec("done"), s2: rec("done"), s3: rec("running") }, { state: "paused" }));
    expect(paused.nodes.find((n) => n.id === "stop:s2")!.state).toBe("done");
    expect(paused.nodes.find((n) => n.id === "stop:s3")!.state).toBe("paused");
  });

  it("collapseFlown folds a flown group to plain line, and never ends inside one", () => {
    const steps = [step("s0", "command", "Build"), PR, N1, N2, WAIT];
    const all = D.collapseFlown(D.flightDiagram(flight(steps, 4, { s0: rec("done"), s1: rec("done"), s2: rec("done"), s3: rec("done") })));
    expect(all.nodes.find((n) => n.id === D.FLOWN_ID)?.count).toBe(4);
    expect(all.edges.filter((e) => e.from === D.FLOWN_ID).map((e) => [e.to, e.kind])).toEqual([["stop:s4", "line"]]);
    // One member still flying: the pill stops before the group.
    const half = D.collapseFlown(D.flightDiagram(flight(steps, 2, { s0: rec("done"), s1: rec("done"), s2: rec("done"), s3: rec("running") })));
    expect(half.nodes.find((n) => n.id === D.FLOWN_ID)?.count).toBe(2);
    expect(half.edges.filter((e) => e.from === D.FLOWN_ID).map((e) => [e.to, e.kind])).toEqual([
      ["stop:s2", "fan"],
      ["stop:s3", "fan"],
    ]);
  });

  it("layoutDiagram stacks a group's members in one column; the line goes on past it", () => {
    const L = D.layoutDiagram(D.flywayDiagram(STEPS));
    const box = (id: string) => L.boxes.find((b) => b.node.id === id)!;
    expect(box("stop:s2").x).toBe(box("stop:s3").x);
    expect(box("stop:s3").y).toBeGreaterThan(box("stop:s2").y);
    expect(box("stop:s4").x).toBeGreaterThan(box("stop:s2").x + box("stop:s2").w);
    expect(box("stop:s4").y).toBe(box("stop:s1").y);
    // Fork / join wires are drawn, the second member's as a curve.
    const fan = L.wires.find((w) => w.from === "stop:s1" && w.to === "stop:s3")!;
    expect(fan.kind).toBe("fan");
    expect(fan.d).toContain(" C ");
    // On a phone: members indented under the place before.
    const V = D.layoutDiagram(D.flywayDiagram(STEPS), { vertical: true });
    const vb = (id: string) => V.boxes.find((b) => b.node.id === id)!;
    expect(vb("stop:s2").x).toBeGreaterThan(vb("stop:s1").x);
    expect(vb("stop:s3").y).toBeGreaterThan(vb("stop:s2").y);
  });
});

// --- lib/skyGraph -----------------------------------------------------------------------------

describe("lib/skyGraph: the column, the wires, the +", () => {
  const f = flight(STEPS, 0, {}, { state: "grounded", session: "" });

  it("members stand in a column: the first at the group's spot, the rest PITCH_Y below", () => {
    const g = graphOf(f, noHead);
    const xy = (id: string) => [nodeOf(g, id).x, nodeOf(g, id).y];
    expect(xy("stop:s1")).toEqual([0, 0]);
    expect(xy("stop:s2")).toEqual([G.PITCH_X, 0]);
    expect(xy("stop:s3")).toEqual([G.PITCH_X, G.PITCH_Y]);
    // The line goes on from the group's row.
    expect(xy("stop:s4")).toEqual([2 * G.PITCH_X, 0]);
  });

  it("a pinned member keeps its spot; the column follows a moved first member", () => {
    const pinned = flight([PR, { ...N1, pos: { x: 400, y: 80 } }, N2, WAIT], 0, {}, { state: "grounded", session: "" });
    const g = graphOf(pinned, noHead);
    expect([nodeOf(g, "stop:s3").x, nodeOf(g, "stop:s3").y]).toEqual([400, 80 + G.PITCH_Y]);
    expect([nodeOf(g, "stop:s4").x, nodeOf(g, "stop:s4").y]).toEqual([400 + G.PITCH_X, 80]);
    const own = flight([PR, N1, { ...N2, pos: { x: 10, y: 600 } }, WAIT], 0, {}, { state: "grounded", session: "" });
    expect(graphOf(own, noHead).nodes.find((n) => n.id === "stop:s3")).toMatchObject({ x: 10, y: 600, pinned: true });
  });

  it("fork and join wires run out→in as béziers; the + trails the last member", () => {
    const g = graphOf(f, noHead);
    const w = (from: string, to: string) => g.wires.find((x) => x.from === from && x.to === to)!;
    expect(w("stop:s1", "stop:s3").kind).toBe("fan");
    expect(w("stop:s3", "stop:s4").kind).toBe("join");
    const a = nodeOf(g, "stop:s1");
    const b = nodeOf(g, "stop:s3");
    expect(w("stop:s1", "stop:s3").d).toBe(G.wirePath(G.outPortOf(a), G.inPortOf(b)));
    const tail = graphOf(flight([PR, N1, N2], 0, {}, { state: "grounded", session: "" }), noHead);
    expect(tail.plus?.after).toBe("stop:s3");
  });

  it("from the session head into a first group: one fork per member, no extra head wire", () => {
    const g = graphOf(flight([N1, N2, WAIT], 0));
    expect(g.wires.filter((x) => x.from === G.HEAD_ID).map((x) => [x.to, x.kind])).toEqual([
      ["stop:s2", "fan"],
      ["stop:s3", "fan"],
    ]);
  });

  it("groupLast / nextOf", () => {
    const g = graphOf(f, noHead);
    expect(G.groupLast(g, nodeOf(g, "stop:s2")).id).toBe("stop:s3");
    expect(G.groupLast(g, nodeOf(g, "stop:s1")).id).toBe("stop:s1");
    expect(G.nextOf(g, nodeOf(g, "stop:s1"))?.id).toBe("stop:s2");
    expect(G.nextOf(g, nodeOf(g, "stop:s2"))?.id).toBe("stop:s4");
    expect(G.nextOf(g, nodeOf(g, "stop:s4"))).toBeNull();
  });
});

describe("lib/skyGraph: wireIntent parallel", () => {
  // Grounded: Open PR, Notify, Wait (no group yet), every stop can change.
  const flat = () => graphOf(flight([PR, step("s2", "notify", "Tell me"), WAIT, step("s5", "notify", "Ping")], 0, {}, { state: "grounded", session: "" }), noHead);

  it("a wire from X's out port to empty sky while X has a next: a stop in parallel with that next", () => {
    const g = flat();
    expect(G.wireIntent(at(g, "stop:s1"), sky)).toEqual({ op: "parallel", chain: "lead", after: nodeOf(g, "stop:s1").node, with: nodeOf(g, "stop:s2").node, at: sky.sky });
    // The last stop has no next: today's palette.
    expect(G.wireIntent(at(g, "stop:s5"), sky)).toMatchObject({ op: "palette" });
  });

  it("a next that can't run in parallel (an agent, a ship…) keeps today's palette", () => {
    const g = graphOf(flight([step("s0", "command", "Build"), PR, WAIT], 0, {}, { state: "grounded", session: "" }), noHead);
    expect(G.wireIntent(at(g, "stop:s0"), sky)).toMatchObject({ op: "palette", after: nodeOf(g, "stop:s0").node });
    // The kinds list decides when given.
    const k = [{ kind: "wait", parallel_safe: false }] as StepKindDef[];
    expect(G.wireIntent(at(g, "stop:s1"), sky, { kinds: k })).toMatchObject({ op: "palette" });
    expect(G.wireIntent(at(g, "stop:s1"), sky)).toMatchObject({ op: "parallel" });
  });

  it("a next that already flies (not editable, in no group) keeps the palette", () => {
    const live = flight([step("s0", "command", "Build"), step("s2", "notify", "Tell"), WAIT], 0, { s0: rec("running") });
    const g = graphOf(live);
    // s0 has no out port at the cursor; from the head the palette is today's.
    expect(G.wireIntent({ chain: g, node: g.headNode!, side: "out" }, sky)).toMatchObject({ op: "palette", after: null });
    const later = flight([step("s0", "command", "Build"), step("s2", "notify", "Tell"), WAIT], 1, { s0: rec("done"), s2: rec("running") });
    const g2 = graphOf(later);
    expect(G.parallelOk(g2, nodeOf(g2, "stop:s2"))).toBe(false);
    expect(G.parallelOk(g2, nodeOf(g2, "stop:s4"))).toBe(true);
  });

  it("from a member, 'after X' is after its whole group", () => {
    const g = graphOf(f2(), noHead);
    expect(G.wireIntent(at(g, "stop:s2"), sky)).toMatchObject({ op: "parallel", after: nodeOf(g, "stop:s3").node, with: nodeOf(g, "stop:s4").node });
    // A Loop drawn back from a member goes after the group too.
    expect(G.wireIntent(at(g, "stop:s2"), { chain: g, node: nodeOf(g, "stop:s1"), side: "in" })).toMatchObject({ op: "loop", from: nodeOf(g, "stop:s3").node });
    // A member of X's own group is no move target.
    expect(G.wireIntent(at(g, "stop:s2"), { chain: g, node: nodeOf(g, "stop:s3"), side: "in" })).toEqual({ op: "none" });
  });

  it("Shift-drop on a stop Y of the same chain: Y joins X's next's group", () => {
    const g = flat();
    const y = { chain: g, node: nodeOf(g, "stop:s5"), side: "in" as const };
    expect(G.wireIntent(at(g, "stop:s1"), y, { shift: true })).toEqual({ op: "parallel", chain: "lead", after: nodeOf(g, "stop:s1").node, with: nodeOf(g, "stop:s2").node, step: nodeOf(g, "stop:s5").node });
    // Without Shift: today's move.
    expect(G.wireIntent(at(g, "stop:s1"), y)).toMatchObject({ op: "move" });
    // Not X's next itself, not X, not an unsafe stop.
    expect(G.wireIntent(at(g, "stop:s1"), { chain: g, node: nodeOf(g, "stop:s2"), side: "in" }, { shift: true })).toEqual({ op: "none" });
    const withShip = graphOf(flight([step("s0", "command", "Build"), step("s2", "notify", "Tell"), PR], 0, {}, { state: "grounded", session: "" }), noHead);
    expect(G.wireIntent(at(withShip, "stop:s0"), { chain: withShip, node: nodeOf(withShip, "stop:s1"), side: "in" }, { shift: true })).toEqual({ op: "none" });
    // wireTargets lights the Shift targets.
    expect([...(G.wireTargets(at(g, "stop:s1"), [g], { shift: true }).get("lead") || [])]).toEqual(["stop:s4", "stop:s5"]);
  });
});

// Build, Notify | Notify (a group), Wait — grounded.
function f2(): Flight {
  return flight([step("s1", "command", "Build"), N1, N2, WAIT], 0, {}, { state: "grounded", session: "" });
}

// --- useSkyWiring: running a dropped parallel wire --------------------------------------------

describe("useSkyWiring: runWireIntent parallel", () => {
  const F2 = flight([PR, step("s2", "notify", "Tell me"), WAIT], 0, {}, { id: "f_p", state: "grounded", session: "" });
  const g = graphOf(F2, noHead);
  const entry = (more: { paletteParallel?: ReturnType<typeof vi.fn> } = {}) => ({ graph: g, key: "lead", origin: { x: 100, y: 200 }, dx: 0, dy: 34, paletteAfter: vi.fn(), ...more });
  const deps = (c: ReturnType<typeof entry>) => ({
    flightOf: () => F2,
    flights: [F2],
    kinds: [] as StepKindDef[],
    chain: () => c as never,
    merge: vi.fn(),
    pinAt: vi.fn(),
    labelH: 34,
  });
  const src = { chain: g, node: nodeOf(g, "stop:s1"), side: "out" as const };
  const drop = { x: 100 + 300, y: 200 + 34 + 50 };

  it("on empty sky: the chain's paletteParallel for a stop beside X's next, its tile on the drop", async () => {
    const c = entry({ paletteParallel: vi.fn() });
    const intent = { op: "parallel" as const, chain: "lead", after: nodeOf(g, "stop:s1").node, with: nodeOf(g, "stop:s2").node, at: drop };
    await W.runWireIntent(intent, { src, at: drop }, deps(c));
    expect(c.paletteParallel).toHaveBeenCalledWith(nodeOf(g, "stop:s2").node, { x: 232, y: 16 });
    expect(c.paletteAfter).not.toHaveBeenCalled();
  });

  it("a chain without the hook opens today's palette after X", async () => {
    const c = entry();
    const intent = { op: "parallel" as const, chain: "lead", after: nodeOf(g, "stop:s1").node, with: nodeOf(g, "stop:s2").node, at: drop };
    await W.runWireIntent(intent, { src, at: drop }, deps(c));
    expect(c.paletteAfter).toHaveBeenCalledWith(nodeOf(g, "stop:s1").node, { x: 232, y: 16 });
  });

  it("Shift-drop: the stop joins the group through the API", async () => {
    const c = entry();
    const intent = { op: "parallel" as const, chain: "lead", after: nodeOf(g, "stop:s1").node, with: nodeOf(g, "stop:s2").node, step: nodeOf(g, "stop:s4").node };
    await W.runWireIntent(intent, { src, at: drop }, deps(c));
    expect(api).toHaveBeenCalledWith("/api/flights/f_p/steps/s4", { method: "PATCH", json: { parallel_with: "s2" } });
    expect(said).toHaveBeenCalledWith("“Wait 5 min” runs beside “Tell me”");
  });
});

// Keep the unused-type imports honest.
export type _Unused = DiagramNode;
