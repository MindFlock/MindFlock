/** Return wires explain themselves (docs/vision/fixwire.md): a stop that may
 * send work back to the agent (the server's `returns`, its `detail.fix`)
 * draws a return wire — an edge `fix` from it back to the Agent stop before
 * it or the session tile, an arrowhead, its case on the wire; lit with the
 * real reason and round while the agent fixes it; stacked with Loop wires so
 * labels never overlap. Pure (vitest env is node). */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Flight, FlightReturn, FlightStep, StepRunState } from "../lib/flights";
import { birdPerch, fixFace, fixOf, flightDiagram, loopCaption, loopIdlePill, returnEdges, returnLabel, returnMark, returnTitle, type DiagramNode } from "../lib/flightDiagram";
import { HEAD_ID, chainColor, chainGraph, fitLabel, inPortOf, labelWidth, outPortOf, wireLabel, type ChainHead } from "../lib/skyGraph";
import { SkyChain } from "../components/flights/SkyChain";

const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}, extra: Partial<FlightStep> = {}): FlightStep => ({ id, kind, title, config, on_fail: "halt", ...extra });
const rec = (state: StepRunState["state"], detail: Record<string, unknown> = {}): StepRunState => ({ state, since: 1, note: "", detail, rounds: 0 });

function flight(steps: FlightStep[], cursor: number, states: Record<string, StepRunState> = {}, returns: Record<string, FlightReturn> = {}): Flight {
  return {
    id: "f_1",
    plan_id: "p",
    plan_name: "To an open PR",
    steps,
    subject: { kind: "manual", source: "", id: "", title: "", url: "" },
    session: "sc-25250",
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
    returns,
  };
}

// The owner's route: Agent works the ticket → Commit → Push → Open PR.
const ROUTE = [step("s1", "agent", "Work the ticket"), step("s2", "ship", "Commit", { lane: "commit" }), step("s3", "ship", "Push", { lane: "push" }), step("s4", "ship", "Open PR", { lane: "pr" })];
const RETURNS: Record<string, FlightReturn> = {
  s2: { to: "s1", case: "if pre-commit fails", of: 2, kinds: ["precommit"] },
  s3: { to: "s1", case: "if the checks fail", of: 2, kinds: ["check"] },
};
const FIXING = { active: true, round: 1, rounds: 1, of: 2, reason: "pre-commit failed at ruff", since: 10, to: "s1", kind: "precommit" };

const me: DiagramNode = { id: "session:sc-25250", kind: "session", row: "main", title: "sc-25250", sub: "", state: "session", icon: "session", session: "sc-25250", chip: { label: "Working", tone: "working" } };
const head: ChainHead = { kind: "session", title: "sc-25250", sub: "", session: "sc-25250", color: chainColor("sc-25250"), node: me };

describe("the fix state and the words", () => {
  it("reads detail.fix defensively", () => {
    expect(fixOf({})).toBeUndefined();
    expect(fixOf({ fix: "x" })).toBeUndefined();
    expect(fixOf({ fix: { active: 1, round: 2, of: null, reason: "red", to: "" } })).toMatchObject({ active: true, round: 2, of: null, to: "agent" });
  });

  it("quiet: the case; fixing: the real reason and round; then fixed / halted", () => {
    const fix = fixOf({ fix: FIXING })!;
    const quiet = returnMark(RETURNS.s2, undefined, "pending", false)!;
    expect(quiet.shown).toBe("case");
    expect(returnLabel(quiet)).toBe("if pre-commit fails");
    expect(returnTitle(quiet)).toContain("If it fails: Let the agent fix it");
    const lit = returnMark(RETURNS.s2, fix, "current", false)!;
    expect(lit.shown).toBe("fixing");
    expect(returnLabel(lit)).toBe("pre-commit failed at ruff · fixing 1/2");
    expect(returnTitle(lit)).toBe("pre-commit failed at ruff — the agent is fixing it (round 1 of 2)");
    expect(fixFace(fix)).toBe("Agent fixing · round 1");
    const uncapped = returnMark({ ...RETURNS.s2, of: null }, fix, "current", false)!;
    expect(returnLabel(uncapped)).toBe("pre-commit failed at ruff · fixing round 1");
    const done = { ...fix, active: false, outcome: "fixed" as const };
    expect(returnLabel(returnMark(RETURNS.s2, done, "done", false)!)).toBe("fixed after 1 round");
    // Once the route is past the next stop it goes quiet again.
    expect(returnMark(RETURNS.s2, done, "done", true)!.shown).toBe("case");
    const out = { ...fix, active: false, rounds: 2, outcome: "halted" as const };
    expect(returnLabel(returnMark(RETURNS.s2, out, "halted", false)!)).toBe("halted after 2 rounds");
  });

  it("no returns entry and no fix: no mark", () => {
    expect(returnMark(undefined, undefined, "pending", false)).toBeUndefined();
  });

  it("a Loop's pill says its case in words; its caption what the count does not", () => {
    const via = { step: step("l", "loop", "L", { times: 3, every_seconds: 1800 }) };
    expect(loopIdlePill({ runs: 0, times: 3, state: "pending" }, via)).toBe("↻ repeat ×3 · every 30 min");
    expect(loopCaption({ runs: 0, times: 3, state: "pending" }, via)).toBe("");
    expect(loopCaption({ runs: 1, times: 3, state: "running" }, via)).toBe("every 30 min");
    expect(loopCaption({ runs: 1, times: 0, state: "running" }, { step: step("l", "loop", "L", { until: "checks green" }) })).toBe("until checks green");
  });
});

