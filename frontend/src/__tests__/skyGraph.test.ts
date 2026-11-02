/** Round 18 — the free-floating chain's pure model (lib/skyGraph,
 * docs/flights.md "Round 18: free-floating chains"): placement (saved `pos`
 * else auto), ports, wires (the loop under the tiles), bbox, colours, hit
 * tests, and every row of the Wiring table (`wireIntent`). */

import { describe, expect, it } from "vitest";
import type { Flight, FlightStep, StepRunState } from "../lib/flights";
import { flightDiagram, type Diagram, type DiagramNode } from "../lib/flightDiagram";
import { groundedDiagram } from "../lib/groundedLines";
import {
  BRANCH_COLS,
  BRANCH_Y,
  CHAIN_HUES,
  GRID_SNAP,
  HEAD_ID,
  LOOP_CLEAR,
  LOOP_LANE,
  NODE_H,
  NODE_W,
  PITCH_X,
  PITCH_Y,
  PLUS_REACH,
  TILE,
  autoPlace,
  chainColor,
  chainGraph,
  inPortOf,
  loopWirePathG,
  moveNode,
  nodeAt,
  outPortOf,
  portAt,
  snapPos,
  wireIntent,
  wirePath,
  wireTargets,
  type ChainGraph,
  type ChainHead,
  type GraphNode,
} from "../lib/skyGraph";

const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}, extra: Partial<FlightStep> = {}): FlightStep => ({ id, kind, title, config, on_fail: "halt", ...extra });
const rec = (state: StepRunState["state"]): StepRunState => ({ state, since: 1, note: "", detail: {}, rounds: 0 });

function flight(steps: FlightStep[], cursor: number, over: Partial<Flight> = {}, states: Record<string, StepRunState> = {}): Flight {
  return {
    id: "f_live",
    plan_id: "p",
    plan_name: "Iterate",
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
  };
}

const sessionHead = (session = "lead"): ChainHead => ({ kind: "session", title: session, sub: "working", session, color: chainColor(session) });
const noHead: ChainHead = { kind: "none", title: "", sub: "", color: chainColor("") };
const triggerHead = (from = "f_live"): ChainHead => ({ kind: "trigger", title: "When Iterate reaches Hand off", sub: "runs in a new session", session: "new", color: chainColor("new"), from: { flight: from, step: "s9", title: "Hand off" } });

// A live flight at s2: s1 done, s2 current, s3..s5 pending (s4 a Loop back to s1).
const LIVE = [step("s1", "agent", "Improve"), step("s2", "command", "Screenshot"), step("s3", "agent", "Judge"), step("s4", "loop", "Loop ×3", { back_to: "", times: 3 }), step("s5", "ship", "Push")];
const liveFlight = () => flight(LIVE, 1, {}, { s1: rec("done"), s2: rec("running") });
const liveChain = (steps = LIVE): ChainGraph => chainGraph(flightDiagram(flight(steps, 1, {}, { s1: rec("done"), s2: rec("running") })), { head: sessionHead(), key: "lead", flightId: "f_live" });

const GROUNDED = [step("g1", "agent", "Fix"), step("g2", "checks", "Checks"), step("g3", "ship", "Push")];
const groundedChain = (head: ChainHead = noHead, steps = GROUNDED, id = "f_g"): ChainGraph =>
  chainGraph(groundedDiagram(flight(steps, 0, { id, state: "grounded", session: "" })), { head, key: "flight:" + id, flightId: id });

const node = (g: ChainGraph, id: string): GraphNode => {
  const n = id === HEAD_ID ? g.headNode : g.nodes.find((x) => x.id === id);
  if (!n) throw new Error("no node " + id);
  return n;
};
const at = (g: ChainGraph, id: string, side: "in" | "out" = "out") => ({ chain: g, node: node(g, id), side });

