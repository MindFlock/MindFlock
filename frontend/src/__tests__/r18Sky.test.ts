/** Round 18 — the Sky integration of free-floating chains (docs/flights.md
 * "Round 18", docs/flights-r18/sky.md): chain heads (session / trigger /
 * none), the Handoff links and their dashed wires between chains, what a
 * dropped wire DOES (runWireIntent, with the flight routes mocked), node
 * spots batched per flight, the Handoff dialog's board-filled selects, and
 * the host's wiring pinned in the source. The graph model itself
 * (skyGraph, SkyChain) is r18-graph's: its `wireIntent` is mocked here. */

/// <reference types="vite/client" />
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { code, css } from "./sourceText";
import type { Flight, FlightStep, StepKindDef } from "../lib/flights";
import type { DiagramNode } from "../lib/flightDiagram";
import type { ChainGraph, ChainHead, GraphNode, WireEnd } from "../lib/skyGraph";

const routes = vi.hoisted(() => ({
  moveStop: vi.fn(async (..._a: unknown[]) => true),
  tackOn: vi.fn(async (..._a: unknown[]) => true),
  tackOnNew: vi.fn(async (..._a: unknown[]) => ({ id: "f_new" })),
  splitOffChain: vi.fn(async (..._a: unknown[]): Promise<unknown> => null),
  saveStopPositions: vi.fn(async (..._a: unknown[]) => true),
}));
const said = vi.hoisted(() => vi.fn());
// What a drop decides is r18-graph's (lib/skyGraph `wireIntent`): a stand-in
// that lights one tile, so the host's half is tested on its own.
const intentOf = vi.hoisted(() => vi.fn((_src: unknown, dst: { node?: { id: string } }) => (dst.node?.id === "stop:s3" ? { op: "move" } : { op: "none" })));

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})), instApi: vi.fn(async () => ({})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: said }));
vi.mock("../lib/flightsApi", async (orig) => ({ ...(await orig<typeof import("../lib/flightsApi")>()), ...routes }));
vi.mock("../lib/skyGraph", async (orig) => ({ ...(await orig<typeof import("../lib/skyGraph")>()), wireIntent: intentOf }));

const G = await import("../lib/groundedLines");
const { chainColor, inPortOf, outPortOf } = await import("../lib/skyGraph");
const W = await import("../components/flights/useSkyWiring");
const { handoffWires, nodeOfStep, panelSpot } = await import("../components/flights/SkyWires");
const SP = await import("../components/flights/StopParts");

afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
});

// --- Fixtures ------------------------------------------------------------------------

const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}, more: Partial<FlightStep> = {}): FlightStep => ({ id, kind, title, config, on_fail: "halt", ...more });
const flight = (over: Partial<Flight>): Flight =>
  ({ id: "f_x", plan_id: "adhoc", plan_name: "", session: "", state: "running", cursor: 0, steps: [], step_states: {}, history: [], started: 1, ...over }) as unknown as Flight;
const KINDS: StepKindDef[] = [
  { kind: "agent", label: "Agent", actor: "robot", summary: "", fields: [], default_title: "Agent works" },
  { kind: "loop", label: "Loop", actor: "robot", summary: "", fields: [{ name: "back_to", type: "select", label: "Repeat from", default: "" }], default_title: "Loop" },
  {
    kind: "handoff",
    label: "Start a route",
    actor: "robot",
    summary: "",
    default_title: "Start a route",
    fields: [
      { name: "target", type: "select", label: "Start which route", default: "", options: [], dynamic: "chains" as never },
      { name: "session", type: "select", label: "Run it on", default: "new", options: ["new"], dynamic: "sessions" as never },
      { name: "wait", type: "bool", label: "Wait for it to land", default: false },
    ],
  },
];

const dn = (id: string, index: number, over: Partial<DiagramNode> = {}): DiagramNode =>
  ({ id, kind: "stop", row: "main", title: id, sub: "", state: "pending", icon: "agent", index, editable: true, step: step(id.replace(/^stop:/, ""), "agent", id), ...over }) as DiagramNode;
