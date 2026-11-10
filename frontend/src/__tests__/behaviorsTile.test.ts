/** The Behaviors tile (docs/behaviors.md §1, §5): every session frame whose
 * branch has behaviors draws them as the LAST tile of its chain — after the
 * last stop, or right of the agent — or its Verify stop stands in for it;
 * the tile wears the summary and its tone, the frame carries the tone, a
 * click opens the BehaviorsCard in the tile's place, and a Verify stop parked
 * on you answers its behaviors in that card (no blanket Done). */

/// <reference types="vite/client" />
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code, css } from "./sourceText";
import type { Instance, TestPlan, TestStep } from "../api/types";
import type { Flight, FlightStep, StepRunState } from "../lib/flights";

const api = vi.fn((..._args: unknown[]): Promise<unknown> => new Promise(() => {}));
vi.mock("../api/client", () => ({ api: (...a: unknown[]) => api(...a), ApiError: class extends Error {} }));
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
const { BEHAVIORS_ID, flightDiagram, verifyStopOf, withBehaviors } = await import("../lib/flightDiagram");
const { BEHAVIORS_GAP, NODE_H, NODE_W, PITCH_X, chainColor, chainGraph } = await import("../lib/skyGraph");
const { behaviorSummary } = await import("../lib/behaviors");
const {
  BH_ASK_H,
  BH_ASK_W,
  BH_CARD_MAX_H,
  BH_CARD_MIN_H,
  BH_CARD_W,
  SkyChain,
  behaviorsCardBox,
  behaviorsFrameClass,
  behaviorsLabel,
  behaviorsMarkOf,
  requestNamesPlan,
} = await import("../components/flights/SkyChain");
const { YouStepActions, liveBoxParts } = await import("../components/flights/FlightStrip");
const { nodeFamily } = await import("../components/flights/StopIcons");

type Mark = NonNullable<ReturnType<typeof behaviorsMarkOf>>;
type DNode = import("../lib/flightDiagram").DiagramNode;
type Graph = import("../lib/skyGraph").ChainGraph;
type Head = import("../lib/skyGraph").ChainHead;

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

const tstep = (id: string, text: string, actor: TestStep["actor"] = "agent"): TestStep => ({ id, text, expect: "", actor });
function plan(over: Partial<TestPlan> = {}): TestPlan {
  return {
    id: "lead",
    title: "lead",
    repo_root: "/r",
    branch: "b",
    sha: "",
    live_branch: "main",
    effective_live_branch: "main",
    state: "generated",
    error: "",
    generated_at: 1,
    gen_started: 1,
    gen_attempts: 1,
    merged_at: 0,
    live_at: 0,
    merged_into: "",
    merged_into_at: 0,
    merged_into_all: [],
    summary: "",
    intent: "",
    focus: "",
    notified_at: 0,
    live_problem: "",
    steps: [tstep("t1", "Logging in lands on the dashboard"), tstep("t2", "A wrong password says so", "human")],
    runs: [],
    run_session: "",
    ...over,
  } as TestPlan;
}

const mark = (tone: Mark["tone"], short = "2 of 5 verified", status = "Not checked"): Mark => ({ planId: "lead", short, status, tone });
const me: DNode = { id: "session:lead", kind: "session", row: "main", title: "lead", sub: "", state: "session", icon: "session", session: "lead", chip: { label: "Working", tone: "working" } };
const head: Head = { kind: "session", title: "Claude", sub: "", session: "lead", color: chainColor("lead"), node: me };

const STOPS = [step("s1", "agent", "Improve the sky"), step("s2", "ship", "Push", { lane: "push" })];
const withMe = (d: ReturnType<typeof flightDiagram>) => ({ ...d, nodes: [me, ...d.nodes], edges: d.nodes.length ? [{ from: me.id, to: d.nodes[0].id, kind: "line" as const }, ...d.edges] : d.edges });
const render = (graph: Graph, over: Record<string, unknown> = {}) => renderToStaticMarkup(createElement(SkyChain, { graph, zoom: 1, ...over }));

