/** Round 11 — a stop with sub-steps (docs/flights.md "Round 11" and "As
 * built: round 11 stops"): consecutive steps sharing `step.group.id` draw as
 * ONE group box (`collapseGroups`), its state from its sub-steps, "k of n"
 * while it flies; a line's `expandedGroups` opens one in place; branches out
 * of a sub-step hang off the group box; the canvas counts a group box as one
 * place (`drawnIndexes` / `drawnCanvas` / `flightMove` / `flightIndexAt`), its
 * markup (the stack, ▸ / ▾, its toolbar and menu), a saved stop dropped on a
 * line, and the session's shown name on its box. */

/// <reference types="vite/client" />
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";
import type { Instance } from "../api/types";
import type { Flight, FlightStep, StepKindDef } from "../lib/flights";
import type { Diagram, DiagramNode } from "../lib/flightDiagram";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/errorPop", () => ({ errorPop: vi.fn() }));

const { FlywayCanvas, groupFrames } = await import("../components/flights/FlywayCanvas");
const { useUi } = await import("../state/store");
const fd = await import("../lib/flightDiagram");
const { birdPerch, collapseGroups, drawnCanvas, drawnIndexes, flightIndexAt, flightMove, layoutDiagram, lineUnits, lineWhere, nodeAtIndex, sessionDiagram } = fd;
const { groupHaltStep, haltMark, insertGaps, isStopDrag, menuItemsFor, removeTool, stopDropOf, canShift } = await import("../lib/stopEditing");
const { STOP_MIME } = await import("../lib/flights");
const { FLYWAY_MIME } = await import("../lib/skyLayout");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const G = { id: "g1", name: "Ticket to merge", plan_id: "builtin:ticket" };
const step = (id: string, kind: string, title: string, group?: typeof G): FlightStep => ({ id, kind, title, config: {}, on_fail: "halt", ...(group ? { group } : {}) });
// s1 alone, s2..s4 one group, s5 and s6 alone.
const STEPS = [step("s1", "agent", "Work the ticket"), step("s2", "ship", "Push", G), step("s3", "ship", "Open the PR", G), step("s4", "reviews", "Reviews settle", G), step("s5", "you", "Approve"), step("s6", "ship", "Merge")];
const KINDS: StepKindDef[] = [
  { kind: "agent", label: "Agent", actor: "robot", summary: "", fields: [], default_title: "Agent" },
  { kind: "ship", label: "Ship", actor: "robot", summary: "", fields: [], default_title: "Ship" },
  { kind: "reviews", label: "Reviews settle", actor: "robot", summary: "", fields: [], default_title: "Reviews settle" },
  { kind: "you", label: "Perch", actor: "you", summary: "", fields: [], default_title: "Perch" },
];

function flight(cursor: number, over: { steps?: FlightStep[]; states?: Record<string, string>; state?: string } = {}): Flight {
  const steps = over.steps || STEPS;
  const state = over.state || "running";
  const step_states: Flight["step_states"] = {};
  steps.forEach((s, i) => {
    const st = over.states?.[s.id] || (i < cursor ? "done" : i === cursor ? state : "pending");
    step_states[s.id] = { state: st as Flight["step_states"][string]["state"], since: 1, note: "", detail: {}, rounds: 0 };
  });
  return {
    id: "f_1",
    plan_id: "adhoc",
    plan_name: "A line",
    steps,
    subject: { kind: "manual", source: "", id: "", title: "", url: "" },
    session: "me",
    repo_root: "/r",
    branch: "b",
    state,
    cursor,
    step_states,
    reason: "",
    history: [],
    started: 1,
    updated: 1,
    finished: 0,
    by: "",
  } as Flight;
}
const rowOf = (title: string, over: Record<string, unknown> = {}) =>
  ({ title, status: "running", activity: "working", stage: "agent", task_headline: "Do " + title, parent: "", branch: title, flight: null, autopilot: null, ...over }) as unknown as Instance;
const me = (f: Flight) => {
  const s = f.steps[f.cursor];
  return rowOf("me", { flight: { id: f.id, plan_name: f.plan_name, cursor: f.cursor, total: f.steps.length, state: f.state, step: { id: s.id, kind: s.kind, title: s.title, state: f.state, actor: "robot" } } });
};
const at = (cursor: number, over: Parameters<typeof flight>[1] & { expanded?: string[]; rows?: Instance[] } = {}) => {
  const f = flight(cursor, over);
  return sessionDiagram(over.rows || [me(f)], "me", { flights: [f], kinds: KINDS, expandedGroups: over.expanded ? new Set(over.expanded) : undefined });
};
const mainIds = (d: Diagram) => d.nodes.filter((n) => n.row === "main").map((n) => n.id);
const node = (d: Diagram, id: string) => d.nodes.find((n) => n.id === id)!;
const edge = (d: Diagram, from: string, to: string) => d.edges.find((e) => e.from === from && e.to === to);