describe("placement", () => {
  it("the head at the origin, stops left→right at PITCH_X after it, none pinned", () => {
    const g = liveChain();
    expect(g.headNode).toMatchObject({ id: HEAD_ID, x: 0, y: 0, w: NODE_W, h: NODE_H, inPort: false, outPort: true });
    // The Loop rides its wire: no tile.
    expect(g.nodes.map((n) => n.id)).toEqual(["stop:s1", "stop:s2", "stop:s3", "stop:s5"]);
    expect(g.nodes.map((n) => [n.x, n.y])).toEqual([
      [PITCH_X, 0],
      [2 * PITCH_X, 0],
      [3 * PITCH_X, 0],
      [4 * PITCH_X, 0],
    ]);
    expect(g.nodes.every((n) => !n.pinned)).toBe(true);
  });

  it("a headless chain starts at the slot's origin", () => {
    const g = groundedChain();
    expect(g.headNode).toBeNull();
    expect(g.nodes.map((n) => n.x)).toEqual([0, PITCH_X, 2 * PITCH_X]);
  });

  it("a saved pos pins the node; the auto-placed tail follows it", () => {
    const steps = [GROUNDED[0], { ...GROUNDED[1], pos: { x: 400, y: 96 } }, GROUNDED[2]];
    const g = groundedChain(noHead, steps);
    expect(g.nodes.map((n) => [n.x, n.y, n.pinned])).toEqual([
      [0, 0, false],
      [400, 96, true],
      [400 + PITCH_X, 96, false],
    ]);
  });

  it("a group's pos is its first sub-step's", () => {
    const grp = { id: "gg", name: "Ship it", plan_id: "" };
    const steps = [GROUNDED[0], { ...GROUNDED[1], group: grp, pos: { x: 10, y: 300 } }, { ...GROUNDED[2], group: grp }];
    const g = chainGraph(groundedDiagram(flight(steps, 0, { id: "f_g", state: "grounded" })), { head: noHead, key: "flight:f_g", flightId: "f_g" });
    // groundedDiagram doesn't collapse groups by itself; the Sky collapses them first.
    expect(g.nodes.find((n) => n.id === "stop:g2")).toMatchObject({ x: 10, y: 300, pinned: true });
  });

  it("autoPlace: branches a row PITCH_Y below their split, from half a pitch right, BRANCH_COLS to a row", () => {
    const mk = (id: string, extra: Partial<DiagramNode> = {}, pinned = false, x = 0, y = 0): GraphNode => ({
      id,
      node: { id, kind: "stop", row: "main", title: id, sub: "", state: "pending", icon: "agent", ...extra },
      x,
      y,
      w: NODE_W,
      h: NODE_H,
      pinned,
      inPort: true,
      outPort: true,
    });
    const br = (id: string) => mk(id, { kind: "branch", row: "branch", branch: { from: "a", to: "b", state: "working", inner: "" } });
    const out = autoPlace([mk("a", {}, true, 40, 16), mk("b"), br("c1"), br("c2")]);
    expect(out.map((n) => [n.id, n.x, n.y])).toEqual([
      ["a", 40, 16],
      ["b", 40 + PITCH_X, 16],
      ["c1", 40 + PITCH_X / 2, 16 + BRANCH_Y],
      ["c2", 40 + PITCH_X / 2 + PITCH_X, 16 + BRANCH_Y],
    ]);
    // Past BRANCH_COLS a new row starts, back under the split.
    const many = autoPlace([mk("a", {}, true, 0, 0), ...Array.from({ length: BRANCH_COLS + 1 }, (_, i) => br("k" + i))]);
    const last = many[many.length - 1];
    expect([last.x, last.y]).toEqual([PITCH_X / 2, BRANCH_Y + PITCH_Y]);
    expect(new Set(many.slice(1, 1 + BRANCH_COLS).map((n) => n.y)).size).toBe(1);
  });

  it("moveNode pins the node, the tail follows, wires re-route, the head never moves", () => {
    const g = groundedChain();
    const m = moveNode(g, "stop:g2", { x: 200, y: 160 });
    expect(m.nodes.map((n) => [n.x, n.y])).toEqual([
      [0, 0],
      [200, 160],
      [200 + PITCH_X, 160],
    ]);
    const w = m.wires.find((x) => x.from === "stop:g1" && x.to === "stop:g2")!;
    expect(w.d).toBe(wirePath(outPortOf(m.nodes[0]), inPortOf(m.nodes[1])));
    expect(m.bbox.h).toBe(160 + NODE_H);
    const s = liveChain();
    expect(moveNode(s, HEAD_ID, { x: 99, y: 99 })).toBe(s);
  });

  it("snapPos: the 8 px grid, or free with Alt", () => {
    expect(snapPos({ x: 13, y: 21 })).toEqual({ x: 16, y: 24 });
    expect(GRID_SNAP).toBe(8);
    expect(snapPos({ x: 13.4, y: 21.6 }, true)).toEqual({ x: 13, y: 22 });
  });
});