describe("the diagram gets the tile (lib/flightDiagram withBehaviors)", () => {
  it("no behaviors: the diagram is left as it is", () => {
    const d = flightDiagram(flight(STOPS, 0));
    expect(withBehaviors(d, null)).toBe(d);
  });

  it("after the last stop, wired in with a plain line; derived (no step, no index, not editable)", () => {
    const d = withMe(flightDiagram(flight(STOPS, 0)));
    const out = withBehaviors(d, mark("quiet"));
    const main = out.nodes.filter((n) => n.row === "main" && n.kind !== "add");
    const tile = main[main.length - 1];
    expect(tile.id).toBe(BEHAVIORS_ID);
    expect(tile.kind).toBe("behaviors");
    expect(tile.title).toBe("Behaviors");
    expect(tile.sub).toBe("2 of 5 verified");
    expect(tile.step).toBeUndefined();
    expect(tile.index).toBeUndefined();
    expect(tile.editable).toBeFalsy();
    expect(out.edges).toContainEqual({ from: "stop:s2", to: BEHAVIORS_ID, kind: "line" });
    // The flight's own line is untouched.
    expect(out.edges.filter((e) => e.to !== BEHAVIORS_ID)).toEqual(d.edges);
  });

  it("no chain: right of the agent", () => {
    const d = { nodes: [me], edges: [], flightId: "", mode: "none" as const, insertFrom: 0 };
    const out = withBehaviors(d, mark("you"));
    expect(out.nodes.map((n) => n.id)).toEqual([me.id, BEHAVIORS_ID]);
    expect(out.edges).toEqual([{ from: me.id, to: BEHAVIORS_ID, kind: "line" }]);
    const g = chainGraph(out, { head, key: "lead", flightId: "" });
    expect(g.wires.find((w) => w.to === BEHAVIORS_ID)?.kind).toBe("head");
  });

  it("a trailing parallel group joins into it out of every member", () => {
    const par = { id: "p1" };
    const d = withMe(flightDiagram(flight([step("s1", "agent", "Build"), step("s2", "test", "Unit", {}, { parallel: par }), step("s3", "test", "Lint", {}, { parallel: par })], 0)));
    const out = withBehaviors(d, mark("quiet"));
    const into = out.edges.filter((e) => e.to === BEHAVIORS_ID);
    expect(into.map((e) => e.from).sort()).toEqual(["stop:s2", "stop:s3"]);
    expect(into.every((e) => e.kind === "join")).toBe(true);
  });

  it("the chain's Verify stop IS the tile: no second one, it wears the summary", () => {
    const d = withMe(flightDiagram(flight([step("s1", "agent", "Build"), step("s2", "verify", "Verify"), step("s3", "ship", "Push")], 0)));
    expect(verifyStopOf(d)?.id).toBe("stop:s2");
    const out = withBehaviors(d, mark("bad", "Failed · 1", "Failed"));
    expect(out.nodes.some((n) => n.kind === "behaviors")).toBe(false);
    expect(out.nodes.length).toBe(d.nodes.length);
    expect(out.nodes.find((n) => n.id === "stop:s2")?.behaviors).toEqual(mark("bad", "Failed · 1", "Failed"));
    expect(out.edges).toEqual(d.edges);
  });
});