describe("the edges", () => {
  it("each stop with a return gets a fix edge back to the Agent stop before it", () => {
    const d = flightDiagram(flight(ROUTE, 1, { s1: rec("done") }, RETURNS));
    const fix = d.edges.filter((e) => e.kind === "fix");
    expect(fix.map((e) => [e.from, e.to])).toEqual([
      ["stop:s2", "stop:s1"],
      ["stop:s3", "stop:s1"],
    ]);
    expect(fix[0].ret?.case).toBe("if pre-commit fails");
    // Open PR hands nothing back: no wire.
    expect(d.edges.some((e) => e.kind === "fix" && e.from === "stop:s4")).toBe(false);
  });

  it("back to the session's agent goes to the head; with no head, none", () => {
    const n: DiagramNode = { id: "stop:c", kind: "stop", row: "main", title: "Checks", sub: "", state: "current", icon: "checks", step: step("c", "checks", "Checks"), ret: { to: "agent", case: "if CI checks fail", of: 2, shown: "case" } };
    expect(returnEdges([me, n], me.id)).toEqual([{ from: "stop:c", to: me.id, kind: "fix", ret: n.ret }]);
    expect(returnEdges([n])).toEqual([]);
  });

  it("the fixing stop's face and its bird: the bird sits on the agent it went back to", () => {
    const d = flightDiagram(flight(ROUTE, 1, { s1: rec("done"), s2: rec("running", { fix: FIXING }) }, RETURNS));
    const commit = d.nodes.find((n) => n.id === "stop:s2")!;
    expect(commit.sub).toBe("Agent fixing · round 1");
    expect(commit.ret?.shown).toBe("fixing");
    expect(birdPerch(d)).toEqual({ nodeId: "stop:s1", tone: "working" });
  });

  it("without a fix the bird stays on the stop", () => {
    const d = flightDiagram(flight(ROUTE, 1, { s1: rec("done"), s2: rec("running") }, RETURNS));
    expect(birdPerch(d)?.nodeId).toBe("stop:s2");
  });
});

