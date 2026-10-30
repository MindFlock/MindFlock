/** Fan-out and join (docs/flights-branches.md): children spawned DURING a
 * stop of the parent's flight (their row's `flight.branch`) are drawn as
 * parallel branches between that stop and the next box, which is the join
 * point ("waiting for 2 of 5"); the rest stay below the session. The model
 * (lib/flightDiagram), its layout, the canvas's markup (renderToStaticMarkup)
 * and the line's Split / Join editing. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { code, css } from "./sourceText";
import type { Instance } from "../api/types";
import type { Flight, FlightStep, StepKindDef } from "../lib/flights";
import { birdPerch, birdPerches, branchState, innerSummary, joinLabel, layoutDiagram, reorderDiagram, sessionDiagram, stopSummary, type Diagram } from "../lib/flightDiagram";
import { FlywayCanvas } from "../components/flights/FlywayCanvas";
import { StopPalette } from "../components/flights/StopParts";
import { inspectorDef, promptLines } from "../components/flights/useSessionLine";
import { insertGaps } from "../lib/stopEditing";

const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}): FlightStep => ({ id, kind, title, config, on_fail: "halt" });

const KINDS: StepKindDef[] = [
  { kind: "agent", label: "Agent", actor: "robot", summary: "The agent works", fields: [{ name: "join", type: "bool", label: "Wait for its workers", default: true }], default_title: "Agent works" },
  {
    kind: "split",
    label: "Split",
    actor: "robot",
    summary: "Workers in parallel",
    fields: [
      { name: "prompts", type: "textarea", label: "Prompts (one per line)" },
      { name: "count", type: "int", label: "Count", default: 1 },
      { name: "model", type: "text", label: "Model" },
      { name: "fence_only", type: "text", label: "Fence" },
      { name: "report_back", type: "bool", label: "Report back", default: true },
    ],
    default_title: "Split",
  },
  {
    kind: "join",
    label: "Join",
    actor: "robot",
    summary: "Waits for every worker",
    fields: [
      { name: "of", type: "select", label: "Of", options: ["all", "spawned"], default: "all" },
      { name: "on_failed", type: "select", label: "If one fails", options: ["halt", "continue"], default: "halt" },
    ],
    default_title: "Join",
  },
  { kind: "ship", label: "Ship", actor: "robot", summary: "Ship", fields: [], default_title: "Push" },
  { kind: "you", label: "Perch", actor: "you", summary: "Waits for you", fields: [], default_title: "Check it" },
];

/** The agent-with-join line: s1 (the agent fans out) → s2 → s3. */
const AGENT_STEPS = [step("s1", "agent", "Fan out the work", { join: true }), step("s2", "ship", "Push", { lane: "push" }), step("s3", "you", "Approve")];
/** The split → join line. */
const SPLIT_STEPS = [step("s1", "split", "Split", { prompts: "a\nb\nc" }), step("s2", "join", "Join"), step("s3", "ship", "Push", { lane: "push" })];

type Kid = { title: string; state: string; report?: string };

function flight(steps: FlightStep[], cursor: number, kids: Kid[] = []): Flight {
  const cur = steps[cursor];
  return {
    id: "f_1",
    plan_id: "p",
    plan_name: "Fan",
    steps,
    subject: { kind: "manual", source: "", id: "", title: "", url: "" },
    session: "lead",
    repo_root: "/r",
    branch: "b",
    state: "running",
    cursor,
    step_states: { [cur.id]: { state: "running", since: 1, note: "", detail: kids.length ? { waiting: 0, total: kids.length, children: kids } : {}, rounds: 0 } },
    reason: "",
    history: [],
    started: 1,
    updated: 1,
    finished: 0,
    by: "",
  };
}

const row = (title: string, over: Record<string, unknown> = {}) =>
  ({ title, status: "running", activity: "idle", stage: "agent", task_headline: "Do " + title, parent: "", branch: title, flight: null, autopilot: null, ...over }) as unknown as Instance;