describe("ports", () => {
  it("a live flight: only stops that can still change take a wire in; out where a stop may follow", () => {
    const g = liveChain();
    const p = (id: string) => [node(g, id).inPort, node(g, id).outPort];
    expect(p("stop:s1")).toEqual([false, false]); // done
    expect(p("stop:s2")).toEqual([false, true]); // current: a stop may go right after it
    expect(p("stop:s3")).toEqual([true, true]);
    expect(p("stop:s5")).toEqual([true, true]);
  });

  it("a stops-only chain: every stop, its first node's in port too", () => {
    const g = groundedChain();
    expect(g.nodes.map((n) => [n.inPort, n.outPort])).toEqual([
      [true, true],
      [true, true],
      [true, true],
    ]);
  });

  it("port spots: in = tile's left middle, out = right middle", () => {
    const n = { x: 100, y: 20, w: NODE_W };
    expect(inPortOf(n)).toEqual({ x: 100 + (NODE_W - TILE) / 2, y: 20 + TILE / 2 });
    expect(outPortOf(n)).toEqual({ x: 100 + (NODE_W + TILE) / 2, y: 20 + TILE / 2 });
  });
});

describe("wires", () => {
  it("head → first stop (tinted head wire), line wires, the + stub off the last node", () => {
    const g = liveChain();
    const kinds = g.wires.map((w) => [w.kind, w.from, w.to]);
    expect(kinds).toContainEqual(["head", HEAD_ID, "stop:s1"]);
    expect(kinds).toContainEqual(["line", "stop:s1", "stop:s2"]);
    expect(kinds).toContainEqual(["line", "stop:s3", "stop:s5"]);
    expect(kinds).toContainEqual(["add", "stop:s5", "add"]);
    const o = outPortOf(node(g, "stop:s5"));
    expect(g.plus).toEqual({ after: "stop:s5", at: { x: o.x + PLUS_REACH, y: o.y } });
    const line = g.wires.find((w) => w.from === "stop:s1")!;
    expect(line.d).toBe(wirePath(outPortOf(node(g, "stop:s1")), inPortOf(node(g, "stop:s2"))));
  });

  it("the session node of a session diagram IS the head", () => {
    const d = flightDiagram(liveFlight());
    const me: DiagramNode = { id: "session:lead", kind: "session", row: "main", title: "lead", sub: "", state: "session", icon: "session", session: "lead" };
    const withMe: Diagram = { ...d, nodes: [me, ...d.nodes], edges: [{ from: me.id, to: "stop:s1", kind: "line" }, ...d.edges] };
    const g = chainGraph(withMe, { head: { ...sessionHead(), node: me }, key: "lead", flightId: "f_live" });
    expect(g.headNode!.node).toBe(me);
    expect(g.nodes.some((n) => n.node.kind === "session")).toBe(false);
    expect(g.wires.filter((w) => w.from === HEAD_ID).map((w) => [w.kind, w.to])).toEqual([["head", "stop:s1"]]);
  });

  it("a head with no stops carries the +", () => {
    const d: Diagram = { nodes: [{ id: "add", kind: "add", row: "main", title: "Tack on a stop", sub: "", state: "pending", icon: "add" }], edges: [], flightId: "", mode: "none", insertFrom: 0 };
    const g = chainGraph(d, { head: sessionHead(), key: "lead", flightId: "" });
    expect(g.nodes).toEqual([]);
    expect(g.plus?.after).toBe(HEAD_ID);
  });

  it("a loop leaves the out port, runs under both tiles and everything between, comes up into the in port; pill at mid", () => {
    const g = liveChain();
    const w = g.wires.find((x) => x.kind === "loop")!;
    expect([w.from, w.to]).toEqual(["stop:s3", "stop:s1"]);
    expect(w.via?.id).toBe("stop:s4");
    expect(w.loop).toMatchObject({ times: 3 });
    const a = node(g, "stop:s3");
    const b = node(g, "stop:s1");
    const o = outPortOf(a);
    const i = inPortOf(b);
    expect(w.d.startsWith(`M ${o.x} ${o.y} `)).toBe(true);
    expect(w.d.endsWith(`${i.x} ${i.y}`)).toBe(true);
    expect(w.mid).toEqual({ x: (o.x + i.x) / 2, y: NODE_H + LOOP_CLEAR });
    // Its run clears the lowest box between, wherever it was dragged.
    const m = moveNode(g, "stop:s2", { x: 2 * PITCH_X, y: 80 });
    const w2 = m.wires.find((x) => x.kind === "loop")!;
    expect(w2.mid.y).toBe(80 + NODE_H + LOOP_CLEAR);
  });

  it("loopWirePathG: nested lanes run lower", () => {
    const n = (x: number): GraphNode => ({ id: "n" + x, node: { id: "n" + x, kind: "stop", row: "main", title: "", sub: "", state: "pending", icon: "agent" }, x, y: 0, w: NODE_W, h: NODE_H, pinned: false, inPort: true, outPort: true });
    expect(loopWirePathG(n(300), n(0), [n(150)], 1).mid.y).toBe(NODE_H + LOOP_CLEAR + LOOP_LANE);
    // Two loops over the same span: the wider one takes the lower lane.
    const steps = [step("a", "agent", "A"), step("b", "agent", "B"), step("l1", "loop", "L1", { back_to: "b", times: 2 }), step("l2", "loop", "L2", { back_to: "a", times: 2 })];
    const g = groundedChain(noHead, steps);
    const loops = g.wires.filter((w) => w.kind === "loop");
    expect(loops).toHaveLength(2);
    const lanes = Object.fromEntries(loops.map((w) => [w.via!.id, w.lane]));
    expect(lanes).toEqual({ "stop:l1": 0, "stop:l2": 1 });
  });

  it("the bbox holds every box, the + and the loop's run and pill", () => {
    const g = liveChain();
    const w = g.wires.find((x) => x.kind === "loop")!;
    expect(g.bbox.x).toBeLessThanOrEqual(0);
    expect(g.bbox.y).toBe(0);
    expect(g.bbox.x + g.bbox.w).toBeGreaterThanOrEqual(g.plus!.at.x + 12);
    expect(g.bbox.y + g.bbox.h).toBeGreaterThanOrEqual(w.mid.y + 12);
    // A node dragged above / left of the origin widens it that way.
    const m = moveNode(groundedChain(), "stop:g1", { x: -80, y: -40 });
    expect(m.bbox.x).toBe(-80);
    expect(m.bbox.y).toBe(-40);
  });
});