describe("the wires on the Sky", () => {
  const graph = (states: Record<string, StepRunState>, steps = ROUTE, returns = RETURNS) => chainGraph(flightDiagram(flight(steps, 1, states, returns)), { head, key: "sc-25250", flightId: "f_1" });

  it("a fix wire runs under the route with an arrowhead into the stop it goes back to", () => {
    const g = graph({ s1: rec("done") });
    const w = g.wires.find((x) => x.id === "fix:stop:s2>stop:s1")!;
    expect(w.kind).toBe("fix");
    const s1 = g.nodes.find((n) => n.id === "stop:s1")!;
    expect(w.mid.y).toBeGreaterThan(s1.y + s1.h);
    expect(w.arrow).toMatch(/^M .* Z$/);
    expect(w.label?.text).toBe("if pre-commit fails");
  });

  it("two return wires on overlapping spans stack a lane apart, and so do their labels", () => {
    const g = graph({ s1: rec("done") });
    const a = g.wires.find((x) => x.id === "fix:stop:s2>stop:s1")!;
    const b = g.wires.find((x) => x.id === "fix:stop:s3>stop:s1")!;
    expect(a.lane).not.toBe(b.lane);
    expect(Math.abs(a.mid.y - b.mid.y)).toBeGreaterThanOrEqual(16);
    expect(Math.abs(a.label!.at.y - b.label!.at.y)).toBeGreaterThanOrEqual(16);
  });

  it("a fix wire and a Loop on the same span stack too", () => {
    const steps = [ROUTE[0], ROUTE[1], step("lp", "loop", "Loop", { back_to: "s1", times: 3 }), ROUTE[2]];
    const g = graph({ s1: rec("done") }, steps, { s2: RETURNS.s2 });
    const fix = g.wires.find((x) => x.kind === "fix")!;
    const loop = g.wires.find((x) => x.kind === "loop")!;
    expect([fix.from, fix.to]).toEqual([loop.from, loop.to]);
    expect(fix.lane).not.toBe(loop.lane);
    expect(loop.arrow).toBeTruthy();
  });

  it("labels stay inside their run: cut, or dropped when nothing fits", () => {
    expect(fitLabel("if pre-commit fails", 1000)).toBe("if pre-commit fails");
    const cut = fitLabel("pre-commit failed at a very long hook name · fixing 1/2", 120);
    expect(cut.endsWith("…")).toBe(true);
    expect(labelWidth(cut)).toBeLessThanOrEqual(120);
    expect(fitLabel("if pre-commit fails", 40)).toBe("");
    const ret = returnMark(RETURNS.s2, undefined, "pending", false)!;
    expect(wireLabel({ kind: "fix", mid: { x: 0, y: 0 }, ret }, 30, 0)).toBeNull();
    const l = wireLabel({ kind: "fix", mid: { x: 150, y: 200 }, ret }, 300, 0)!;
    expect(l.at.x - l.w / 2).toBeGreaterThanOrEqual(0);
    expect(l.at.x + l.w / 2).toBeLessThanOrEqual(300);
  });

  it("the markup: a lit wire, its label with the reason, the fixing tile", () => {
    const g = graph({ s1: rec("done"), s2: rec("running", { fix: FIXING }) });
    const html = renderToStaticMarkup(createElement(SkyChain, { graph: g, zoom: 1, bird: { nodeId: "stop:s1", tone: "working" } }));
    expect(html).toContain('class="sky-wire sky-wire-fix sky-ret-fixing"');
    expect(html).toContain("sky-wire-arrow");
    expect(html).toMatch(/class="sky-ret-label sky-ret-label-fix sky-ret-fixing"[^>]*title="pre-commit failed at ruff — the agent is fixing it \(round 1 of 2\)"[^>]*>pre-commit…? ?[^<]*· fixing 1\/2</);
    // Between neighbours the run is short: the reason gives way, the round stays whole.
    const w = g.wires.find((x) => x.id === "fix:stop:s2>stop:s1")!;
    expect(w.label!.text.endsWith(" · fixing 1/2")).toBe(true);
    const at = (id: string) => g.nodes.find((n) => n.id === id)!;
    expect(w.label!.at.x - w.label!.w / 2).toBeGreaterThanOrEqual(inPortOf(at("stop:s1")).x);
    expect(w.label!.at.x + w.label!.w / 2).toBeLessThanOrEqual(outPortOf(at("stop:s2")).x);
    expect(html).toMatch(/class="sky-node sky-node-stop st-current[^"]*is-fixing/);
    expect(html).toContain("Agent fixing · round 1");
  });

  it("back to the session's agent: the wire lands on the head", () => {
    const steps = [step("c", "checks", "CI checks", {}, { on_fail: "agent" })];
    const f = flight(steps, 0, { c: rec("running", { fix: { ...FIXING, to: "agent", reason: "CI failed: lint" } }) }, { c: { to: "agent", case: "if CI checks fail", of: 2 } });
    const d = flightDiagram(f);
    // flightDiagram has no session box: nothing to go back to on its own…
    expect(d.edges.some((e) => e.kind === "fix")).toBe(false);
    // …but under a session head the edge lands on it.
    const e = returnEdges([me, ...d.nodes], me.id);
    expect(e.map((x) => x.to)).toEqual([me.id]);
    const g = chainGraph({ ...d, nodes: [me, ...d.nodes], edges: [...d.edges, ...e] }, { head, key: "s", flightId: "f_1" });
    expect(g.wires.find((w) => w.kind === "fix")?.to).toBe(HEAD_ID);
  });
});

describe("done by the agent", () => {
  it("a Ship stop the agent did itself says so on its face", () => {
    const states = {
      s1: rec("done"),
      s2: rec("done", { by: "agent", receipt: "already committed · 3 commits" }),
      s3: rec("done", { by: "agent", receipt: "already pushed" }),
      s4: rec("done", { by: "agent", receipt: "PR #123", url: "https://github.com/o/r/pull/123" }),
    };
    const f = { ...flight(ROUTE, 4, states), state: "done" as const };
    const d = flightDiagram(f);
    const by = d.nodes.filter((n) => n.by === "agent").map((n) => n.id);
    expect(by).toEqual(["stop:s2", "stop:s3", "stop:s4"]);
    const g = chainGraph(d, { head, key: "sc-25355", flightId: "f_1" });
    const html = renderToStaticMarkup(createElement(SkyChain, { graph: g, zoom: 1 }));
    expect(html.split('class="sky-node-by"').length - 1).toBe(3);
    expect(html).toContain("already committed · 3 commits");
    expect(html).toMatch(/aria-label="Commit — done by the agent, already committed · 3 commits"/);
  });

  it("a stop the lane did has no by-line", () => {
    const d = flightDiagram(flight(ROUTE, 2, { s1: rec("done"), s2: rec("done", { receipt: "committed" }) }));
    expect(d.nodes.find((n) => n.id === "stop:s2")?.by).toBeUndefined();
  });
});