function lead(steps: FlightStep[], cursor: number, join: { waiting: number; total: number } | null) {
  const s = steps[cursor];
  return row("lead", {
    activity: "working",
    flight: { id: "f_1", plan_name: "Fan", cursor, total: steps.length, state: "running", step: { id: s.id, kind: s.kind, title: s.title, state: "running", actor: "robot" }, join },
  });
}

const kid = (title: string, stepId: string, over: Record<string, unknown> = {}) => row(title, { parent: "lead", flight: { branch: { parent: "lead", step: stepId } }, ...over });

const FIVE = [
  kid("w1", "s1", { activity: "working" }),
  kid("w2", "s1", { activity: "working" }),
  kid("w3", "s1", { last_report: { id: "m3", status: "done", summary: "Fixed the parser", ts: 1 } }),
  kid("w4", "s1", { last_report: { id: "m4", status: "done", summary: "Tests green", ts: 1 } }),
  kid("w5", "s1", { last_report: { id: "m5", status: "done", summary: "Docs", ts: 1 } }),
];

const diagramOf = (rows: Instance[], f: Flight) => sessionDiagram(rows, "lead", { flights: [f], kinds: KINDS });
const ids = (d: Diagram, kind: string) => d.nodes.filter((n) => n.kind === kind).map((n) => n.id);