describe("a group is one stop on the line", () => {
  it("consecutive steps sharing group.id: one `group` box named after it, the line chaining through it", () => {
    const d = at(2);
    expect(mainIds(d)).toEqual(["session:me", "stop:s1", "group:g1", "stop:s5", "stop:s6", "add"]);
    const g = node(d, "group:g1");
    expect(g).toMatchObject({ kind: "group", row: "main", title: "Ticket to merge", state: "current", index: 1, editable: false });
    expect(g.group).toMatchObject({ id: "g1", name: "Ticket to merge", plan_id: "builtin:ticket", total: 3, done: 1 });
    expect(g.group!.steps.map((s) => s.id)).toEqual(["s2", "s3", "s4"]);
    expect(g.group!.nodes.map((n) => n.state)).toEqual(["done", "current", "pending"]);
    // While it flies: the sub-step it is at is its second line.
    expect(g.group!.current?.title).toBe("Open the PR");
    expect(g.sub).toBe("Open the PR");
    expect(edge(d, "stop:s1", "group:g1")?.kind).toBe("line");
    expect(edge(d, "group:g1", "stop:s5")?.kind).toBe("line");
    expect(d.edges.some((e) => e.from.startsWith("stop:s2") || e.to === "stop:s3")).toBe(false);
    // The flight's own numbers are untouched.
    expect(d.insertFrom).toBe(3);
    expect(node(d, "stop:s5").index).toBe(4);
  });

  it("its state comes from its sub-steps", () => {
    // Not reached yet: pending, and it can change.
    const ahead = node(at(0), "group:g1");
    expect(ahead.state).toBe("pending");
    expect(ahead.editable).toBe(true);
    expect(ahead.sub).toBe("3 stops: Push → Open the PR → Reviews settle");
    expect(ahead.group!.current).toBeUndefined();
    // All flown: done.
    expect(node(at(4), "group:g1").state).toBe("done");
    expect(node(at(4, { states: { s3: "skipped" } }), "group:g1").state).toBe("done");
    // One failed (the flight went on): failed.
    expect(node(at(4, { states: { s3: "failed" } }), "group:g1").state).toBe("failed");
    // Perched / halted / paused at a sub-step: the group says so.
    expect(node(at(2, { state: "halted" }), "group:g1").state).toBe("halted");
    expect(node(at(2, { state: "paused" }), "group:g1").state).toBe("paused");
  });

  it("the bird sits on it, and the bar says where", () => {
    const d = at(2);
    expect(birdPerch(d)).toEqual({ nodeId: "group:g1", tone: "working" });
    expect(lineWhere(d)).toBe("at Ticket to merge · Open the PR");
    expect(nodeAtIndex(d, 3)?.id).toBe("group:g1");
    expect(nodeAtIndex(d, 4)?.id).toBe("stop:s5");
  });

  it("a halt point on its first sub-step not yet flown marks the group", () => {
    const steps = STEPS.map((s) => (s.id === "s3" ? { ...s, halt: true } : s));
    const g = node(at(1, { steps }), "group:g1");
    // s2 is where the flight is: its halt point is s2's (none).
    expect(groupHaltStep(g)?.step?.id).toBe("s2");
    expect(haltMark(g)).toBeNull();
    const g2 = node(at(2, { steps }), "group:g1");
    expect(groupHaltStep(g2)?.step?.id).toBe("s3");
    expect(haltMark(g2)?.glyph).toBe("⏸");
    expect(groupHaltStep(node(at(5), "group:g1"))).toBeNull();
  });

  it("the same id back later on the line is a box of its own — a lone member draws as itself", () => {
    const steps = [...STEPS.slice(0, 4), step("s5", "you", "Approve"), step("s6", "ship", "Merge", G), step("s7", "ship", "Tag", G)];
    expect(mainIds(at(0, { steps }))).toEqual(["session:me", "stop:s1", "group:g1", "stop:s5", "group:g1@5", "add"]);
    // A group of ONE stop is that stop (a joined-on lone Commit stays "Commit").
    const lone = [...STEPS.slice(0, 4), step("s5", "you", "Approve"), step("s6", "ship", "Merge", G)];
    expect(mainIds(at(0, { steps: lone }))).toEqual(["session:me", "stop:s1", "group:g1", "stop:s5", "stop:s6", "add"]);
  });

  it("a line's expandedGroups draws its sub-steps as stops, marked for the outline", () => {
    const d = at(2, { expanded: ["g1"] });
    expect(mainIds(d)).toEqual(["session:me", "stop:s1", "stop:s2", "stop:s3", "stop:s4", "stop:s5", "stop:s6", "add"]);
    expect(["s2", "s3", "s4"].map((s) => node(d, "stop:" + s).inGroup)).toEqual([
      { id: "g1", name: "Ticket to merge", first: true, last: false },
      { id: "g1", name: "Ticket to merge", first: false, last: false },
      { id: "g1", name: "Ticket to merge", first: false, last: true },
    ]);
    expect(node(d, "stop:s1").inGroup).toBeUndefined();
    expect(collapseGroups(d, new Set(["g1"]))).not.toBe(d);
    // The outline: around its sub-steps.
    const l = layoutDiagram(d);
    const frames = groupFrames(l.boxes);
    const s2 = l.boxes.find((b) => b.node.id === "stop:s2")!;
    const s4 = l.boxes.find((b) => b.node.id === "stop:s4")!;
    expect(frames).toEqual([{ id: "g1", name: "Ticket to merge", x: s2.x, y: s2.y, w: s4.x + s4.w - s2.x, h: Math.max(s2.h, s4.h) }]);
  });

  it("branches spawned at a sub-step hang off the group box and join past it", () => {
    const kid = rowOf("w1", { parent: "me", activity: "working", flight: { branch: { parent: "me", step: "s3" } } });
    const f = flight(2);
    const d = sessionDiagram([me(f), kid], "me", { flights: [f], kinds: KINDS });
    const b = node(d, "branch:w1");
    expect(b.branch).toMatchObject({ from: "group:g1", to: "stop:s5" });
    expect(edge(d, "group:g1", "branch:w1")?.kind).toBe("fan");
    expect(edge(d, "branch:w1", "stop:s5")?.kind).toBe("join");
    // The line reaches the next box THROUGH the branch; never a self-edge.
    expect(edge(d, "group:g1", "stop:s5")).toBeUndefined();
    expect(d.edges.some((e) => e.from === e.to)).toBe(false);
  });

  it("the flown fold stops at a group (it stays in sight)", () => {
    const f = flight(5);
    const d = sessionDiagram([me(f)], "me", { flights: [f], kinds: KINDS, foldFlown: true });
    expect(d.nodes.some((n) => n.kind === "flown")).toBe(false);
    expect(node(d, "group:g1").state).toBe("done");
  });
});