const gn = (id: string, index: number, x: number, y = 0, over: Partial<DiagramNode> = {}): GraphNode => ({ id, node: dn(id, index, over), x, y, w: 132, h: 118, pinned: false, inPort: true, outPort: true });
const NONE: ChainHead = { kind: "none", title: "", sub: "", color: "var(--muted)" };
const chain = (key: string, flightId: string, nodes: GraphNode[], head: ChainHead = NONE): ChainGraph => ({
  key,
  flightId,
  head,
  headNode: head.kind === "none" ? null : { ...gn("head", -1, 0), id: "head" },
  nodes,
  wires: [],
  bbox: { x: 0, y: 0, w: 800, h: 118 },
});
const placed = (g: ChainGraph, origin: { x: number; y: number }, dy = 34) => ({ graph: g, key: g.key, origin, dx: 0, dy, paletteAfter: vi.fn() });

// A session chain "me" (flight f_a: s1 s2 s3 s4) and a stops-only chain f_b.
const A_NODES = [gn("stop:s1", 0, 150), gn("stop:s2", 1, 300), gn("stop:s3", 2, 450), gn("stop:s4", 3, 600)];
const SESSION_HEAD: ChainHead = { kind: "session", title: "me", sub: "", session: "me", color: chainColor("me") };
const A = placed(chain("me", "f_a", A_NODES, SESSION_HEAD), { x: 100, y: 200 });
const B = placed(chain("flight:f_b", "f_b", [gn("stop:s1", 0, 0), gn("stop:s2", 1, 150)]), { x: 900, y: 200 });
const FA = flight({ id: "f_a", session: "me", plan_name: "Fix", steps: [step("s1", "agent", "Work"), step("s2", "agent", "Check"), step("s3", "agent", "Push"), step("s4", "agent", "Tell")] });
const FB = flight({ id: "f_b", state: "grounded", plan_name: "Release", steps: [step("s1", "agent", "Tag"), step("s2", "agent", "Notes")] });

// --- Heads and handoffs (lib/groundedLines) ---------------------------------------------

describe("one route starts another: the links and the trigger head", () => {
  const withHandoff = (detail?: Record<string, unknown>) =>
    flight({
      id: "f_a",
      session: "me",
      steps: [step("s1", "agent", "Work"), step("s2", "handoff", "Start a route", { target: "flight:f_b", session: "new", wait: false })],
      step_states: detail ? ({ s2: { state: "done", detail } } as never) : {},
    });

  it("a Handoff naming a stops-only route links to it; nothing else does", () => {
    const links = G.handoffLinks([withHandoff(), FB]);
    expect(links).toEqual([{ fromFlight: "f_a", fromStep: "s2", stepTitle: "Start a route", fromSession: "me", fromName: "", target: "f_b", started: false, session: "new" }]);
    // A saved stop target names no chain until it has started one.
    const plan = flight({ id: "f_p", steps: [step("s1", "handoff", "Start a route", { target: "plan:p_1" })] });
    expect(G.handoffLinks([plan])).toEqual([]);
    expect(G.handoffLinks(null)).toEqual([]);
  });

  it("once started, the link follows the started flight (detail.child) and its session", () => {
    const links = G.handoffLinks([withHandoff({ child: "f_c", session: "me-w1" })]);
    expect(links[0]).toMatchObject({ target: "f_c", started: true, session: "me-w1" });
  });

  it("the trigger head: “When <route> reaches <stop>”, where it runs, the SOURCE session's colour", () => {
    const head = G.triggerHeadFor(FB, [withHandoff(), FB], (t) => t.toUpperCase());
    expect(head).toEqual({
      kind: "trigger",
      title: "When ME reaches Start a route",
      sub: "runs on a new session",
      session: "new",
      color: chainColor("me"),
      from: { flight: "f_a", step: "s2", title: "Start a route" },
    });
    // Nothing hands off to the source; a started link leaves the chain headless.
    expect(G.triggerHeadFor(FA, [withHandoff(), FB])).toBeNull();
    expect(G.triggerHeadFor(FB, [withHandoff({ child: "f_b2" }), FB])).toBeNull();
    expect(G.sessionWord("new")).toBe("a new session");
    expect(G.sessionWord("sc-1", () => "Login fix")).toBe("Login fix");
  });

  it("a Handoff stop: the kind's defaults, aimed at the route, on a new session, not waiting", () => {
    const st = G.handoffStepFor(KINDS[2], ["s1", "s2"], "flight:f_b");
    expect(st).toMatchObject({ id: "s3", kind: "handoff", title: "Start a route", config: { target: "flight:f_b", session: "new", wait: false } });
  });
});

// --- The wires between chains (SkyWires) ------------------------------------------------