describe("branches in the session diagram", () => {
  const f = flight(AGENT_STEPS, 0);
  const d = diagramOf([lead(AGENT_STEPS, 0, { waiting: 2, total: 5 }), ...FIVE], f);

  it("children spawned at a stop become branches between it and the next stop", () => {
    expect(ids(d, "branch")).toEqual(["branch:w1", "branch:w2", "branch:w3", "branch:w4", "branch:w5"]);
    for (const n of d.nodes.filter((x) => x.kind === "branch")) {
      expect(n.row).toBe("branch");
      expect(n.branch?.from).toBe("stop:s1");
      expect(n.branch?.to).toBe("stop:s2");
      expect(n.session).toBe(n.title);
    }
    // Drawn as branches, so not ALSO below.
    expect(ids(d, "child")).toEqual([]);
  });

  it("fans out and back in: 2 × children edges, and the stop reaches the next one through them", () => {
    const fan = d.edges.filter((e) => e.kind === "fan");
    const join = d.edges.filter((e) => e.kind === "join");
    expect(fan.length + join.length).toBe(2 * 5);
    expect(fan.every((e) => e.from === "stop:s1")).toBe(true);
    expect(join.every((e) => e.to === "stop:s2")).toBe(true);
    expect(d.edges.some((e) => e.from === "stop:s1" && e.to === "stop:s2")).toBe(false);
    // The rest of the line is untouched.
    expect(d.edges).toContainEqual({ from: "session:lead", to: "stop:s1", kind: "line" });
    expect(d.edges).toContainEqual({ from: "stop:s2", to: "stop:s3", kind: "line" });
  });

  it("the join point is the next stop and reads “waiting for 2 of 5”", () => {
    const point = d.nodes.find((n) => n.id === "stop:s2")!;
    expect(point.join).toEqual({ waiting: 2, total: 5 });
    expect(joinLabel(point.join!)).toBe("waiting for 2 of 5");
    expect(d.nodes.find((n) => n.id === "stop:s1")!.join).toBeUndefined();
  });

  it("each branch's state: working, done from its report; a done branch's fan-in draws done", () => {
    const st = Object.fromEntries(d.nodes.filter((n) => n.kind === "branch").map((n) => [n.title, n.branch!.state]));
    expect(st).toEqual({ w1: "working", w2: "working", w3: "done", w4: "done", w5: "done" });
    expect(d.nodes.find((n) => n.id === "branch:w1")!.chip).toEqual({ label: "Working", tone: "working" });
    expect(d.nodes.find((n) => n.id === "branch:w3")!.state).toBe("done");
    const done = d.edges.filter((e) => e.kind === "join" && e.done).map((e) => e.from);
    expect(done).toEqual(["branch:w3", "branch:w4", "branch:w5"]);
  });

  it("when nobody is outstanding (waiting 0) every fan-in draws done", () => {
    const d0 = diagramOf([lead(AGENT_STEPS, 0, { waiting: 0, total: 5 }), ...FIVE], f);
    expect(d0.edges.filter((e) => e.kind === "join").every((e) => e.done)).toBe(true);
  });

  it("the bird waits on the join point while it is outstanding; an asking branch turns it gold", () => {
    expect(birdPerch(d)).toEqual({ nodeId: "stop:s2", tone: "working" });
    const asking = [...FIVE.slice(0, 4), kid("w5", "s1", { activity: "clarify" })];
    const da = diagramOf([lead(AGENT_STEPS, 0, { waiting: 3, total: 5 }), ...asking], f);
    expect(da.nodes.find((n) => n.id === "branch:w5")!.branch!.state).toBe("asking");
    expect(da.nodes.find((n) => n.id === "branch:w5")!.state).toBe("perched");
    expect(birdPerch(da)).toEqual({ nodeId: "stop:s2", tone: "you" });
    // Nothing outstanding: back on the current stop.
    expect(birdPerch(diagramOf([lead(AGENT_STEPS, 0, { waiting: 0, total: 5 }), ...FIVE], f))).toEqual({ nodeId: "stop:s1", tone: "working" });
    // A halted flight whose lead is busy: the bird follows the work, on the
    // session box (round 12 — "why is the bird on Commit when the agent is
    // running"); the stop keeps its red state on its own.
    const halted = diagramOf([lead(AGENT_STEPS, 0, { waiting: 2, total: 5 }), ...FIVE], { ...f, state: "halted" });
    expect(birdPerch(halted)).toEqual({ nodeId: "session:lead", tone: "working" });
  });

  it("children with no branch, or one spawned at a stop not in this flight, hang in parallel off the current stop", () => {
    const rows = [lead(AGENT_STEPS, 0, null), kid("w1", "s1"), row("plain", { parent: "lead" }), kid("old", "zz"), kid("moved", "s1", { flight: { branch: { parent: "someone-else", step: "s1" } } })];
    const db = diagramOf(rows, f);
    expect(ids(db, "branch")).toEqual(["branch:w1"]);
    expect(ids(db, "child")).toEqual(["child:plain", "child:old", "child:moved"]);
    // Round 12: off the flight's current stop, into the box after it — the
    // worker's column, one row each.
    expect(db.edges.filter((e) => e.kind === "branch").map((e) => [e.from, e.to])).toEqual([
      ["stop:s1", "child:plain"],
      ["stop:s1", "child:old"],
      ["stop:s1", "child:moved"],
    ]);
    expect(db.edges.filter((e) => e.kind === "join" && e.from.startsWith("child:")).map((e) => e.to)).toEqual(["stop:s2", "stop:s2", "stop:s2"]);
    // No join count: no pill, the bird on the current stop.
    expect(db.nodes.find((n) => n.id === "stop:s2")!.join).toBeUndefined();
    expect(birdPerch(db)).toEqual({ nodeId: "stop:s1", tone: "working" });
  });

  it("gone children leave: a row that closed, or one the join lists as gone", () => {
    const fk = flight(AGENT_STEPS, 0, [
      { title: "w1", state: "working" },
      { title: "w2", state: "gone" },
      { title: "w9", state: "gone" },
    ]);
    const dg = diagramOf([lead(AGENT_STEPS, 0, { waiting: 1, total: 2 }), kid("w1", "s1"), kid("w2", "s1")], fk);
    expect(ids(dg, "branch")).toEqual(["branch:w1"]);
    expect(dg.edges.filter((e) => e.kind === "fan" || e.kind === "join")).toHaveLength(2);
  });

  it("a branch with no activity of its own reads the join's detail (failed, with its report)", () => {
    const fk = flight(AGENT_STEPS, 0, [{ title: "w1", state: "failed", report: "Could not build" }]);
    const dk = diagramOf([lead(AGENT_STEPS, 0, { waiting: 0, total: 1 }), kid("w1", "s1")], fk);
    const b = dk.nodes.find((n) => n.id === "branch:w1")!;
    expect(b.branch!.state).toBe("failed");
    expect(b.chip).toEqual({ label: "Failed", tone: "broken" });
    expect(b.sub).toBe("Could not build");
    // Its live activity wins over the detail; a blocked report is a question.
    expect(branchState(row("x", { activity: "working" }), { title: "x", state: "failed" })).toBe("working");
    expect(branchState(row("x", { last_report: { id: "m", status: "blocked", summary: "?", ts: 1 } }))).toBe("asking");
    expect(branchState(row("x"))).toBe("working");
  });

  it("a branch with its own flight says it in brief: k/n stops · its current one", () => {
    const own = kid("w1", "s1", {
      flight: { id: "f_9", plan_name: "x", cursor: 1, total: 3, state: "running", step: { id: "a2", kind: "ship", title: "Push", state: "running", actor: "robot" }, branch: { parent: "lead", step: "s1" } },
    });
    expect(innerSummary(own)).toBe("1/3 stops · Push");
    const dn = diagramOf([lead(AGENT_STEPS, 0, { waiting: 1, total: 1 }), own], f);
    const b = dn.nodes.find((n) => n.id === "branch:w1")!;
    expect(b.branch!.inner).toBe("1/3 stops · Push");
    expect(b.sub).toBe("1/3 stops · Push");
    expect(innerSummary(row("x", { stage: "committed", autopilot: { depth: "pr", state: "running" } }))).toBe("1/3 stops · Push");
    expect(innerSummary(row("x"))).toBe("");
  });

  it("split → join: branches between the split and the join, the pill on the (current) join stop", () => {
    const fs = flight(SPLIT_STEPS, 1);
    const kids = ["a", "b", "c"].map((t) => kid(t, "s1", { activity: "working" }));
    const ds = diagramOf([lead(SPLIT_STEPS, 1, { waiting: 3, total: 3 }), ...kids], fs);
    expect(ds.nodes.filter((n) => n.kind === "branch").map((n) => [n.branch!.from, n.branch!.to])).toEqual([
      ["stop:s1", "stop:s2"],
      ["stop:s1", "stop:s2"],
      ["stop:s1", "stop:s2"],
    ]);
    expect(ds.nodes.find((n) => n.id === "stop:s2")!.join).toEqual({ waiting: 3, total: 3 });
    expect(birdPerch(ds)).toEqual({ nodeId: "stop:s2", tone: "working" });
  });

  it("drawn from the row alone (no full flight), branches of the current stop still fan out", () => {
    const dr = sessionDiagram([lead(AGENT_STEPS, 0, { waiting: 2, total: 2 }), kid("w1", "s1"), kid("w2", "s1")], "lead");
    expect(dr.nodes.filter((n) => n.kind === "branch").map((n) => n.branch!.from)).toEqual(["stop:i0", "stop:i0"]);
    expect(dr.nodes.find((n) => n.id === "stop:i1")!.join).toEqual({ waiting: 2, total: 2 });
  });

  it("the Board's one-row card (no family) draws no branches", () => {
    const dc = sessionDiagram([lead(AGENT_STEPS, 0, { waiting: 2, total: 5 }), ...FIVE], "lead", { flights: [f], kinds: KINDS, family: false });
    expect(ids(dc, "branch")).toEqual([]);
    expect(dc.nodes.find((n) => n.kind === "session")!.sub).toContain("5 workers");
  });

  it("reordering keeps branches on their stop and re-targets the join at its new next box", () => {
    const f2 = flight(AGENT_STEPS, 0);
    const d2 = diagramOf([lead(AGENT_STEPS, 0, null), kid("w1", "s1")], f2);
    const r = reorderDiagram(d2, 1, 2);
    expect(r.nodes.find((n) => n.id === "branch:w1")!.branch!.to).toBe("stop:s3");
    expect(r.edges).toContainEqual({ from: "branch:w1", to: "stop:s3", kind: "join" });
    expect(r.edges.some((e) => e.from === "stop:s1" && e.kind === "line")).toBe(false);
  });
});

