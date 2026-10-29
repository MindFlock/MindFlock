/** The flyway canvas's model (lib/flightDiagram.ts), pure: a stop's one-line
 * summary, a flyway's and a flight's boxes, the fast-track's rungs, the
 * SESSION DIAGRAM composed from `/api/instances` rows (parent above, children
 * below, the ladder or the flight's stops, the trailing +), the layout's
 * positions and connectors, and reordering by drag. */

import { describe, expect, it } from "vitest";
import type { Instance } from "../api/types";
import type { Flight, FlightRow, FlightStep, StepKindDef } from "../lib/flights";
import { moveStep } from "../lib/flights";
import {
  dropIndex,
  flightDiagram,
  flywayDiagram,
  hasSessionDiagram,
  ladderRungs,
  layoutDiagram,
  reorderDiagram,
  sessionDiagram,
  stopSummary,
} from "../lib/flightDiagram";

const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}): FlightStep => ({
  id,
  kind,
  title,
  config,
  on_fail: "halt",
});

const STEPS: FlightStep[] = [
  step("s1", "agent", "Work the ticket"),
  step("s2", "ship", "Push", { lane: "push" }),
  step("s3", "you", "Approve the merge", { items: ["Read the diff", "Checks green"] }),
  step("s4", "ship", "Merge", { lane: "merge" }),
];

const KINDS: StepKindDef[] = [
  { kind: "agent", label: "Agent", actor: "robot", summary: "", fields: [], default_title: "Agent works" },
  { kind: "ship", label: "Ship", actor: "robot", summary: "", fields: [], default_title: "Push" },
  { kind: "you", label: "Perch", actor: "you", summary: "", fields: [], default_title: "Check it" },
];

function flight(over: Partial<Flight> = {}): Flight {
  return {
    id: "f_1",
    plan_id: "builtin:ticket",
    plan_name: "Ticket to merge",
    steps: STEPS,
    subject: { kind: "ticket", source: "sc", id: "sc-12", title: "Fix the login", url: "" },
    session: "sess",
    repo_root: "/r",
    branch: "b",
    state: "running",
    cursor: 1,
    step_states: {
      s1: { state: "done", since: 1, note: "", detail: {}, rounds: 0 },
      s2: { state: "running", since: 2, note: "", detail: {}, rounds: 0 },
    },
    reason: "",
    history: [],
    started: 1,
    updated: 2,
    finished: 0,
    by: "user",
    ...over,
  };
}

const flightRow = (f: Flight): FlightRow => ({
  id: f.id,
  plan_name: f.plan_name,
  cursor: f.cursor,
  total: f.steps.length,
  state: f.state,
  step: { id: f.steps[f.cursor].id, kind: f.steps[f.cursor].kind, title: f.steps[f.cursor].title, state: "running", actor: "robot" },
});

const inst = (title: string, over: Partial<Instance> = {}): Instance =>
  ({ title, status: "running", activity: "idle", stage: "agent", task_headline: "Do " + title, parent: "", flight: null, autopilot: null, ...over }) as unknown as Instance;

const ids = (d: { nodes: { id: string }[] }) => d.nodes.map((n) => n.id);

describe("a stop in one line", () => {
  it("says the config the way its box does", () => {
    expect(stopSummary(step("a", "ship", "", { lane: "push" }))).toBe("push");
    expect(stopSummary(step("a", "ship", "", { lane: "pr", ask_first: true }))).toBe("open pr · asks first");
    expect(stopSummary(step("a", "reviews", "", { quiet_minutes: 10 }))).toBe("10 min quiet");
    expect(stopSummary(step("a", "verify", "", { who: "agent" }))).toBe("who: agent");
    expect(stopSummary(step("a", "wait", "", { minutes: 5 }))).toBe("5 min");
    expect(stopSummary(step("a", "wait", "", { minutes: 5, until: "18:00" }))).toBe("until 18:00");
    expect(stopSummary(step("a", "you", "", { items: ["x", "y", "z"] }))).toBe("3 items to tick");
    expect(stopSummary(step("a", "checks", "", {}))).toBe("up to 60 min");
    expect(stopSummary(step("a", "agent", "", {}))).toBe("works its turn");
    expect(stopSummary(step("a", "ticket", "", { state: "Done" }))).toBe("move to Done");
  });
});