describe("the canvas counts a group box as one place", () => {
  // Nothing flown: s1 | g1 (s2..s4) | s5 | s6, the flight at s1.
  const d = at(0);

  it("lineUnits / drawnIndexes: places 0..3, insertFrom in places", () => {
    expect(lineUnits(d).map((u) => [u.node.id, u.first, u.size])).toEqual([
      ["stop:s1", 0, 1],
      ["group:g1", 1, 3],
      ["stop:s5", 4, 1],
      ["stop:s6", 5, 1],
    ]);
    const drawn = drawnIndexes(d);
    expect(lineUnits(drawn).map((u) => u.node.index)).toEqual([0, 1, 2, 3]);
    expect(drawn.insertFrom).toBe(1);
    // A line without groups (or a fold) is its own drawing.
    const plain = sessionDiagram([me(flight(0, { steps: STEPS.map(({ group: _g, ...s }) => s) }))], "me", { flights: [flight(0, { steps: STEPS.map(({ group: _g, ...s }) => s) })], kinds: KINDS });
    expect(drawnIndexes(plain)).toBe(plain);
  });

  it("flightIndexAt / flightMove: a place back to the flight's index; a move lands past or before the whole group", () => {
    expect([0, 1, 2, 3, 4].map((p) => flightIndexAt(d, p))).toEqual([0, 1, 4, 5, 6]);
    // s6 (place 3) to before the group (place 1): index 1.
    expect(flightMove(d, 3, 1)).toEqual({ from: 5, to: 1 });
    // s5 (place 2) past s6: index 5.
    expect(flightMove(d, 2, 3)).toEqual({ from: 4, to: 5 });
    // s1 (place 0) past the group (place 1): after its last step, index 3.
    expect(flightMove(d, 0, 1)).toEqual({ from: 0, to: 3 });
  });

  it("drawnCanvas maps the handlers back; applied twice it is a no-op", () => {
    const calls: unknown[][] = [];
    const canvas = {
      onMove: (a: number, b: number) => calls.push(["move", a, b]),
      onInsert: (a: number) => calls.push(["insert", a]),
      onDropStop: (drop: unknown, a: number | null) => calls.push(["drop", drop, a]),
      onRemove: (n: DiagramNode) => calls.push(["remove", n.id, n.index]),
      onEdit: (n: DiagramNode) => calls.push(["edit", n.id, n.index]),
    };
    const shown = drawnCanvas(d, canvas);
    const g = shown.diagram.nodes.find((n) => n.id === "group:g1")!;
    expect(g.index).toBe(1);
    shown.canvas.onMove!(3, 2);
    shown.canvas.onInsert!(2);
    shown.canvas.onDropStop!({ planId: "builtin:ticket" } as never, 3);
    shown.canvas.onDropStop!({ kind: "ship" } as never, null);
    shown.canvas.onRemove!(shown.diagram.nodes.find((n) => n.id === "stop:s6")!);
    shown.canvas.onEdit!(g);
    expect(calls).toEqual([
      ["move", 5, 4],
      ["insert", 4],
      ["drop", { planId: "builtin:ticket" }, 5],
      ["drop", { kind: "ship" }, null],
      ["remove", "stop:s6", 5],
      ["edit", "group:g1", 1],
    ]);
    const again = drawnCanvas(shown.diagram, shown.canvas);
    expect(again.diagram).toBe(shown.diagram);
    expect(again.canvas).toBe(shown.canvas);
  });

  it("a + sits before a group; a group itself doesn't move (⇤ / ⇥), a stop may trade places with one that can change", () => {
    const drawn = drawnIndexes(d);
    const l = layoutDiagram(drawn);
    expect(insertGaps(l.boxes, drawn.insertFrom, false).map((g) => g.at)).toEqual([1, 2, 3]);
    const places = drawn.nodes.filter((n) => n.row === "main" && (n.kind === "stop" || n.kind === "group"));
    expect(canShift(places, 1, 1, drawn.insertFrom)).toBe(false);
    expect(canShift(places, 2, -1, drawn.insertFrom)).toBe(true);
  });

  it("the hook hands the canvas a drawn line (so SkyLine's flownCanvas stays a no-op)", () => {
    const src = code("components/flights/useSessionLine.tsx");
    expect(src).toContain("const shown = drawnCanvas(line, raw);");
    expect(src).toContain("diagram: shown.diagram,");
    expect(src).toContain("canvas: shown.canvas,");
  });
});