describe("the layout of a branch column", () => {
  const f = flight(AGENT_STEPS, 0);
  const d = diagramOf([lead(AGENT_STEPS, 0, { waiting: 2, total: 5 }), ...FIVE], f);
  const at = (l: ReturnType<typeof layoutDiagram>, id: string) => l.boxes.find((b) => b.node.id === id)!;

  it("stacks the branches in one column between the two stops; the canvas grows to fit it", () => {
    const l = layoutDiagram(d);
    const s1 = at(l, "stop:s1");
    const s2 = at(l, "stop:s2");
    const col = d.nodes.filter((n) => n.kind === "branch").map((n) => at(l, n.id));
    for (const b of col) {
      expect(b.x).toBeGreaterThan(s1.x + s1.w);
      expect(b.x + b.w).toBeLessThan(s2.x);
      expect(b.x).toBe(col[0].x);
      // Smaller than the line's boxes.
      expect(b.w).toBeLessThan(s1.w);
    }
    for (let i = 1; i < col.length; i++) expect(col[i].y).toBeGreaterThan(col[i - 1].y + col[i - 1].h - 1);
    const last = col[col.length - 1];
    expect(l.height).toBe(last.y + last.h);
    expect(l.height).toBeGreaterThan(s1.y + s1.h);
    // One branch sits on the line instead of hanging below it.
    const one = layoutDiagram(diagramOf([lead(AGENT_STEPS, 0, null), kid("w1", "s1")], f));
    expect(one.height).toBe(Math.max(...one.boxes.map((b) => b.y + b.h)));
    expect(at(one, "branch:w1").y).toBeGreaterThanOrEqual(0);
    expect(at(one, "branch:w1").y + at(one, "branch:w1").h).toBeLessThanOrEqual(at(one, "stop:s1").y + at(one, "stop:s1").h);
  });

  it("reading order: the stop, its branches, then the join point", () => {
    const order = layoutDiagram(d).boxes.map((b) => b.node.id);
    expect(order.indexOf("branch:w1")).toBe(order.indexOf("stop:s1") + 1);
    expect(order.indexOf("stop:s2")).toBe(order.indexOf("branch:w5") + 1);
  });

  it("a hung child takes a row of the same column, under the workers", () => {
    const rows = [lead(AGENT_STEPS, 0, null), ...FIVE, row("plain", { parent: "lead" })];
    const l = layoutDiagram(diagramOf(rows, f));
    const lowest = Math.max(...l.boxes.filter((b) => b.node.kind === "branch").map((b) => b.y + b.h));
    expect(at(l, "child:plain").y).toBeGreaterThan(lowest);
    expect(at(l, "child:plain").x).toBe(at(l, "branch:w1").x);
  });

  it("phone: the branches are an indented group under their stop, before the join point", () => {
    const l = layoutDiagram(d, { vertical: true });
    const s1 = at(l, "stop:s1");
    const s2 = at(l, "stop:s2");
    for (const n of d.nodes.filter((x) => x.kind === "branch")) {
      const b = at(l, n.id);
      expect(b.x).toBe(22);
      expect(b.x + b.w).toBeLessThanOrEqual(s1.x + s1.w);
      expect(b.y).toBeGreaterThan(s1.y + s1.h);
      expect(b.y + b.h).toBeLessThan(s2.y);
    }
    const fan = l.wires.find((w) => w.kind === "fan")!;
    expect(fan.d.startsWith("M 11 ")).toBe(true);
  });
});