describe("colour and hit tests", () => {
  it("chainColor: one of 8 hues, stable per title; neutral without a session", () => {
    expect(chainColor("lead")).toBe(chainColor("lead"));
    const hues = new Set(["a", "b", "lead", "worker-1", "worker-2", "x", "yy", "zzz", "q", "rr"].map((t) => chainColor(t)));
    for (const c of hues) expect(CHAIN_HUES.some((h) => c === `hsl(${h} 70% 60%)`)).toBe(true);
    expect(hues.size).toBeGreaterThan(2);
    expect(chainColor("")).toBe("var(--muted)");
    expect(chainColor(null)).toBe("var(--muted)");
    expect(chainColor("new")).toBe("var(--muted)");
  });

  it("nodeAt / portAt", () => {
    const g = liveChain();
    expect(nodeAt(g, { x: 5, y: 5 })?.id).toBe(HEAD_ID);
    expect(nodeAt(g, { x: PITCH_X + 10, y: 100 })?.id).toBe("stop:s1");
    expect(nodeAt(g, { x: 140, y: 5 })).toBeNull();
    const s3 = node(g, "stop:s3");
    expect(portAt(g, inPortOf(s3))).toEqual({ node: s3, side: "in" });
    expect(portAt(g, { x: outPortOf(s3).x + 4, y: outPortOf(s3).y })).toEqual({ node: s3, side: "out" });
    // The done stop takes no wire: no live port.
    expect(portAt(g, inPortOf(node(g, "stop:s1")))).toBeNull();
    expect(portAt(g, outPortOf(g.headNode!))?.side).toBe("out");
  });
});

