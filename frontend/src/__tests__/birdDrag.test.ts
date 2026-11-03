/** The bird in hand (docs/flights.md "As built: round 12 bird"): a click on
 * the bird halts the flight (Carry on while held, Retry on a failed stop), a
 * drag carries it to another stop and jumps the flight there. The pure rules
 * in lib/stopEditing (`birdAction`, `jumpStepOf`, `jumpFlightOf`,
 * `canJumpTo`, `jumpTargets`), the stop menu's Jump the bird here, and the
 * marker's markup in each state. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { Flight, FlightStep } from "../lib/flights";
import type { Diagram, DiagramNode } from "../lib/flightDiagram";
import type { StopMenuCtx } from "../lib/stopEditing";
import { SRC, css } from "./sourceText";

const api = vi.fn(async (..._args: unknown[]) => ({}));
vi.mock("../api/client", () => ({ api: (...a: unknown[]) => api(...a), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/errorPop", () => ({ errorPop: vi.fn() }));

const { FlywayCanvas } = await import("../components/flights/FlywayCanvas");
const { BirdMarker } = await import("../components/flights/Bird");
const { birdPerch, collapseGroups, flightDiagram, flywayDiagram } = await import("../lib/flightDiagram");
const { jumpFlight } = await import("../lib/flightsApi");
const { birdAction, canJumpTo, jumpFlightOf, jumpStepOf, jumpTargets, menuItemsFor } = await import("../lib/stopEditing");

const step = (id: string, group?: string): FlightStep => ({ id, kind: "agent", title: "Step " + id, config: {}, on_fail: "halt", ...(group ? { group: { id: group, name: "G " + group, plan_id: "" } } : {}) });
const STEPS = [step("s1"), step("s2"), step("s3"), step("s4")];
// s1, [s2 s3 s4 in g1], s5
const GROUPED = [step("s1"), step("s2", "g1"), step("s3", "g1"), step("s4", "g1"), step("s5")];

const NOW_S = Math.floor(Date.now() / 1000);
/** A flight at `cursor`: the stops before it done, the one at it running. */
function flight(over: Partial<Flight> = {}): Flight {
  const steps = over.steps || STEPS;
  const cursor = over.cursor ?? 1;
  const rec = (state: string) => ({ state, since: NOW_S - 300, note: "", detail: {}, rounds: 0 });
  const step_states = Object.fromEntries(steps.slice(0, cursor + 1).map((s, i) => [s.id, rec(i < cursor ? "done" : "running")]));
  return {
    id: "f_1",
    plan_id: "p",
    plan_name: "Ticket to merge",
    steps: STEPS,
    subject: { kind: "ticket", source: "sc", id: "sc-12", title: "Fix the login", url: "" },
    session: "me",
    repo_root: "/r",
    branch: "sc-12",
    state: "running",
    cursor,
    step_states: step_states as Flight["step_states"],
    reason: "",
    history: [],
    started: NOW_S - 900,
    updated: NOW_S - 300,
    finished: 0,
    by: "intake",
    ...over,
  };
}
const ids = (ns: readonly DiagramNode[]) => ns.map((n) => n.id);
const byId = (d: Diagram, id: string) => d.nodes.find((n) => n.id === id)!;

describe("a click on the bird", () => {
  it("halts the flight while it flies or waits on you, carries on while held, retries a failed stop", () => {
    expect(birdAction("working", true)).toMatchObject({ key: "pause", label: "Halt the chain here" });
    expect(birdAction("you", true)).toMatchObject({ key: "pause", label: "Halt the chain here" });
    expect(birdAction("paused", true)).toMatchObject({ key: "resume", label: "Carry on" });
    expect(birdAction("halted", true)).toMatchObject({ key: "retry", label: "Retry" });
  });

  it("does nothing off a flight's stop (the session box, a fast-track rung)", () => {
    for (const tone of ["working", "you", "paused", "halted"] as const) expect(birdAction(tone, false)).toBeNull();
  });
});