describe("the canvas draws them", () => {
  const f = flight(AGENT_STEPS, 0);
  const d = diagramOf([lead(AGENT_STEPS, 0, { waiting: 2, total: 5 }), ...FIVE], f);
  const render = (over: Record<string, unknown> = {}) => renderToStaticMarkup(createElement(FlywayCanvas, { diagram: d, label: "Lead", vertical: false, ...over }));
  const boxOf = (html: string, id: string) => new RegExp(`<div[^>]*class="(fl-box [^"]*)"[^>]*data-node="${id}"[^>]*style="([^"]*)"`).exec(html);

  it("small branch boxes: the title, the state chip, the line in brief", () => {
    const html = render();
    expect(boxOf(html, "branch:w1")?.[1]).toContain("fl-box-branch");
    expect(boxOf(html, "branch:w1")?.[2]).toContain("width:156px");
    expect(html).toContain('aria-label="w1, Working"');
    expect(html).toContain('<span class="fl-box-chip tone-working">Working</span>');
    expect(html).toContain('<div class="fl-box-sub">Fixed the parser</div>');
  });

  it("the join point wears the “2 of 5” pill and says it", () => {
    const html = render();
    expect(html).toContain(">2 of 5</span>");
    expect(html).toMatch(/class="fl-join-pill"/);
    expect(html).toContain("waiting for 2 of 5");
    const done = renderToStaticMarkup(createElement(FlywayCanvas, { diagram: diagramOf([lead(AGENT_STEPS, 0, { waiting: 0, total: 5 }), ...FIVE], f), label: "Lead", vertical: false }));
    expect(done).not.toContain("fl-join-pill");
  });

  it("fans out without arrowheads and in with them; an arrived branch's fan-in draws done", () => {
    const html = render();
    const fans = html.match(/<path class="fl-wire fl-wire-fan"[^>]*>/g) || [];
    const joins = html.match(/<path class="fl-wire fl-wire-join[^"]*"[^>]*>/g) || [];
    expect(fans).toHaveLength(5);
    expect(joins).toHaveLength(5);
    expect(fans.every((p) => !p.includes("marker-end"))).toBe(true);
    expect(joins.every((p) => p.includes("marker-end"))).toBe(true);
    expect(joins.filter((p) => p.includes("fl-wire-done"))).toHaveLength(3);
    const sheet = css("components/flights/FlywayCanvas.css");
    expect(sheet).toMatch(/\.fl-wire-join:not\(\.fl-wire-done\)\s*\{[^}]*stroke-dasharray/);
    expect(sheet).toMatch(/\.fl-join-pill\s*\{[^}]*position: absolute/);
  });

  it("the stage is as tall as the branch column", () => {
    const l = layoutDiagram(d);
    const html = render();
    // pad 6, plus the bird's 26px headroom only with a bird.
    expect(html).toContain(`height:${l.height + 12}px`);
  });

  it("box sizes: compact is ~110px, wide is wider than normal but under a line box", () => {
    expect(boxOf(render({ size: "compact" }), "branch:w1")?.[2]).toContain("width:110px");
    expect(boxOf(render({ size: "wide" }), "branch:w1")?.[2]).toContain("width:210px");
  });

  it("phone: the group sits indented under the stop", () => {
    const html = render({ vertical: true });
    expect(html).toContain("fl-canvas-vertical");
    expect(boxOf(html, "branch:w1")?.[2]).toMatch(/translate\(28px, /);
  });

  it("no + between a stop and its join point", () => {
    const branched = renderToStaticMarkup(createElement(FlywayCanvas, { diagram: { ...d, insertFrom: 1 }, label: "Lead", vertical: false, onInsert: () => {} }));
    expect(branched).not.toContain("before stop 2");
    expect(branched).toContain("before stop 3");
  });
});

describe("Split and Join on the line", () => {
  it("their boxes say what they do", () => {
    expect(stopSummary(step("x", "split", "Split", { prompts: "a\nb\nc" }))).toBe("3 workers in parallel");
    expect(stopSummary(step("x", "split", "Split", { prompts: "just one", count: 4 }))).toBe("4 workers in parallel");
    expect(stopSummary(step("x", "split", "Split", {}))).toBe("1 worker in parallel");
    expect(stopSummary(step("x", "join", "Join", { of: "all" }))).toBe("waits for every worker");
    expect(stopSummary(step("x", "join", "Join", { of: "spawned", on_failed: "continue" }))).toBe("waits for its split's workers · goes on past a failure");
  });

  it("the palette lists Split and Join under “The flock does” (the registry's)", () => {
    const html = renderToStaticMarkup(createElement(StopPalette, { kinds: KINDS, onPick: () => {}, onClose: () => {} }));
    const flock = html.slice(html.indexOf("The flock does"), html.indexOf("You do"));
    expect(flock).toContain(">Split<");
    expect(flock).toContain(">Join<");
  });

  it("a Split's count shows only while its prompts name one worker (or none)", () => {
    const split = KINDS.find((k) => k.kind === "split")!;
    const names = (prompts: unknown) => inspectorDef(split, step("x", "split", "Split", { prompts }))!.fields.map((f) => f.name);
    expect(names("")).toContain("count");
    expect(names("just one")).toContain("count");
    expect(names("one\n\n")).toContain("count");
    expect(names("one\ntwo")).not.toContain("count");
    expect(names(["one", "two", "three"])).not.toContain("count");
    expect(names("one\ntwo")).toContain("prompts");
    expect(promptLines("a\n \nb\n")).toBe(2);
    // Other kinds are untouched.
    const agent = KINDS[0];
    expect(inspectorDef(agent, step("x", "agent", "A", { prompts: "a\nb" }))).toBe(agent);
  });

  it("a Split offers “+ Join”: one click puts a Join right after it", () => {
    const src = code("components/flights/useSessionLine.tsx");
    expect(src).toContain('const joinDef = kindOf.get("join");');
    expect(src).toContain('flight.steps[splitAt + 1]?.kind !== "join"');
    expect(src).toContain("const at = splitAt + 1;");
    expect(src).toContain("tackOn(flight.id, newStep(joinDef, flight.steps.map((s) => s.id)), at < end ? at : undefined)");
    expect(src).toContain("+ Join");
    expect(src).toContain("one worker per line");
  });
});

describe("every child a parallel row, each with its own bird (round 12)", () => {
  const f1 = flight(AGENT_STEPS, 1);
  const at = (l: ReturnType<typeof layoutDiagram>, id: string) => l.boxes.find((b) => b.node.id === id)!;
  const plainKids = [row("a", { parent: "lead", activity: "working" }), row("b", { parent: "lead", activity: "clarify" }), row("c", { parent: "lead" })];

  it("plain children hang off the flight's current stop and converge at the next, the line running through them", () => {
    const d = diagramOf([lead(AGENT_STEPS, 1, null), ...plainKids], f1);
    expect(d.nodes.filter((n) => n.kind === "child").map((n) => [n.id, n.branch!.from, n.branch!.to])).toEqual([
      ["child:a", "stop:s2", "stop:s3"],
      ["child:b", "stop:s2", "stop:s3"],
      ["child:c", "stop:s2", "stop:s3"],
    ]);
    expect(d.edges.some((e) => e.from === "stop:s2" && e.to === "stop:s3")).toBe(false);
    // Working / asking / idle: an idle one's turn is done (the flight never waits on it).
    expect(d.nodes.filter((n) => n.kind === "child").map((n) => n.branch!.state)).toEqual(["working", "asking", "done"]);
    const l = layoutDiagram(d);
    expect(new Set(["child:a", "child:b", "child:c"].map((id) => at(l, id).x)).size).toBe(1);
    expect(at(l, "child:b").y).toBeGreaterThan(at(l, "child:a").y);
    expect(at(l, "child:a").x).toBeGreaterThan(at(l, "stop:s2").x + at(l, "stop:s2").w);
    expect(at(l, "stop:s3").x).toBeGreaterThan(at(l, "child:a").x + at(l, "child:a").w);
  });

  it("with no flight they hang off the fast-track's current rung, else the session box", () => {
    const armed = row("lead", { stage: "committed", autopilot: { depth: "push", state: "running", step: "", reason: "", source: "user", item: "" } });
    const dl = sessionDiagram([armed, ...plainKids], "lead");
    expect(dl.nodes.find((n) => n.id === "child:a")!.branch).toMatchObject({ from: "rung:push", to: "add" });
    const dn = sessionDiagram([row("lead"), ...plainKids], "lead");
    expect(dn.nodes.find((n) => n.id === "child:a")!.branch).toMatchObject({ from: "session:lead", to: "add" });
  });

  it("phone: an indented group under the box they hang off", () => {
    const d = diagramOf([lead(AGENT_STEPS, 1, null), ...plainKids], f1);
    const l = layoutDiagram(d, { vertical: true });
    expect(at(l, "child:a").y).toBeGreaterThan(at(l, "stop:s2").y);
    expect(at(l, "stop:s3").y).toBeGreaterThan(at(l, "child:c").y);
  });

  it("the + after the current stop stays, drawn between it and the column (a worker column keeps none)", () => {
    const d = diagramOf([lead(AGENT_STEPS, 1, null), ...plainKids], f1);
    const l = layoutDiagram(d);
    const gap = insertGaps(l.boxes, 2, false).find((g) => g.at === 2)!;
    expect(gap.x).toBeGreaterThan(at(l, "stop:s2").x + at(l, "stop:s2").w);
    expect(gap.x).toBeLessThan(at(l, "child:a").x);
  });

  it("reordering keeps a hung child's fan-in on its stop's new next box", () => {
    const steps = [...AGENT_STEPS, step("s4", "ship", "Merge", { lane: "merge" })];
    const d = diagramOf([lead(steps, 0, null), row("a", { parent: "lead" })], flight(steps, 0));
    // Swap s3 and s4 (places 2 and 3): s1's next box stays s2.
    const r = reorderDiagram(d, 2, 3);
    expect(r.edges.filter((e) => e.kind === "join" && e.from === "child:a").map((e) => e.to)).toEqual(["stop:s2"]);
  });

  it("birdPerches: the main bird first, then one per working (or asking) branch — none for an idle one", () => {
    const d = diagramOf([lead(AGENT_STEPS, 1, null), ...plainKids, kid("w1", "s2", { activity: "working" }), kid("w2", "s2")], f1);
    expect(birdPerches(d)).toEqual([
      { nodeId: "stop:s2", tone: "working" },
      { nodeId: "branch:w1", tone: "working", child: "w1" },
      { nodeId: "child:a", tone: "working", child: "a" },
      { nodeId: "child:b", tone: "you", child: "b" },
    ]);
    // The main one is still birdPerch's.
    expect(birdPerches(d)[0]).toEqual(birdPerch(d));
    expect(birdPerches(sessionDiagram([row("lead"), row("c", { parent: "lead" })], "lead"))).toEqual([]);
  });

  it("the canvas draws a bird on each live branch: a click opens that child; the main bird keeps its hand", () => {
    const d = diagramOf([lead(AGENT_STEPS, 1, null), ...plainKids], f1);
    const render = (over: Record<string, unknown>) => renderToStaticMarkup(createElement(FlywayCanvas, { diagram: d, label: "Lead", vertical: false, ...over }));
    const html = render({ bird: birdPerch(d), onOpen: () => {} });
    const birds = [...html.matchAll(/<span class="(fl-bird-marker[^"]*)"[^>]*role="(\w+)" aria-label="([^"]*)"[^>]*title="([^"]*)"/g)].map((m) => [m[1], m[2], m[3], m[4]]);
    expect(birds).toEqual([
      ["fl-bird-marker tone-working", "button", "The bird: flying Push", expect.stringMatching(/^Halt the flight here/)],
      ["fl-bird-marker tone-working fl-bird-branch", "button", "a’s bird: working", "Open a — working"],
      ["fl-bird-marker tone-you fl-bird-branch", "button", "b’s bird: waiting on you", "Open b — waiting on you"],
    ]);
    // A branch's bird is smaller and never a drag.
    expect(html).toMatch(/fl-bird-branch"[^>]*><span class="fl-bird-big fl-sprite-bird fl-flapping" style="width:18px/);
    // No onOpen: a picture. No bird prop at all (the flyway editor): none.
    expect(render({ bird: null })).toMatch(/class="fl-bird-marker tone-working fl-bird-branch"[^>]*role="img"/);
    expect(render({})).not.toContain("fl-bird-marker");
    expect(css("components/flights/FlywayCanvas.css")).toMatch(/\.fl-bird-marker\.fl-bird-branch \{[^}]*transform: translate\(-50%, -50%\)/);
  });
});