describe("handoff wires", () => {
  it("run from the Handoff tile's out port to the target route's entry, dashed in the source's colour", () => {
    const links = [{ fromFlight: "f_a", fromStep: "s3", stepTitle: "Push", fromSession: "me", fromName: "", target: "f_b", started: false, session: "new" }];
    const [w] = handoffWires(links, [A, B]);
    const o = outPortOf(A_NODES[2]);
    const i = inPortOf(B.graph.nodes[0]);
    expect(w.from).toEqual({ x: 100 + o.x, y: 200 + 34 + o.y });
    expect(w.to).toEqual({ x: 900 + i.x, y: 200 + 34 + i.y });
    expect(w.d.startsWith("M " + w.from.x + " " + w.from.y)).toBe(true);
    expect(w.color).toBe(chainColor("me"));
    expect(w.started).toBe(false);
    // An end off the plane (a filtered chain): no wire.
    expect(handoffWires(links, [A])).toEqual([]);
  });

  it("a step inside a group is drawn from its group's tile", () => {
    const grp = gn("group:g1", 0, 0, 0, { kind: "group", step: undefined, group: { id: "g1", name: "G", total: 2, steps: [step("s1", "agent", "a"), step("s2", "handoff", "b")], plan_id: "", nodes: [] } as never });
    expect(nodeOfStep({ headNode: null, nodes: [grp] }, "s2")?.id).toBe("group:g1");
    expect(nodeOfStep({ headNode: null, nodes: [grp] }, "nope")).toBeNull();
  });

  it("a palette or dialog floats right of its tile, or at the spot a wire was dropped", () => {
    const g = { headNode: null, nodes: A_NODES };
    expect(panelSpot(g, "stop:s2", null)).toEqual({ x: 300 + (132 + 64) / 2 + 16, y: -6 });
    expect(panelSpot(g, "", null).x).toBeGreaterThan(600);
    expect(panelSpot(g, "stop:s2", { x: 40, y: 80 })).toEqual({ x: 64, y: 48 });
  });
});

// --- The host's wiring helpers (useSkyWiring) --------------------------------------------

describe("wiring helpers", () => {
  it("the targets of a drag: every node wireIntent says something about, by route", () => {
    const src: WireEnd = { chain: A.graph, node: A_NODES[0], side: "out" };
    const lit = W.wireTargetsOf(src, [A, B]);
    expect([...lit.keys()]).toEqual(["me"]);
    expect([...lit.get("me")!]).toEqual(["stop:s3"]);
    expect(intentOf).toHaveBeenCalledWith(src, expect.objectContaining({ side: "in" }), { shift: false });
  });

  it("the node under the pointer: a tile, port or socket inside a route — never inside an open card", () => {
    const el = (attrs: Record<string, string | null>) => {
      const node = { getAttribute: (k: string) => attrs[k] ?? null, closest: (s: string) => (s.includes(".sky-chain") ? chainEl : s === ".skl-card" ? (attrs.card ? {} : null) : null) };
      const chainEl = { getAttribute: (k: string) => (k === "data-chain" ? attrs.chain : null) };
      return { closest: (s: string) => (s === "[data-node]" ? node : null) } as unknown as Element;
    };
    const map = new Map([["me", A]]);
    expect(W.nodeUnder(el({ "data-node": "stop:s2", chain: "me" }), map)).toEqual({ key: "me", node: A_NODES[1] });
    expect(W.nodeUnder(el({ "data-node": "head", chain: "me" }), map)?.node.id).toBe("head");
    expect(W.nodeUnder(el({ "data-node": "stop:s2", chain: "me", card: "1" }), map)).toBeNull();
    expect(W.nodeUnder(el({ "data-node": "stop:s2", chain: "gone" }), map)).toBeNull();
    expect(W.nodeUnder(null, map)).toBeNull();
  });

  it("the host redraws only when what it draws moved — a graph that is merely a new object is no change", () => {
    const again = { ...A, graph: { ...A.graph, nodes: A.graph.nodes.map((n) => ({ ...n })) } };
    expect(W.entrySig(again)).toBe(W.entrySig(A));
    const moved = { ...A, graph: { ...A.graph, nodes: [{ ...A_NODES[0], x: 160 }, ...A_NODES.slice(1)] } };
    expect(W.entrySig(moved)).not.toBe(W.entrySig(A));
    expect(W.entrySig({ ...A, dy: 40 })).not.toBe(W.entrySig(A));
    // The loading lists are one object each, so the chains' memos hold.
    expect(code("components/flights/SkyView.tsx")).toContain("const kinds = useFlightKinds().data || NO_KINDS;");
    expect(code("components/flights/SkyLine.tsx")).toContain("kinds: kinds || NO_KINDS");
  });

  it("plane ↔ route px go through the slot's origin and the route's place in it", () => {
    expect(W.onPlane(A, { x: 10, y: 5 })).toEqual({ x: 110, y: 239 });
    expect(W.inChain(A, { x: 110, y: 239 })).toEqual({ x: 10, y: 5 });
  });

  it("a dropped tile's spot is laid over its step (a group's first sub-step) until the server has it", () => {
    const d = { nodes: [dn("stop:s1", 0), dn("group:g1", 1, { kind: "group", step: undefined, group: { id: "g1", name: "G", total: 2, steps: [step("s2", "agent", "a"), step("s3", "agent", "b")] } as never })], edges: [], insertFrom: 0 } as never;
    expect(W.withLocalPos(d, {})).toBe(d);
    const out = W.withLocalPos(d, { s1: { x: 8, y: 16 }, s2: { x: 40, y: 0 } }) as unknown as { nodes: DiagramNode[] };
    expect(out.nodes[0].step?.pos).toEqual({ x: 8, y: 16 });
    expect(out.nodes[1].group?.steps[0].pos).toEqual({ x: 40, y: 0 });
    expect(out.nodes[1].group?.steps[1].pos).toBeUndefined();
  });
});