describe("where a dragged bird may land", () => {
  it("a stop jumps to itself, a group to its first sub-step; no other box jumps", () => {
    const d = collapseGroups(flightDiagram(flight({ steps: GROUPED, cursor: 2 })));
    expect(jumpStepOf(byId(d, "group:g1"))?.id).toBe("s2");
    const first = d.nodes.find((n) => n.kind === "stop")!;
    expect(jumpStepOf(first)?.id).toBe("s1");
    expect(jumpStepOf(byId(d, "add"))).toBeNull();
    for (const kind of ["session", "parent", "child", "branch", "rung"] as const) expect(jumpStepOf({ ...first, kind })).toBeNull();
  });

  it("knows the flight and the step it is at — inside a group, the sub-step", () => {
    expect(jumpFlightOf(flywayDiagram(STEPS))).toBeNull();
    expect(jumpFlightOf(flightDiagram(flight()))).toEqual({ id: "f_1", at: "s2" });
    expect(jumpFlightOf(collapseGroups(flightDiagram(flight({ steps: GROUPED, cursor: 2 }))))).toEqual({ id: "f_1", at: "s3" });
    expect(jumpFlightOf(flightDiagram(flight({ state: "paused", cursor: 3 })))).toEqual({ id: "f_1", at: "s4" });
    expect(jumpFlightOf(flightDiagram(flight({ state: "halted", cursor: 2 })))).toEqual({ id: "f_1", at: "s3" });
  });

  it("any stop of the flight, earlier or later, but the one it is at — never the +", () => {
    const d = flightDiagram(flight({ cursor: 1 }));
    const all = d.nodes.filter((n) => n.kind === "stop");
    expect(ids(jumpTargets(d)).map((id) => byId(d, id).step!.id)).toEqual(["s1", "s3", "s4"]);
    expect(canJumpTo(jumpFlightOf(d), all[1])).toBe(false);
    expect(canJumpTo(jumpFlightOf(d), byId(d, "add"))).toBe(false);
    // No live flight: nowhere.
    expect(jumpTargets(flywayDiagram(STEPS))).toEqual([]);
    expect(canJumpTo(null, all[0])).toBe(false);
  });

  it("a group is a target by its first sub-step — even the group the flight is in, once past its first", () => {
    const inside = collapseGroups(flightDiagram(flight({ steps: GROUPED, cursor: 3 })));
    expect(ids(jumpTargets(inside))).toContain("group:g1");
    const atFirst = collapseGroups(flightDiagram(flight({ steps: GROUPED, cursor: 1 })));
    expect(ids(jumpTargets(atFirst))).not.toContain("group:g1");
    expect(jumpTargets(atFirst).map((n) => jumpStepOf(n)!.id)).toEqual(["s1", "s5"]);
    // A group drawn open: its sub-steps are stops of their own.
    const open = collapseGroups(flightDiagram(flight({ steps: GROUPED, cursor: 2 })), new Set(["g1"]));
    expect(jumpTargets(open).map((n) => n.step!.id)).toEqual(["s1", "s2", "s4", "s5"]);
  });

  it("the jump route: POST /{id}/jump {step}", async () => {
    api.mockClear();
    await jumpFlight("f_1", "s1", "Step s1");
    expect(api).toHaveBeenCalledWith("/api/flights/f_1/jump", { json: { step: "s1" } });
  });
});

describe("the stop's menu: Jump the bird here", () => {
  const can: StopMenuCtx["can"] = { edit: true, move: true, duplicate: true, remove: true, open: true, halt: true };
  const keys = (n: DiagramNode, ctx: Partial<StopMenuCtx> = {}) => menuItemsFor(n, { can, live: true, ...ctx }).map((i) => i.key);
  const d = flightDiagram(flight({ cursor: 1 }));
  const [flown, here, ahead] = d.nodes.filter((n) => n.kind === "stop");

  it("on every stop it can land on, above Remove and the notes", () => {
    expect(keys(ahead, { jump: true })).toEqual(["edit", "earlier", "later", "duplicate", "halt", "jump", "remove"]);
    expect(keys(flown, { jump: true })).toEqual(["edit", "jump", "remove", "note"]);
    expect(menuItemsFor(ahead, { can, live: true, jump: true }).find((i) => i.key === "jump")).toMatchObject({ label: "Jump the bird here" });
  });

  it("not on the stop the flight is at, nor on a box that is no stop, nor without `jump`", () => {
    expect(keys(here, { here: true, jump: canJumpTo(jumpFlightOf(d), here) })).not.toContain("jump");
    expect(keys(ahead)).not.toContain("jump");
    expect(keys({ ...ahead, kind: "session", session: "me" }, { jump: true })).toEqual(["open"]);
  });

  it("the canvas offers it on a live line only (its menu ctx reads canJumpTo)", () => {
    const src = SRC["components/flights/FlywayCanvas.tsx"];
    expect(src).toContain("jump: canJumpTo(jumpFlightOf(p.diagram), menuNode)");
    expect(src).toMatch(/key === "jump"[\s\S]{0,120}jumpFlight\(flight, to\.id, n\.title\)/);
  });
});