describe("the chain lays it out (lib/skyGraph)", () => {
  const g = chainGraph(withBehaviors(withMe(flightDiagram(flight(STOPS, 0))), mark("quiet")), { head, key: "lead", flightId: "f_live" });
  const tile = g.nodes.find((n) => n.id === BEHAVIORS_ID)!;
  const push = g.nodes.find((n) => n.id === "stop:s2")!;

  it("the last tile, past the trailing + (which still adds a stop before it)", () => {
    expect(g.nodes[g.nodes.length - 1].id).toBe(BEHAVIORS_ID);
    expect(tile.x).toBe(push.x + PITCH_X + BEHAVIORS_GAP);
    expect(tile.y).toBe(push.y);
    expect(g.plus?.after).toBe("stop:s2");
  });

  it("never a wire's source or target (no live ports), so it can't be wired off", () => {
    expect(tile.inPort).toBe(false);
    expect(tile.outPort).toBe(false);
  });

  it("an open card takes its box, and the stops after a Verify stop's card make room", () => {
    const d = withBehaviors(withMe(flightDiagram(flight([step("s1", "verify", "Verify"), step("s2", "ship", "Push")], 0))), mark("you"));
    const box = behaviorsCardBox(plan());
    const open = chainGraph(d, { head, key: "lead", flightId: "f_live", big: { "stop:s1": box } });
    const v = open.nodes.find((n) => n.id === "stop:s1")!;
    const after = open.nodes.find((n) => n.id === "stop:s2")!;
    expect(v.w).toBe(BH_CARD_W);
    expect(after.x).toBeGreaterThanOrEqual(v.x + v.w);
    // Folded back: the old pitch.
    const shut = chainGraph(d, { head, key: "lead", flightId: "f_live" });
    expect(shut.nodes.find((n) => n.id === "stop:s2")!.x - shut.nodes.find((n) => n.id === "stop:s1")!.x).toBe(PITCH_X);
  });

  it("the card's box grows with its rows, within a cap; the delete question is small", () => {
    expect(behaviorsCardBox(plan({ steps: [] }))).toEqual({ w: BH_CARD_W, h: BH_CARD_MIN_H });
    expect(behaviorsCardBox(plan({ steps: Array.from({ length: 30 }, (_, i) => tstep("t" + i, "x")) })).h).toBe(BH_CARD_MAX_H);
    expect(BH_ASK_W).toBeLessThan(BH_CARD_W);
    expect(BH_ASK_H).toBeGreaterThan(NODE_H);
    expect(NODE_W).toBeLessThan(BH_CARD_W);
  });
});