describe("a flyway being drawn", () => {
  const d = flywayDiagram(STEPS, KINDS);

  it("is its stops in order, then the trailing +", () => {
    expect(ids(d)).toEqual(["stop:s1", "stop:s2", "stop:s3", "stop:s4", "add"]);
    expect(d.nodes.every((n) => n.kind === "add" || (n.state === "edit" && n.editable))).toBe(true);
  });

  it("calls a you stop a perch", () => {
    const perch = d.nodes.find((n) => n.id === "stop:s3")!;
    expect(perch.perch).toBe(true);
    expect(perch.title).toBe("Perch: Approve the merge");
    expect(perch.sub).toBe("2 items to tick");
  });

  it("joins consecutive stops — connectors = stops − 1, plus the + stub", () => {
    expect(d.edges.filter((e) => e.kind === "line")).toHaveLength(STEPS.length - 1);
    expect(d.edges.filter((e) => e.kind === "add")).toEqual([{ from: "stop:s4", to: "add", kind: "add" }]);
  });
});

describe("a flight in the air", () => {
  it("done ✓, current, pending — only pending stops after the current one are editable", () => {
    const d = flightDiagram(flight(), KINDS);
    expect(d.nodes.map((n) => n.state)).toEqual(["done", "current", "pending", "pending", "pending"]);
    expect(d.nodes.filter((n) => n.editable).map((n) => n.id)).toEqual(["stop:s3", "stop:s4"]);
    expect(d.insertFrom).toBe(2);
  });

  it("perched gold on a you stop, halted red with the reason, paused, skipped", () => {
    const perched = flightDiagram(
      flight({ state: "waiting_you", cursor: 2, step_states: { s3: { state: "waiting_you", since: 1, note: "", detail: {}, rounds: 0 } } }),
      KINDS
    );
    expect(perched.nodes[2].state).toBe("perched");
    const halted = flightDiagram(flight({ state: "halted", reason: "Checks failed" }), KINDS);
    expect(halted.nodes[1]).toMatchObject({ state: "halted", reason: "Checks failed" });
    expect(flightDiagram(flight({ state: "paused" }), KINDS).nodes[1].state).toBe("paused");
    const skipped = flightDiagram(flight({ step_states: { s1: { state: "skipped", since: 1, note: "", detail: {}, rounds: 0 } } }), KINDS);
    expect(skipped.nodes[0].state).toBe("skipped");
  });

  it("a landed flight has no + (nothing to tack onto)", () => {
    expect(ids(flightDiagram(flight({ state: "done", cursor: 4 }), KINDS))).not.toContain("add");
  });
});

describe("the fast-track ladder as boxes", () => {
  it("rungs up to the depth, done from the git stage, the first not reached current", () => {
    const run = { depth: "pr", state: "running", step: "", reason: "", source: "user", item: "" };
    expect(ladderRungs({ stage: "committed", autopilot: run })).toEqual([
      { lane: "commit", state: "done" },
      { lane: "push", state: "current" },
      { lane: "pr", state: "pending" },
    ]);
    expect(ladderRungs({ stage: "pushed", autopilot: { ...run, state: "halted", reason: "hook failed" } }).map((r) => r.state)).toEqual([
      "done",
      "done",
      "halted",
    ]);
    expect(ladderRungs({ stage: "agent", autopilot: { ...run, depth: "merge" } }).map((r) => r.lane)).toEqual(["commit", "push", "pr", "merge"]);
  });

  it("nothing when Off or unarmed", () => {
    expect(ladderRungs({ stage: "agent", autopilot: null })).toEqual([]);
    expect(ladderRungs({ stage: "agent", autopilot: { depth: "off", state: "running", step: "", reason: "", source: "", item: "" } })).toEqual([]);
  });
});