describe("the bird's markup", () => {
  const render = (f: Flight) => {
    const diagram = flightDiagram(f);
    return renderToStaticMarkup(createElement(FlywayCanvas, { label: "Stops", vertical: false, diagram, bird: birdPerch(diagram) } as never));
  };
  const marker = (html: string) => html.match(/<span class="fl-bird-marker[^>]*>/)?.[0] || "";

  it("a flying bird is a button: Halt the flight here, and drag it to jump", () => {
    const m = marker(render(flight()));
    expect(m).toMatch(/^<span class="fl-bird-marker tone-working" style="left:[^"]*" role="button" aria-label="The bird: flying Step s2"/);
    expect(m).toContain('aria-description="Halt the chain here — Carry on picks it up again · Drag the bird to another stop to jump there"');
    expect(m).toContain('tabindex="0"');
    expect(m).toMatch(/title="Halt the chain here/);
  });

  it("perched on you: Halt the flight here too", () => {
    const f = flight({ state: "waiting_you", step_states: { s2: { state: "waiting_you", since: NOW_S, note: "", detail: {}, rounds: 0 } } });
    expect(marker(render(f))).toMatch(/class="fl-bird-marker tone-you"[^>]*role="button"[^>]*title="Halt the chain here/);
  });

  it("paused: Carry on, perched still (no flap)", () => {
    const html = render(flight({ state: "paused" }));
    expect(marker(html)).toMatch(/class="fl-bird-marker tone-paused"[^>]*role="button"[^>]*title="Carry on/);
    expect(html).not.toContain("fl-flapping");
  });

  it("halted on a failure: Retry", () => {
    expect(marker(render(flight({ state: "halted", reason: "push failed" })))).toMatch(/class="fl-bird-marker tone-halted"[^>]*role="button"[^>]*title="Retry/);
  });

  it("a bird with nowhere to jump still clicks, and says nothing of a drag", () => {
    const one = flight({ steps: [step("s1")], cursor: 0, step_states: { s1: { state: "running", since: NOW_S, note: "", detail: {}, rounds: 0 } } });
    const m = marker(render(one));
    expect(m).toContain('role="button"');
    expect(m).not.toContain("Drag the bird");
  });

  it("without a hand (no flight under it) the marker stays a picture", () => {
    const html = renderToStaticMarkup(createElement(BirdMarker, { tone: "working", x: 1, y: 2, label: "The bird" }));
    expect(html).toMatch(/^<span class="fl-bird-marker tone-working" style="left:1px;top:2px" role="img" aria-label="The bird" title="The bird" data-tour="flights-bird">/);
    expect(html).not.toContain("tabindex");
  });

  it("the flyway editor has no bird to pick up", () => {
    const html = renderToStaticMarkup(createElement(FlywayCanvas, { label: "Stops", vertical: false, diagram: flywayDiagram(STEPS) } as never));
    expect(html).not.toContain("fl-bird-marker");
    expect(html).not.toContain("fl-jump-target");
  });
});

describe("the drag's look", () => {
  const canvas = css("components/flights/FlywayCanvas.css");
  it("in hand it follows the pointer, screen-sized at any zoom, and a long-press can lift it", () => {
    expect(canvas).toMatch(/\.fl-bird-marker\[role="button"\] \{[^}]*touch-action: none/);
    expect(canvas).toMatch(/\.fl-bird-marker\.fl-bird-dragging \{[^}]*transition: none;[^}]*scale\(calc\(1\.15 \/ var\(--sky-z, 1\)\)\)/);
    expect(canvas).toMatch(/\.fl-bird-marker\.fl-bird-snap \{[^}]*transition: left 0\.22s/);
  });

  it("every stop it can land on is lit, the one under it more", () => {
    expect(canvas).toMatch(/\.fl-box\.fl-jump-target \{[^}]*outline: 2px dashed/);
    expect(canvas).toMatch(/\.fl-box\.fl-jump-over \{[^}]*outline-style: solid/);
  });

  it("a pointer drag, not HTML5 DnD, that stops before the line's drags and the sky's marquee", () => {
    const src = SRC["components/flights/FlywayCanvas.tsx"];
    const down = src.slice(src.indexOf("const onBirdDown"), src.indexOf("const onBirdMove"));
    expect(down).toContain("e.stopPropagation()");
    expect(down).toContain("setPointerCapture(e.pointerId)");
    expect(down).toContain("BIRD_HOLD_MS");
    expect(src).not.toMatch(/draggable=\{?["']?true/);
  });
});