// --- What a dropped wire does (runWireIntent) --------------------------------------------

describe("running a dropped wire", () => {
  const deps = (over: Partial<Parameters<typeof W.runWireIntent>[2]> = {}) => ({
    flightOf: (k: string) => (k === "me" ? FA : k === "flight:f_b" ? FB : null),
    flights: [FA, FB],
    kinds: KINDS,
    chain: (k: string) => (k === "me" ? A : k === "flight:f_b" ? B : null),
    merge: vi.fn(),
    pinAt: vi.fn(),
    labelH: 34,
    ...over,
  });
  const at = { x: 0, y: 0 };
  const src: WireEnd = { chain: A.graph, node: A_NODES[0], side: "out" };

  it("move: the stop goes right after the tile the wire left", async () => {
    await W.runWireIntent({ op: "move", chain: "me", step: A_NODES[3].node, after: A_NODES[0].node }, { src, at }, deps());
    expect(routes.moveStop).toHaveBeenCalledWith("f_a", "s4", 1);
  });

  it("loop: a Loop right after the tile, back to the one dropped on", async () => {
    await W.runWireIntent({ op: "loop", chain: "me", from: A_NODES[1].node, to: A_NODES[0].node }, { src, at }, deps());
    const [fid, st, where] = routes.tackOn.mock.calls[0] as [string, FlightStep, number];
    expect(fid).toBe("f_a");
    expect(st).toMatchObject({ kind: "loop", config: { back_to: "s1" } });
    expect(where).toBe(2);
    expect(said).toHaveBeenCalledWith("Loops back to “stop:s1”");
  });

  it("merge: a stops-only route's stops spliced in right after the tile; attach: onto the session", async () => {
    const d = deps();
    await W.runWireIntent({ op: "merge", chain: "me", after: A_NODES[1].node, from: "flight:f_b" }, { src, at }, d);
    expect(d.merge).toHaveBeenCalledWith("flight:f_b", "me", 2);
    await W.runWireIntent({ op: "attach", session: "me", chain: "flight:f_b" }, { src, at }, d);
    expect(d.merge).toHaveBeenLastCalledWith("flight:f_b", "me");
  });

  it("handoff: a Start a route stop after the tile, aimed at the route, on a new session", async () => {
    await W.runWireIntent({ op: "handoff", chain: "me", from: A_NODES[1].node, target: "flight:f_b" }, { src, at }, deps());
    const [fid, st, where] = routes.tackOn.mock.calls[0] as [string, FlightStep, number];
    expect(fid).toBe("f_a");
    expect(st).toMatchObject({ kind: "handoff", config: { target: "flight:f_b", session: "new", wait: false } });
    expect(where).toBe(2);
    expect(said).toHaveBeenCalledWith("“stop:s2” starts “Release” on a new session");
  });

  it("handoff on a server without the kind says so and changes nothing", async () => {
    await W.runWireIntent({ op: "handoff", chain: "me", from: A_NODES[1].node, target: "flight:f_b" }, { src, at }, deps({ kinds: KINDS.slice(0, 2) }));
    expect(routes.tackOn).not.toHaveBeenCalled();
    expect(said).toHaveBeenCalledWith("This server has no “Start a route” stop yet");
  });

  it("palette: the route's palette opens for a stop after the tile, its tile centred on the drop (8 px grid)", async () => {
    const drop = { x: 100 + 300, y: 200 + 34 + 50 };
    await W.runWireIntent({ op: "palette", chain: "me", after: A_NODES[1].node, at: drop }, { src, at: drop }, deps());
    expect(A.paletteAfter).toHaveBeenCalledWith(A_NODES[1].node, { x: 232, y: 16 });
  });

  it("split: the tile and the stops after it leave into a route placed at the drop, its tiles kept relative to it", async () => {
    routes.splitOffChain.mockResolvedValueOnce({ id: "f_new", plan_name: "Fix (from Push)", steps: [step("s1", "agent", "Push", {}, { pos: { x: 0, y: 0 } }), step("s2", "agent", "Tell", { }, { pos: { x: 600, y: 40 } })] });
    const d = deps();
    const drop = { x: 500, y: 700 };
    await W.runWireIntent({ op: "split", chain: "me", step: A_NODES[2].node, at: drop }, { src: { chain: A.graph, node: A_NODES[2], side: "in" }, at: drop }, d);
    expect(routes.splitOffChain).toHaveBeenCalledWith("f_a", "s3", { x: 0, y: 0 });
    expect(d.pinAt).toHaveBeenCalledWith("flight:f_new", { x: 500 - 66, y: 700 - 32 - 34 });
    expect(routes.saveStopPositions).toHaveBeenCalledWith("f_new", { s2: { x: 150, y: 40 } });
    expect(said).toHaveBeenCalledWith("Split off “Fix (from Push)”");
  });

  it("none, or a route with no flight, does nothing", async () => {
    const d = deps();
    await W.runWireIntent({ op: "none" }, { src, at }, d);
    await W.runWireIntent({ op: "move", chain: "gone", step: A_NODES[3].node, after: null }, { src, at }, d);
    expect(routes.moveStop).not.toHaveBeenCalled();
    expect(d.merge).not.toHaveBeenCalled();
  });
});