describe("the session diagram", () => {
  const lead = inst("lead");
  const me = inst("me", { parent: "lead" });
  const kid1 = inst("w1", { parent: "me", flight: flightRow(flight({ id: "f_k" })) });
  const kid2 = inst("w2", { parent: "me" });
  const rows = [lead, me, kid1, kid2];

  it("is the parent above, the session, its children below, then the + — from rows alone", () => {
    const d = sessionDiagram(rows, "me");
    expect(d.nodes.find((n) => n.row === "above")?.id).toBe("parent:lead");
    expect(d.nodes.filter((n) => n.row === "main").map((n) => n.id)).toEqual(["session:me", "add"]);
    expect(d.nodes.filter((n) => n.row === "below").map((n) => n.id)).toEqual(["child:w1", "child:w2"]);
    expect(d.edges).toEqual(
      expect.arrayContaining([
        { from: "parent:lead", to: "session:me", kind: "up" },
        { from: "session:me", to: "child:w1", kind: "branch" },
        { from: "session:me", to: "child:w2", kind: "branch" },
        { from: "session:me", to: "add", kind: "add" },
      ])
    );
    expect(d.mode).toBe("none");
  });

  it("each box carries its session's state chip and headline; a child its mini ladder", () => {
    const d = sessionDiagram(rows, "me");
    const s = d.nodes.find((n) => n.id === "session:me")!;
    expect(s).toMatchObject({ kind: "session", session: "me", sub: "Do me" });
    expect(s.chip?.label).toBeTruthy();
    expect(d.nodes.find((n) => n.id === "child:w1")!.dots).toBe("●●○○");
  });

  it("an armed fast-track draws its rungs on the line, the same boxes as ship stops", () => {
    const armed = inst("me", { stage: "committed", autopilot: { depth: "push", state: "running", step: "", reason: "", source: "user", item: "" } });
    const d = sessionDiagram([armed], "me");
    expect(d.mode).toBe("ladder");
    expect(d.nodes.map((n) => n.id)).toEqual(["session:me", "rung:commit", "rung:push", "add"]);
    expect(d.nodes.find((n) => n.id === "rung:push")).toMatchObject({ kind: "rung", icon: "ship", state: "current" });
  });

  it("a live flight draws its stops (the full flight when cached), and wins over the ladder", () => {
    const f = flight();
    const flying = inst("me", {
      flight: flightRow(f),
      autopilot: { depth: "push", state: "running", step: "", reason: "", source: "flight", item: "f_1" },
    });
    const d = sessionDiagram([flying], "me", { flights: [f], kinds: KINDS });
    expect(d.mode).toBe("flight");
    expect(d.flightId).toBe("f_1");
    expect(d.nodes.map((n) => n.id)).toEqual(["session:me", "stop:s1", "stop:s2", "stop:s3", "stop:s4", "add"]);
    // Without the full flight the row alone still draws the line.
    const bare = sessionDiagram([flying], "me");
    expect(bare.nodes.filter((n) => n.kind === "stop").map((n) => n.state)).toEqual(["done", "current", "pending", "pending"]);
  });

  it("a live flight's worker spawned at one of its stops is a branch there, not a child below (skyBranches.test.ts)", () => {
    const f = flight();
    const flying = inst("me", { flight: flightRow(f) });
    const spawned = inst("w1", { parent: "me", flight: { branch: { parent: "me", step: "s2" } } as unknown as FlightRow });
    const d = sessionDiagram([flying, spawned, kid2], "me", { flights: [f], kinds: KINDS });
    expect(d.nodes.filter((n) => n.kind === "branch").map((n) => [n.id, n.branch!.from, n.branch!.to])).toEqual([["branch:w1", "stop:s2", "stop:s3"]]);
    expect(d.nodes.filter((n) => n.row === "below").map((n) => n.id)).toEqual(["child:w2"]);
    // The line itself is the same boxes, in the same order.
    expect(d.nodes.filter((n) => n.row === "main").map((n) => n.id)).toEqual(["session:me", "stop:s1", "stop:s2", "stop:s3", "stop:s4", "add"]);
    // Without a flight the same row stays below.
    expect(sessionDiagram([inst("me"), spawned], "me").nodes.map((n) => n.id)).toEqual(["session:me", "add", "child:w1"]);
  });

  it("family: false keeps one row and counts the workers on the session box", () => {
    const d = sessionDiagram(rows, "me", { family: false });
    expect(d.nodes.every((n) => n.row === "main")).toBe(true);
    expect(d.nodes[0].sub).toBe("Do me · 2 workers");
  });

  it("says whether a session has anything to draw", () => {
    expect(hasSessionDiagram(rows, "me")).toBe(true);
    expect(hasSessionDiagram([inst("solo")], "solo")).toBe(false);
    expect(hasSessionDiagram([], "nobody")).toBe(false);
    expect(sessionDiagram([], "nobody").nodes).toEqual([]);
  });
});