describe("wireIntent — the Wiring table", () => {
  const A = () => liveChain();
  const B = () => groundedChain(noHead, GROUNDED, "f_b");
  const T = () => groundedChain(triggerHead(), GROUNDED, "f_t");
  const sky = { sky: { x: 900, y: 400 } };

  it("move: X out on a node Y of A after X → Y right after X", () => {
    const g = A();
    expect(wireIntent(at(g, "stop:s2"), at(g, "stop:s5", "in"))).toEqual({ op: "move", chain: "lead", step: node(g, "stop:s5").node, after: node(g, "stop:s2").node });
    // Already right after it: nothing to do.
    expect(wireIntent(at(g, "stop:s2"), at(g, "stop:s3", "in"))).toEqual({ op: "none" });
  });

  it("loop: X out on a node at or before X (X itself too) → a Loop after X back to Y", () => {
    const g = A();
    expect(wireIntent(at(g, "stop:s3"), at(g, "stop:s1"))).toEqual({ op: "loop", chain: "lead", from: node(g, "stop:s3").node, to: node(g, "stop:s1").node });
    expect(wireIntent(at(g, "stop:s3"), at(g, "stop:s3"))).toMatchObject({ op: "loop" });
  });

  it("merge: X out on the first node / socket of a stops-only chain → spliced after X", () => {
    const g = A();
    const b = B();
    expect(wireIntent(at(g, "stop:s3"), at(b, "stop:g1", "in"))).toEqual({ op: "merge", chain: "lead", after: node(g, "stop:s3").node, from: "flight:f_b" });
    // Not its first node.
    expect(wireIntent(at(g, "stop:s3"), at(b, "stop:g2", "in"))).toEqual({ op: "none" });
    // Stops-only into stops-only merges too.
    const c = groundedChain(noHead, GROUNDED, "f_c");
    expect(wireIntent(at(c, "stop:g3"), at(b, "stop:g1"))).toMatchObject({ op: "merge", chain: "flight:f_c", from: "flight:f_b" });
  });

  it("handoff: A has a session and the drop asks for another (Shift, or B already has a trigger)", () => {
    const g = A();
    expect(wireIntent(at(g, "stop:s3"), at(B(), "stop:g1"), { shift: true })).toEqual({ op: "handoff", chain: "lead", from: node(g, "stop:s3").node, target: "flight:f_b" });
    const t = T();
    expect(wireIntent(at(g, "stop:s3"), at(t, HEAD_ID))).toMatchObject({ op: "handoff", target: "flight:f_t" });
    expect(wireIntent(at(g, "stop:s3"), at(t, "stop:g1"))).toMatchObject({ op: "handoff", target: "flight:f_t" });
    // A stops-only A can't hand off; a triggered B can't be merged.
    const c = groundedChain(noHead, GROUNDED, "f_c");
    expect(wireIntent(at(c, "stop:g3"), at(t, "stop:g1"))).toEqual({ op: "none" });
  });

  it("attach: a session tile's out on the first node / socket of a stops-only chain", () => {
    const g = A();
    expect(wireIntent(at(g, HEAD_ID), at(B(), "stop:g1", "in"))).toEqual({ op: "attach", session: "lead", chain: "flight:f_b" });
    expect(wireIntent(at(g, HEAD_ID), at(B(), "stop:g2"))).toEqual({ op: "none" });
    // Its own stops: nothing.
    expect(wireIntent(at(g, HEAD_ID), at(g, "stop:s3"))).toEqual({ op: "none" });
  });

  it("palette: an out port (node, session, the +) dropped on empty sky", () => {
    const g = A();
    expect(wireIntent(at(g, "stop:s5"), sky)).toEqual({ op: "palette", chain: "lead", after: node(g, "stop:s5").node, at: sky.sky });
    expect(wireIntent(at(g, HEAD_ID), sky)).toEqual({ op: "palette", chain: "lead", after: null, at: sky.sky });
  });

  it("split: an in port's wire end pulled into empty sky", () => {
    const g = A();
    expect(wireIntent(at(g, "stop:s3", "in"), sky)).toEqual({ op: "split", chain: "lead", step: node(g, "stop:s3").node, at: sky.sky });
    // A stop that has flown can't leave; nor the whole of a stops-only chain.
    expect(wireIntent(at(g, "stop:s1", "in"), sky)).toEqual({ op: "none" });
    expect(wireIntent(at(B(), "stop:g1", "in"), sky)).toEqual({ op: "none" });
    expect(wireIntent(at(B(), "stop:g2", "in"), sky)).toMatchObject({ op: "split", chain: "flight:f_b" });
  });

  it("none: only targets that can change, nothing that fits", () => {
    const g = A();
    // A done stop has no out port; a done stop can't move.
    expect(wireIntent(at(g, "stop:s1"), sky)).toEqual({ op: "none" });
    // The current stop's out on a done stop before it: the Loop target is fine, so loop…
    expect(wireIntent(at(g, "stop:s2"), at(g, "stop:s1"))).toMatchObject({ op: "loop" });
    // …but a session chain can't be merged into or handed to.
    const other = chainGraph(flightDiagram(flight(GROUNDED, 0, { id: "f_o" })), { head: sessionHead("other"), key: "other", flightId: "f_o" });
    expect(wireIntent(at(g, "stop:s3"), at(other, "stop:g1"))).toEqual({ op: "none" });
    expect(wireIntent(at(g, "stop:s3"), at(other, HEAD_ID), { shift: true })).toEqual({ op: "none" });
    // From the sky, or onto the own head.
    expect(wireIntent(sky, sky)).toEqual({ op: "none" });
    expect(wireIntent(at(g, "stop:s3"), at(g, HEAD_ID))).toEqual({ op: "none" });
    // An in port dropped on a node.
    expect(wireIntent(at(g, "stop:s3", "in"), at(g, "stop:s5"))).toEqual({ op: "none" });
    // A trigger head's out on the sky.
    expect(wireIntent(at(T(), HEAD_ID), sky)).toEqual({ op: "none" });
  });

  it("wireTargets lights every node a drop means something on, per chain", () => {
    const g = A();
    const b = B();
    const t = wireTargets(at(g, "stop:s3"), [g, b]);
    expect([...t.get("lead")!].sort()).toEqual(["stop:s1", "stop:s2", "stop:s3", "stop:s5"]);
    expect([...t.get("flight:f_b")!]).toEqual(["stop:g1"]);
  });
});