// --- Spots saved per flight (lib/flightsApi) ----------------------------------------------

describe("node spots", () => {
  it("are batched: one POST …/layout per flight, a moment after the last drop", async () => {
    vi.useFakeTimers();
    const real = await vi.importActual<typeof import("../lib/flightsApi")>("../lib/flightsApi");
    const { api } = await import("../api/client");
    const done = vi.fn();
    real.queueStopPos("f_a", "s1", { x: 1.4, y: 2 }, done);
    real.queueStopPos("f_a", "s2", { x: 3, y: 4 });
    real.queueStopPos("f_b", "s1", null);
    expect(api).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(real.POS_BATCH_MS + 5);
    const calls = vi.mocked(api).mock.calls.filter((c) => String(c[0]).endsWith("/layout"));
    expect(calls.map((c) => c[0])).toEqual(["/api/flights/f_a/layout", "/api/flights/f_b/layout"]);
    expect(calls[0][1]).toEqual({ json: { positions: { s1: { x: 1, y: 2 }, s2: { x: 3, y: 4 } } } });
    expect(calls[1][1]).toEqual({ json: { positions: { s1: null } } });
    expect(done).toHaveBeenCalledWith(true);
  });

  it("a stop tacked on at a spot carries it", async () => {
    const real = await vi.importActual<typeof import("../lib/flightsApi")>("../lib/flightsApi");
    expect(real.wireStep(step("s1", "agent", "A", {}, { pos: { x: 8, y: 0 } }))).toMatchObject({ pos: { x: 8, y: 0 } });
    expect(real.wireStep(step("s1", "agent", "A"))).not.toHaveProperty("pos");
  });
});

// --- The Handoff's dialog (StopParts) -------------------------------------------------------

