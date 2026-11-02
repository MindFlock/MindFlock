/** Round 18 — SkyChain's markup (docs/flights.md "Round 18" → "What it looks
 * like"): tiles with the name and summary under them, ports, one SVG of
 * wires, the loop pill, the head tile, the empty socket, the trailing +,
 * the tile states and highlight rings. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Flight, FlightStep, StepRunState } from "../lib/flights";
import { collapseGroups, flightDiagram, type DiagramNode } from "../lib/flightDiagram";
import { groundedDiagram } from "../lib/groundedLines";
import { HEAD_ID, chainColor, chainGraph, type ChainGraph, type ChainHead } from "../lib/skyGraph";
import { SkyChain, tileLabel, tileSub, type SkyChainProps } from "../components/flights/SkyChain";
import { kindFamily, nodeFamily } from "../components/flights/StopIcons";

const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}, extra: Partial<FlightStep> = {}): FlightStep => ({ id, kind, title, config, on_fail: "halt", ...extra });
const rec = (state: StepRunState["state"], detail: Record<string, unknown> = {}): StepRunState => ({ state, since: 1, note: "", detail, rounds: 0 });

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

const LIVE = [
  step("s1", "agent", "Improve the sky"),
  step("s2", "command", "Screenshot", { command: "npm run shots" }),
  step("s3", "agent", "Judge it", {}, { halt: true }),
  step("s4", "loop", "Loop ×3", { back_to: "", times: 3 }),
  step("s5", "ship", "Push", { lane: "push" }),
];
const me: DiagramNode = { id: "session:lead", kind: "session", row: "main", title: "lead", sub: "", state: "session", icon: "session", session: "lead", chip: { label: "Working", tone: "working" } };
const sessionHead: ChainHead = { kind: "session", title: "lead", sub: "Opus · emandel/sky", session: "lead", color: chainColor("lead"), node: me };

const liveGraph = (): ChainGraph =>
  chainGraph(flightDiagram(flight(LIVE, 1, {}, { s1: rec("done", { receipt: "312 passed" }), s2: rec("running") })), { head: sessionHead, key: "lead", flightId: "f_live" });
const groundedGraph = (head: ChainHead = { kind: "none", title: "", sub: "", color: chainColor("") }): ChainGraph =>
  chainGraph(groundedDiagram(flight([step("g1", "agent", "Fix the bug"), step("g2", "checks", "Checks")], 0, { id: "f_g", state: "grounded" })), { head, key: "flight:f_g", flightId: "f_g" });

const render = (graph: ChainGraph, over: Partial<SkyChainProps> = {}) => renderToStaticMarkup(createElement(SkyChain, { graph, zoom: 1, ...over }));
const count = (html: string, needle: string) => html.split(needle).length - 1;

describe("SkyChain markup", () => {
  it("a list of tiles: the head, each stop with its name and summary under the tile", () => {
    const html = render(liveGraph());
    expect(html).toContain('role="list"');
    expect(html).toContain('data-chain="lead"');
    expect(count(html, 'role="listitem"')).toBe(5); // head + s1 s2 s3 s5 (the Loop is its wire)
    expect(html).toContain('data-node="head"');
    expect(html).toMatch(/class="sky-node-title">Judge it</);
    expect(html).toContain('class="sky-node-sub">npm run shots<');
    // A flown stop says its receipt.
    expect(html).toContain('class="sky-node-sub">312 passed<');
    expect(html).toContain('class="sky-tile"');
    expect(html).toMatch(/translate\(150px, 0px\)/);
  });

  it("tile states: done ✓ badge, running, a halt point's ⏸, family colours", () => {
    const html = render(liveGraph());
    expect(html).toMatch(/class="sky-node sky-node-stop st-done[^"]*"[^>]*data-node="stop:s1"/);
    expect(html).toContain("sky-badge sky-badge-done");
    expect(html).toMatch(/class="sky-node sky-node-stop st-current[^"]*"[^>]*data-node="stop:s2"/);
    expect(html).toContain("sky-badge sky-badge-halt");
    expect(html).toContain("sky-fam-ship");
    expect(html).toContain("sky-fam-work");
  });

  it("ports: data-port + data-node on live ones, passive ends where a wire lands on a fixed tile", () => {
    const html = render(liveGraph());
    expect(html).toContain('class="sky-port sky-port-in" data-port="in" data-node="stop:s3"');
    expect(html).toContain('class="sky-port sky-port-out" data-port="out" data-node="stop:s2"');
    expect(html).toContain('class="sky-port sky-port-out" data-port="out" data-node="head"');
    // s1 is done: its wire ends are drawn, not grabbed.
    expect(html).not.toContain('data-port="in" data-node="stop:s1"');
    expect(html).toContain('class="sky-port sky-port-in sky-port-off"');
    // The head has no in port.
    expect(html).not.toContain('data-port="in" data-node="head"');
  });

  it("wires in one SVG: head/line tinted with the session colour, the loop, the + stub", () => {
    const html = render(liveGraph());
    expect(count(html, "<svg class=\"sky-wires\"")).toBe(1);
    expect(html).toMatch(/class="sky-wire sky-wire-head"[^>]*data-from="head" data-to="stop:s1"[^>]*style="stroke:hsl/);
    expect(html).toMatch(/class="sky-wire sky-wire-line is-live"[^>]*data-to="stop:s2"/);
    expect(html).toContain('data-wire="loop"');
    expect(html).toContain('data-wire="add"');
  });

  it("the loop pill: a button for the Loop, screen-sized", () => {
    const html = render(liveGraph(), { selected: "stop:s4" });
    expect(html).toMatch(/<span role="button" tabindex="0" class="sky-loop-pill sky-loop-pending st-pending is-selected"[^>]*data-node="stop:s4"/);
    expect(html).toContain("↻ ×3");
  });

  it("the head: the agent tile with its session's colour, chip (or the host's renderHead), and the bird", () => {
    const html = render(liveGraph(), { bird: { nodeId: "session:lead", tone: "working" } });
    // Round 20: a session's head is its AGENT (the sparkle, `sky-agent`).
    expect(html).toMatch(/class="sky-node sky-head sky-head-session sky-agent st-session sky-fam-work tone-working/);
    expect(html).toMatch(/aria-label="Agent: lead — Working/);
    expect(html).toContain('data-agent=""');
    expect(html).toContain("--sky-head-color:hsl");
    expect(html).toContain('class="sky-head-dot"');
    expect(html).toContain('class="sky-head-chip tone-working">Working<');
    expect(html).toContain("fl-bird-marker tone-working");
    const own = render(liveGraph(), { renderHead: () => createElement("i", { className: "mine" }, "⋯") });
    expect(own).toContain('<i class="mine">⋯</i>');
    expect(own).not.toContain("sky-head-chip");
  });

  it("a headless chain: no head tile, an empty socket on its first node's in port", () => {
    const html = render(groundedGraph());
    expect(html).not.toContain('data-node="head"');
    expect(html).toMatch(/class="sky-socket"[^>]*data-port="in" data-node="stop:g1" data-socket=""/);
    expect(html).toContain("Wire a session&#x27;s out port here to run it");
    expect(html).toContain('data-head="none"');
  });

  it("a trigger head: dashed tile, the handoff icon", () => {
    const html = render(groundedGraph({ kind: "trigger", title: "When Iterate reaches Hand off", sub: "runs in a new session", session: "new", color: chainColor("new"), from: { flight: "f_live", step: "s9", title: "Hand off" } }));
    expect(html).toContain("sky-head sky-head-trigger");
    expect(html).toContain("When Iterate reaches Hand off");
    expect(html).not.toContain("sky-socket");
  });

  it("the trailing + after the last node", () => {
    const html = render(groundedGraph());
    expect(html).toMatch(/class="sky-plus fl-icon-btn"[^>]*data-port="out" data-node="stop:g2" data-plus=""/);
  });

  it("selection, highlight, targets and the one under the pointer", () => {
    const html = render(groundedGraph(), { selected: "stop:g1", highlighted: new Set(["stop:g2"]), targets: new Set(["stop:g1", "stop:g2"]), over: "stop:g2" });
    expect(html).toMatch(/st-pending[^"]*is-selected[^"]*is-target[^"]*"[^>]*data-node="stop:g1"/);
    expect(html).toMatch(/is-highlighted is-target is-over[^"]*"[^>]*data-node="stop:g2"/);
    expect(html).toContain('class="sky-socket is-target"');
    // The selected head by its diagram id.
    expect(render(liveGraph(), { selected: "session:lead" })).toMatch(/sky-head[^"]*is-selected/);
  });

  it("a collapsed group: the stack look and k of n", () => {
    const grp = { id: "gg", name: "Ship it", plan_id: "" };
    const steps = [step("a", "agent", "Work"), step("b", "checks", "Checks", {}, { group: grp }), step("c", "ship", "Push", {}, { group: grp })];
    const d = collapseGroups(flightDiagram(flight(steps, 2, {}, { a: rec("done"), b: rec("done"), c: rec("running") })));
    const g = chainGraph(d, { head: sessionHead, key: "lead", flightId: "f_live" });
    const html = render(g);
    expect(html).toContain("sky-node-group");
    expect(html).toContain("is-group");
    expect(html).toContain("1 of 2 · Push");
  });

  it("risk dots under the tile", () => {
    const html = render(chainGraph(groundedDiagram(flight([step("p", "ship", "Push", { lane: "push" })], 0, { id: "f_r", state: "grounded" }), [{ kind: "ship", label: "Ship", actor: "robot", summary: "", fields: [], default_title: "Push", risk: ["remote"] }]), { head: { kind: "none", title: "", sub: "", color: "var(--muted)" }, key: "flight:f_r", flightId: "f_r" }));
    expect(html).toMatch(/class="sky-risk"[^>]*data-risk="remote"/);
    expect(html).toContain('class="sky-risk-remote"');
  });

  it("labels and summaries", () => {
    const g = liveGraph();
    const s2 = g.nodes.find((n) => n.id === "stop:s2")!.node;
    expect(tileLabel(s2)).toBe("Screenshot — flying now, npm run shots");
    expect(tileLabel({ ...me, title: "lead", sub: "Opus" }, true)).toBe("lead — Working, Opus");
    expect(tileSub({ ...s2, receipt: "ok" })).toBe("ok");
    expect(g.headNode!.id).toBe(HEAD_ID);
  });

  it("families: the server's word when it names one, else ours", () => {
    expect(kindFamily("ship")).toBe("ship");
    expect(kindFamily("mystery")).toBe("outside");
    expect(kindFamily("loop")).toBe("flow");
    expect(kindFamily("agent", [{ kind: "agent", family: "people" }])).toBe("people");
    expect(nodeFamily({ kind: "stop", icon: "agent", perch: true })).toBe("people");
  });
});