describe("the group box on the canvas", () => {
  const render = (props: Record<string, unknown>) => renderToStaticMarkup(createElement(FlywayCanvas, { label: "Line", vertical: false, ...props } as never));
  const boxOf = (html: string, id: string) => {
    const from = html.indexOf('data-node="' + id + '"');
    const start = html.lastIndexOf("<div", from);
    const next = html.indexOf('role="listitem"', from + 1);
    return html.slice(start, next < 0 ? undefined : next);
  };
  const handlers = { onSelect: () => {}, onEdit: () => {}, onMove: () => {}, onRemove: () => {}, onDuplicate: () => {}, onToggleHalt: () => {}, onToggleGroup: () => {} };

  it("a stack with ▸, its name, “k of n” while it flies, a dot per sub-step", () => {
    const html = render({ diagram: drawnIndexes(at(2)), ...handlers });
    const g = boxOf(html, "group:g1");
    expect(g).toContain("fl-box fl-box-group fl-st-current");
    expect(g).toMatch(/aria-label="Ticket to merge, a group of 3 stops, 1 of 3 done, under way"/);
    expect(g).toContain('aria-label="Show the stops in “Ticket to merge”"');
    expect(g).toContain('aria-expanded="false"');
    expect(g).toContain('<span class="fl-group-count">1 of 3</span>');
    expect(g.match(/class="fl-gdot /g)).toHaveLength(3);
    expect(g).toContain('<div class="fl-box-sub">Open the PR</div>');
  });

  it("its toolbar: ✎, ⇤ ⇥ disabled this round, ⏸ on its first sub-step not flown, 🗑 the whole group", () => {
    const g = boxOf(render({ diagram: drawnIndexes(at(0)), ...handlers }), "group:g1");
    expect(g).toContain('role="toolbar" aria-label="Change “Ticket to merge”"');
    expect(g).toContain('aria-label="Edit “Ticket to merge”"');
    expect(g).toMatch(/aria-label="Move “Ticket to merge” earlier" title="A group doesn&#x27;t move yet" disabled=""/);
    expect(g).toMatch(/aria-label="Move “Ticket to merge” later"[^>]*disabled=""/);
    expect(g).toContain('aria-label="Halt here — “Ticket to merge”"');
    expect(g).toMatch(/aria-label="Remove “Ticket to merge”" title="Remove this group and its 3 stops from the route"/);
    // It never drags.
    expect(g).not.toContain("fl-draggable");
  });

  it("open: its sub-steps inside a labelled outline with ▾", () => {
    const html = render({ diagram: drawnIndexes(at(2, { expanded: ["g1"] })), ...handlers });
    expect(html).toContain('class="fl-group-frame"');
    expect(html).toContain('aria-label="Fold “Ticket to merge” into one stop"');
    expect(html).toContain('aria-expanded="true"');
    expect(boxOf(html, "stop:s3")).toContain("fl-in-group");
    expect(boxOf(html, "stop:s3")).toMatch(/aria-label="Open the PR, under way, in “Ticket to merge”"/);
    expect(html).not.toContain('data-node="group:g1"');
  });

  it("without onToggleGroup (the Board's card): no ▸, no ▾", () => {
    const { onToggleGroup: _t, ...rest } = handlers;
    expect(render({ diagram: drawnIndexes(at(2)), ...rest })).not.toContain("fl-group-chev");
  });

  it("its menu: Edit (Details once it flies), Show / Fold its sub-steps, its halt point, Remove", () => {
    const ctx = { can: { edit: true, move: true, remove: true, halt: true, group: true }, live: true };
    const keys = (n: DiagramNode, c: Parameters<typeof menuItemsFor>[1] = ctx) => menuItemsFor(n, c).map((i) => i.key);
    const ahead = node(at(0), "group:g1");
    expect(keys(ahead)).toEqual(["edit", "expand", "halt", "remove"]);
    expect(menuItemsFor(ahead, ctx).find((i) => i.key === "expand")!.label).toBe("Show its stops");
    expect(menuItemsFor(ahead, { ...ctx, expanded: true }).find((i) => i.key === "expand")!.label).toBe("Fold its stops");
    const flying = node(at(2), "group:g1");
    expect(menuItemsFor(flying, { ...ctx, here: true }).map((i) => i.label)).toEqual([
      "Halt now",
      "Details",
      "Show its stops",
      "Halt here",
      "Remove",
      "The route is in this group — only what is still ahead can change",
    ]);
    expect(removeTool(flying).danger).toBe(true);
  });

  it("a saved stop dragged from the shelf counts as a stop drag; the drop carries its plan id", () => {
    expect(isStopDrag([FLYWAY_MIME])).toBe(true);
    const dt = (m: Record<string, string>) => ({ getData: (k: string) => m[k] || "" });
    expect(stopDropOf(dt({ [STOP_MIME]: "ship" }))).toEqual({ kind: "ship" });
    expect(stopDropOf(dt({ [FLYWAY_MIME]: "builtin:ticket" }))).toEqual({ planId: "builtin:ticket" });
    expect(stopDropOf(dt({ "text/plain": "x" }))).toBeNull();
    const src = code("components/flights/useSessionLine.tsx");
    expect(src).toMatch(/if \("planId" in drop\)/);
    expect(src).toContain("tackOnPlan(fid, plan.id, where)");
  });
});

describe("names: a session's box says its shown name", () => {
  it("the session box (and a branch, a child) reads windowName; its id stays the title", () => {
    useUi.setState({ aliases: { me: "Login fix", w1: "Worker one" } } as never);
    try {
      const kid = rowOf("w1", { parent: "me" });
      const f = flight(0);
      const d = sessionDiagram([me(f), kid], "me", { flights: [f], kinds: KINDS });
      expect(node(d, "session:me").title).toBe("Login fix");
      expect(node(d, "session:me").session).toBe("me");
      expect(node(d, "child:w1").title).toBe("Worker one");
    } finally {
      useUi.setState({ aliases: {} } as never);
    }
  });

  it("FlightStrip and SessionFlight show windowName, never the raw title", () => {
    expect(code("components/flights/FlightStrip.tsx")).toContain('<span className="fl-strip-session">{windowName(flight.session)}</span>');
    expect(code("components/flights/SessionFlight.tsx")).toContain('"What " + windowName(title) + " is doing and will do"');
  });
});