describe("the Handoff dialog's board-filled selects", () => {
  it("routes: the board's stops-only routes, then the saved stops; sessions: a new one, then each", () => {
    const plans = [{ id: "p_1", name: "Ticket to merge" }] as never;
    expect(SP.chainChoices([FA, FB], plans)).toEqual([
      { value: "flight:f_b", label: "Release", group: "On the Sky" },
      { value: "plan:p_1", label: "Ticket to merge", group: "Saved routes — a fresh copy each time" },
    ]);
    expect(SP.sessionChoices([{ title: "sc-1" }], (t) => t + "!")).toEqual([
      { value: "new", label: "a new session" },
      { value: "sc-1", label: "sc-1!" },
    ]);
    expect(SP.boardDynamic({ dynamic: "chains" as never })).toBe("chains");
    expect(SP.boardDynamic({ dynamic: "stops_before" })).toBe("");
  });

  it("draws as chips; a value no longer on the Sky stays visible", () => {
    const f = KINDS[2].fields[0];
    const html = renderToStaticMarkup(createElement(SP.StopField, { f, value: "flight:f_gone", onChange() {}, choices: SP.chainChoices([FB], []) }));
    expect(html).toContain('data-dynamic="chains"');
    expect(html).toContain(">Release<");
    expect(html).toContain("a route no longer on the Sky");
    const s = renderToStaticMarkup(createElement(SP.StopField, { f: KINDS[2].fields[1], value: "new", onChange() {}, choices: SP.sessionChoices([]) }));
    expect(s).toContain('data-dynamic="sessions"');
    expect(s).toContain(">a new session<");
  });
});

// --- The host's wiring, pinned in the source ------------------------------------------------

describe("the sky wires it", () => {
  const sky = code("components/flights/SkyView.tsx");
  const line = code("components/flights/SkyLine.tsx");
  const grounded = code("components/flights/GroundedLine.tsx");

  it("every route registers with the host, takes its port drags, lights its targets", () => {
    for (const src of [line, grounded]) {
      expect(src).toContain("useChainEntry(p.registerChain, graph, wrapRef, line.paletteAfter, line.paletteParallel);");
      expect(src).toContain("onPortDown={p.onPortDown}");
      expect(src).toContain("targets={p.targets}");
      expect(src).toContain("over={p.over}");
      expect(src).toContain("onNodeMove={moves.move}");
      expect(src).toContain("<ChainPanels graph={graph} anchor={line.anchor} anchorPos={line.anchorPos}>");
    }
    expect(sky.match(/registerChain=\{at \? chainReg\.registerFor\(id\) : undefined\}/g)?.length).toBe(2);
    expect(sky.match(/onPortDown=\{at \? portDownFor\(id\) : undefined\}/g)?.length).toBe(2);
    expect(sky).toContain("head={triggerHeads.get(f.id) || null}");
  });

  it("draws the wires between routes on the plane; a port press is the wire's in either tool", () => {
    expect(sky).toContain("<SkyWires band={wiring.band} wires={xwires} />");
    expect(sky).toContain('if (el?.closest(".sky-chain [data-port]")) return;');
    expect(sky).not.toContain("startConnect");
    expect(css("components/flights/Sky.css")).toMatch(/\.sky-xwires \{[^}]*pointer-events: none;/);
    expect(css("components/flights/Sky.css")).toMatch(/\.sky-xwire-path \{[^}]*stroke-dasharray:/);
  });

  it("the marquee takes tiles (not their ports); Delete finds a selected tile", () => {
    expect(sky).toContain('slot?.querySelectorAll(":is(.sky-node, .fl-box)[data-node]")');
    expect(sky).toContain(".sky-node.is-selected[data-node], .fl-box.selected[data-node]");
  });

  it("keeps the coach tour's hooks on the route markup", () => {
    expect(line).toContain('data-tour="sky-line-bar"');
    expect(line).toContain('".sky-head": "sky-session-box"');
    // Round 20: the head is the agent tile; a sub-session's frame is no stop.
    expect(line).toContain('".sky-node:not(.sky-head):not(.sky-subframe)": "sky-stop"');
    expect(grounded).toContain('data-tour="sky-grounded"');
    expect(sky).toContain('data-tour={l.flight ? undefined : "sky-line"}');
  });

  it("no “No session yet” box: a headless route, or the trigger tile of the route that starts it", () => {
    expect(grounded).not.toContain("No session yet");
    expect(grounded).toContain("const head = p.head || NO_HEAD;");
    expect(sky).toContain("triggerHeadFor(f, flights, windowName)");
  });
});