describe("the layout", () => {
  it("horizontal: the line left to right, the parent above, children stacked on a branch below", () => {
    const rows = [inst("lead"), inst("me", { parent: "lead" }), inst("w1", { parent: "me" }), inst("w2", { parent: "me" })];
    const l = layoutDiagram(sessionDiagram(rows, "me"));
    const at = (id: string) => l.boxes.find((b) => b.node.id === id)!;
    expect(at("parent:lead").y).toBeLessThan(at("session:me").y);
    expect(at("add").x).toBeGreaterThan(at("session:me").x);
    expect(at("child:w1").y).toBeGreaterThan(at("session:me").y);
    expect(at("child:w2").y).toBeGreaterThan(at("child:w1").y);
    expect(l.wires.find((w) => w.kind === "branch")!.d).toMatch(/^M [\d.]+ [\d.]+ V [\d.]+ H [\d.]+$/);
    expect(l.width).toBeGreaterThan(0);
  });

  it("every connector is a path that ends where its box begins", () => {
    const l = layoutDiagram(flywayDiagram(STEPS, KINDS));
    expect(l.wires).toHaveLength(STEPS.length);
    const b2 = l.boxes[1];
    // Same anchor height on both ends: a straight run into the next box's edge.
    expect(l.wires[0].d).toBe("M " + (l.boxes[0].x + l.boxes[0].w) + " 38 H " + (b2.x - 2));
  });

  it("vertical (a phone): the line top to bottom", () => {
    const l = layoutDiagram(flywayDiagram(STEPS, KINDS), { vertical: true });
    const ys = l.boxes.filter((b) => b.node.kind === "stop").map((b) => b.y);
    expect(ys).toEqual([...ys].sort((a, b) => a - b));
    expect(new Set(l.boxes.filter((b) => b.node.kind === "stop").map((b) => b.x)).size).toBe(1);
    expect(l.wires[0].d).toMatch(/ V /);
  });

  it("a box drawn bigger (an expanded perch) pushes the next one along", () => {
    const d = flywayDiagram(STEPS, KINDS);
    const plain = layoutDiagram(d);
    const big = layoutDiagram(d, { sizeOf: (n, base) => (n.id === "stop:s3" ? { w: 260, h: 140 } : base) });
    expect(big.boxes[3].x).toBe(plain.boxes[3].x + (260 - plain.boxes[2].w));
  });
});

describe("reordering by drag", () => {
  it("moveStep moves one stop and keeps the rest in order", () => {
    expect(moveStep(["a", "b", "c", "d"], 0, 2)).toEqual(["b", "c", "a", "d"]);
    expect(moveStep(["a", "b", "c", "d"], 3, 0)).toEqual(["d", "a", "b", "c"]);
    expect(moveStep(["a", "b"], 1, 1)).toEqual(["a", "b"]);
    expect(moveStep(["a", "b"], 5, 0)).toEqual(["a", "b"]);
  });

  it("the drop lands among the other stops by where the box's centre is", () => {
    expect(dropIndex([100, 300, 500], 50)).toBe(0);
    expect(dropIndex([100, 300, 500], 320)).toBe(2);
    expect(dropIndex([100, 300, 500], 900)).toBe(3);
  });

  it("the preview redraws the connectors in the new order", () => {
    const d = reorderDiagram(flywayDiagram(STEPS, KINDS), 0, 2);
    expect(d.nodes.filter((n) => n.row === "main").map((n) => n.id)).toEqual(["stop:s2", "stop:s3", "stop:s1", "stop:s4", "add"]);
    expect(d.edges.filter((e) => e.kind === "line").map((e) => e.from + ">" + e.to)).toEqual([
      "stop:s2>stop:s3",
      "stop:s3>stop:s1",
      "stop:s1>stop:s4",
    ]);
  });
});