describe("the tile's face (SkyChain)", () => {
  const graphWith = (m: Mark) => chainGraph(withBehaviors(withMe(flightDiagram(flight(STOPS, 0))), m), { head, key: "lead", flightId: "f_live" });
  const tileHtml = (html: string) => html.slice(html.indexOf('data-node="behaviors"') - 400);

  it("a ✓-shield, “Behaviors”, the summary, its tone class", () => {
    const html = render(graphWith(mark("you", "Needs you · 2", "Needs you")));
    expect(html).toContain("sky-node-behaviors");
    expect(html).toMatch(/class="[^"]*sky-behaviors is-you[^"]*"[^>]*data-node="behaviors"/);
    expect(html).toContain('data-icon="behaviors"');
    expect(html).toContain('<div class="sky-node-title">Behaviors</div>');
    expect(html).toContain('<div class="sky-node-sub">Needs you · 2</div>');
    expect(html).toContain('aria-label="Behaviors — Needs you, Needs you · 2"');
  });

  it.each(["quiet", "busy", "you", "bad", "ok"] as const)("tone %s → is-%s", (tone) => {
    expect(render(graphWith(mark(tone)))).toContain("sky-behaviors is-" + tone);
  });

  it("verified ✓ / failed ! on the tile itself", () => {
    expect(tileHtml(render(graphWith(mark("ok", "5 of 5 verified", "Verified"))))).toContain("sky-badge-done");
    expect(tileHtml(render(graphWith(mark("bad", "Failed · 1", "Failed"))))).toContain("sky-badge-failed");
    expect(tileHtml(render(graphWith(mark("quiet"))))).not.toContain("sky-badge-");
  });

  it("its ⋯ (a quiet icon button) only where the host has a menu; never draggable", () => {
    const g = graphWith(mark("quiet"));
    expect(render(g, { onMenu: () => {} })).toContain('class="fl-icon-btn sky-bh-more"');
    expect(render(g)).not.toContain("sky-bh-more");
    const html = render(g, { onNodeMove: () => {}, onMenu: () => {} });
    expect(html).not.toMatch(/class="[^"]*sky-behaviors[^"]*is-draggable/);
  });

  it("a Verify stop standing in for it shows the shield and the same summary", () => {
    const d = withBehaviors(withMe(flightDiagram(flight([step("s1", "agent", "Build"), step("s2", "verify", "Verify")], 1, { state: "waiting_you" }, { s1: rec("done") }))), mark("you", "Needs you · 1", "Needs you"));
    const html = render(chainGraph(d, { head, key: "lead", flightId: "f_live" }));
    expect(html).not.toContain('data-node="behaviors"');
    expect(html).toMatch(/class="[^"]*sky-node-stop[^"]*sky-behaviors is-you/);
    expect(html).toContain('data-icon="behaviors"');
    expect(html).toContain('<div class="sky-node-sub">Needs you · 1</div>');
    expect(html).toContain("Verify, the behaviors — Needs you, Needs you · 1");
  });

  it("opened, the card is drawn in the tile's place (renderOpen)", () => {
    const html = render(graphWith(mark("quiet")), { renderOpen: (n: DNode) => (n.behaviors ? createElement("div", { className: "bh-card-here" }, "card") : null) });
    expect(html).toMatch(/class="sky-node sky-node-open" data-node="behaviors"[^>]*><div class="bh-card-here">/);
  });

  it("is the people family (as Verify is)", () => {
    expect(nodeFamily({ kind: "behaviors", icon: "behaviors" })).toBe("people");
  });

  it("the label a screen reader hears", () => {
    expect(behaviorsLabel({ kind: "behaviors", title: "Behaviors" }, mark("quiet", "Not checked", "Not checked"))).toBe("Behaviors — Not checked");
  });
});

describe("the tile's words and the frame's tone", () => {
  it("the mark is behaviorSummary's, for this plan", () => {
    const p = plan();
    const s = behaviorSummary(p);
    expect(behaviorsMarkOf(p)).toEqual({ planId: "lead", short: s.short, status: s.status, tone: s.tone });
    expect(behaviorsMarkOf(null)).toBeNull();
  });

  it("the frame: red top edge when failed, the Needs-you mark when one waits on you", () => {
    expect(behaviorsFrameClass(mark("bad"))).toBe("is-bh-bad");
    expect(behaviorsFrameClass(mark("you"))).toBe("is-bh-you");
    expect(behaviorsFrameClass(mark("ok"))).toBe("");
    expect(behaviorsFrameClass(null)).toBe("");
    const sc = css("components/flights/SkyChain.css");
    expect(sc).toMatch(/\.skl-chainline\.skl-frame\.is-bh-bad > \.skl-frame-box \{[^}]*var\(--red/);
    expect(sc).toMatch(/\.fl-chain-view\.is-bh-bad > \.fl-chain-frame \{[^}]*var\(--red/);
    const line = code("components/flights/SkyLine.tsx");
    expect(line).toContain('behaviors.mark?.tone === "you" && ss.tone !== "attention"');
    expect(line).toContain("(behaviors.frameClass ? \" \" + behaviors.frameClass : \"\")");
  });
});

describe("a click opens the card in the tile's place", () => {
  it("SkyChain sends a behaviors tile's press to onOpen (not select)", () => {
    expect(code("components/flights/SkyChain.tsx")).toContain("if (n.id !== HEAD_ID && n.node.behaviors) p.onOpen?.(n.node);");
  });

  it("both homes (the Sky's frame, the grid's Flight tab) route the tile through the one hook", () => {
    const sl = code("components/flights/useSessionLine.tsx");
    expect(sl).toContain("const behaviors = useBehaviorsTile(title, row, { flight, listen: !!opts.listenBehaviors });");
    expect(sl).toContain("const graphDiagram = useMemo(() => behaviors.apply(line), [behaviors.apply, line]);");
    for (const f of ["components/flights/SkyLine.tsx", "components/flights/FlightChain.tsx"]) {
      const src = code(f);
      expect(src, f).toContain("const behaviors = line.behaviors;");
      expect(src, f).toContain("if (behaviors.onOpen(n)) return;");
      expect(src, f).toContain("if (behaviors.onMenu(n, at)) return;");
      expect(src, f).toContain("...behaviors.big(moves.diagram)");
      expect(src, f).toContain("{behaviors.menu}");
    }
    expect(code("components/flights/SkyLine.tsx")).toContain("listenBehaviors: true");
    expect(code("components/flights/FlightChain.tsx")).toContain("renderOpen={behaviors.renderOpen}");
  });

  it("records the open plan, folds back on Esc / ×, and never takes the focus request (SkyView does)", () => {
    const src = code("components/flights/SkyChain.tsx");
    expect(src).toContain("if (mode === \"card\") st.setOpenPlan(planId);");
    expect(src).toContain('if (e.key !== "Escape") return;');
    expect(src).toContain("<BehaviorsCard plan={plan} onClose={close} flightStep={flightStep(n)} />");
    expect(src).not.toMatch(/\.take\(\)/);
    expect(src).toContain('label: "Delete these behaviors"');
  });

  const plans = [plan(), plan({ id: "other", title: "other", branch: "feature/x" }), plan({ id: "due", title: "due", state: "due" })];
  it.each([
    [{ planId: "lead" }, true],
    [{ title: "lead" }, true],
    [{ branch: "b" }, true],
    [{ planId: "other" }, false],
    [{ branch: "feature/x" }, false],
    [{ board: true }, false],
  ])("a focus request %j names this session's behaviors: %s", (req, yes) => {
    expect(requestNamesPlan(plans, req, plans[0], "lead")).toBe(yes);
  });

  it("Alt+V's next opens the one that needs you", () => {
    expect(requestNamesPlan(plans, { next: true }, plans[2], "due")).toBe(true);
    expect(requestNamesPlan(plans, { next: true }, plans[0], "lead")).toBe(false);
  });
});

describe("the Verify stop parked on you (docs/behaviors.md §5)", () => {
  const VERIFY = [step("s1", "agent", "Build"), step("s2", "verify", "Verify", { who: "you" })];
  const parked = flight(VERIFY, 1, { state: "waiting_you" }, { s1: rec("done"), s2: rec("running") });
  const ROW = { title: "lead", branch: "b", repo: "r" } as Instance;
  function withClient(el: ReactElement, plans: TestPlan[] | null) {
    const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
    c.setQueryData(["instances"], [ROW]);
    if (plans) c.setQueryData(["test-plans"], { plans, live_branch: "main" });
    return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
  }

  it("answers its behaviors in the Behaviors card — no blanket Done", () => {
    const html = withClient(createElement(YouStepActions, { flight: parked }), [plan()]);
    expect(html).toContain("fl-you-verify");
    expect(html).toContain("Logging in lands on the dashboard");
    expect(html).not.toContain("fl-done");
    expect(html).not.toContain(">Done<");
  });

  it("the pane's one-line strip: a way to the card, and Skip", () => {
    const html = withClient(createElement(YouStepActions, { flight: parked, compact: true }), [plan()]);
    expect(html).toContain('class="btn-primary fl-btn fl-behaviors"');
    expect(html).toContain(">Verify</button>");
    expect(html).toContain(">Skip</button>");
    expect(html).not.toContain("fl-done");
  });

  it("before they are written: what it waits for, and Skip", () => {
    const html = withClient(createElement(YouStepActions, { flight: parked }), []);
    expect(html).toContain("Its agent is writing its verify steps…");
    expect(html).toContain(">Skip</button>");
    expect(html).not.toContain("fl-done");
  });

  it("a Wait-for-me stop keeps its own Done", () => {
    const perch = flight([step("s1", "you", "Look at it", { items: ["the page"] })], 0, { state: "waiting_you" });
    expect(withClient(createElement(YouStepActions, { flight: perch }), [plan()])).toContain("fl-done");
  });

  it("on the chain the stop stays a tile (its card opens in place); elsewhere its box makes room for the card", () => {
    const node = { kind: "stop", index: 1, state: "perched" } as DNode;
    const inPlace = liveBoxParts(parked, true, () => {}, { verifyInPlace: true });
    const boxed = liveBoxParts(parked, true, () => {});
    expect(inPlace.sizeOf(node, { w: 132, h: 140 })).toEqual({ w: 132, h: 140 });
    expect(boxed.sizeOf(node, { w: 132, h: 140 }).w).toBeGreaterThanOrEqual(320);
    expect(code("components/flights/useSessionLine.tsx")).toContain("liveBoxParts(flight, true, cancel.ask, { verifyInPlace: true, inTile: true })");
  });
});
