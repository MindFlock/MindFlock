/** Round 21 "a stop that stops you tells you why and what to do"
 * (JUDGE-R21-2 #2 #3 #7, JUDGE-R21-1 #8 #9): a halted stop on the Sky keeps
 * its name, says why in one red line under it and offers Retry · Skip · Let
 * the agent fix it below that; a Wait for me stop keeps its name too; stop
 * subtitles never echo the title or leak an internal id; the promote item
 * never says "from undefined"; a route built on a session starts on ▶ Run
 * only. */

/// <reference types="vite/client" />
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code, css } from "./sourceText";
import type { Flight, FlightStep, StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), instApi: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { flightDiagram, stopSummary, stopSub } = await import("../lib/flightDiagram");
const { NODE_H, chainColor, chainGraph } = await import("../lib/skyGraph");
const C = await import("../components/flights/SkyChain");
const { canFixStep, liveBoxParts } = await import("../components/flights/FlightStrip");
const { withFixItem } = await import("../components/flights/useChainMenu");
const L = await import("../components/flights/useSessionLine");

const KINDS: StepKindDef[] = [
  { kind: "agent", label: "Agent", actor: "robot", summary: "The agent works", fields: [], default_title: "Agent works" },
  { kind: "you", label: "Wait for me", actor: "you", summary: "Waits for your OK", fields: [], default_title: "Wait for me" },
];

const st = (id: string, kind: string, title: string, config: Record<string, unknown> = {}): FlightStep => ({ id, kind, title, config, on_fail: "halt" }) as FlightStep;
const rec = (state: string, over: Record<string, unknown> = {}) => ({ state, since: 1, note: "", detail: {}, rounds: 0, ...over });

const flight = (over: Partial<Flight> = {}): Flight =>
  ({
    id: "f_1",
    plan_id: "adhoc",
    plan_name: "Ticket to merge",
    session: "sc-17436",
    state: "running",
    cursor: 0,
    steps: [],
    step_states: {},
    reason: "",
    history: [],
    started: 1,
    updated: 1,
    ...over,
  }) as unknown as Flight;

const HALTED = flight({
  state: "halted",
  cursor: 2,
  reason: "CI checks failed: 2 of 14 checks red: test-unit, lint",
  steps: [st("s1", "agent", "Agent works the ticket"), st("s2", "ship", "Open PR", { lane: "pr" }), st("s3", "checks", "CI checks"), st("s4", "land", "Merge when green")],
  step_states: { s1: rec("done"), s2: rec("done"), s3: rec("failed", { note: "2 of 14 checks red: test-unit, lint" }) },
} as Partial<Flight>);

const WAITING = flight({
  state: "waiting_you",
  cursor: 1,
  steps: [st("s1", "agent", "Agent works the ticket"), st("s2", "you", "Approve the diff", { text: "Read the limiter diff before it ships" }), st("s3", "verify", "Verify"), st("s4", "ship", "Commit", { lane: "commit" })],
  step_states: { s1: rec("done"), s2: rec("waiting_you") },
} as Partial<Flight>);

/** One flight's chain as the Sky draws it: SkyChain over chainGraph, the
 * room `stopTileBoxes` gives, and the live parts in the boxes. */
function sky(f: Flight) {
  const d = flightDiagram(f, KINDS);
  const g = chainGraph(d, { head: { kind: "session", title: "Claude", sub: "", session: f.session, color: chainColor(f.session) }, key: f.session, flightId: f.id, big: C.stopTileBoxes(d, f) });
  const parts = liveBoxParts(f, true, () => {}, { verifyInPlace: true, inTile: true });
  const html = renderToStaticMarkup(createElement(C.SkyChain, { graph: g, zoom: 1, renderInBox: parts.renderInBox, onEdit: () => {} }));
  return { d, g, html };
}

/** The markup of one tile (its `data-diagram-node` up to the next tile). */
const tileOf = (html: string, id: string) => {
  const at = html.indexOf('data-diagram-node="' + id + '"');
  const next = html.indexOf('role="listitem"', at + 1);
  return html.slice(at, next < 0 ? undefined : next);
};

describe("a halted stop keeps its name, says why, then the buttons", () => {
  const { g, html } = sky(HALTED);
  const t = tileOf(html, "stop:s3");

  it("name, then one red reason line (its whole text as the tooltip), then Retry · Skip · Let the agent fix it", () => {
    expect(t).toContain('class="sky-node-title">CI checks</div>');
    // The stop's own name is not said twice: "CI checks failed: …" → "Failed: …".
    expect(t).toContain('<div class="sky-node-reason" title="Failed: 2 of 14 checks red: test-unit, lint">Failed: 2 of 14 checks red: test-unit, lint</div>');
    const name = t.indexOf("sky-node-title");
    const why = t.indexOf("sky-node-reason");
    const retry = t.indexOf(">Retry</button>");
    const fix = t.indexOf(">Let the agent fix it</button>");
    const skip = t.indexOf(">Skip</button>");
    expect(name).toBeGreaterThan(0);
    expect(why).toBeGreaterThan(name);
    for (const b of [retry, fix, skip]) expect(b).toBeGreaterThan(why);
    expect(t).toContain('class="fl-you fl-you-tile"');
    // The reason is the tile's line: no sub beside it, no paragraph in the buttons.
    expect(t).not.toContain("sky-node-sub");
    expect(t).not.toContain("fl-you-text");
  });

  it("the tile's tooltip carries the whole reason; the graph gives it room for all of it", () => {
    expect(t).toContain('title="CI checks — CI checks failed: 2 of 14 checks red: test-unit, lint"');
    const n = g.nodes.find((x) => x.node.id === "stop:s3")!;
    expect(n.h).toBe(NODE_H + 40);
    expect(n.w).toBe(C.STOP_TILE_W);
    // The other stops keep the plain box.
    expect(g.nodes.find((x) => x.node.id === "stop:s1")!.h).toBe(NODE_H);
  });

  it("nothing under a halted / waiting tile shrinks (the buttons squeezed the name out), the fix button gets its own row", () => {
    const s = css("components/flights/SkyChain.css");
    expect(s).toMatch(/\.sky-node\.st-halted > :is\(\.sky-node-title, \.sky-node-sub, \.sky-node-reason, \.sky-node-extra\)/);
    expect(s).toMatch(/\.sky-node-reason \{[^}]*white-space: nowrap;[^}]*text-overflow: ellipsis;/);
    expect(s).toMatch(/\.sky-node \.fl-you-tile \.fl-fix \{\s*order: 3;\s*flex-basis: 100%;/);
  });

  it("a plain click on a halted stop opens its Details (onEdit, the right-click's Details)", () => {
    expect(code("components/flights/SkyChain.tsx")).toContain('else if (n.id !== HEAD_ID && n.node.state === "halted" && n.node.kind === "stop" && p.onEdit) p.onEdit(n.node);');
  });
});

describe("a Wait for me stop keeps its name too", () => {
  const { g, html } = sky(WAITING);
  const t = tileOf(html, "stop:s2");

  it("its name, what it asks of you on the line under it, then Done · Skip (Done keeps its word)", () => {
    expect(t).toContain('class="sky-node-title">Wait for me: Approve the diff</div>');
    expect(t).toContain('<div class="sky-node-reason" title="Read the limiter diff before it ships">Read the limiter diff before it ships</div>');
    expect(t.indexOf(">Done</button>")).toBeGreaterThan(t.indexOf("sky-node-reason"));
    expect(t).toContain(">Skip</button>");
    expect(t).not.toContain("Let the agent fix it");
    expect(g.nodes.find((x) => x.node.id === "stop:s2")!.h).toBe(NODE_H + 24);
  });

  it("subtitles never echo the title or say an internal id", () => {
    expect(tileOf(html, "stop:s3")).toContain('class="sky-node-sub">Verify on this branch · agent, then you · right away</div>');
    expect(tileOf(html, "stop:s4")).not.toContain("sky-node-sub");
  });
});

describe("Let the agent fix it — wherever the server's fix op takes it", () => {
  it("a failed stop (any kind), or a failure handed to you; never a halt point, a moved-on failure, or a plain Wait for me", () => {
    expect(canFixStep(HALTED)).toBe(true);
    expect(canFixStep({ ...HALTED, step_states: { ...HALTED.step_states, s3: rec("failed", { detail: { moved_on: true } }) } } as Flight)).toBe(false);
    expect(canFixStep({ ...HALTED, state: "paused", step_states: { s3: rec("pending") } } as unknown as Flight)).toBe(false);
    expect(canFixStep(WAITING)).toBe(false);
    expect(canFixStep({ ...WAITING, step_states: { s2: rec("waiting_you", { detail: { failed_reason: "lint failed" } }) } } as unknown as Flight)).toBe(true);
    expect(canFixStep(null)).toBe(false);
  });

  it("the Needs you card and the Details footer ask the same rule", () => {
    expect(code("components/flights/useSessionLine.tsx")).toContain("onFix={here && canFixStep(flight) ?");
    expect(code("components/flights/SkyDrawer.tsx")).toContain("<YouStepActions flight={flight} />");
  });

  it("the right-click menu of any halted stop has it right after Retry (once)", () => {
    const items = [
      { key: "retry", label: "Retry", title: "" },
      { key: "skip", label: "Skip", title: "" },
    ] as Parameters<typeof withFixItem>[0];
    expect(withFixItem(items, { state: "halted", title: "CI checks" }).map((i) => i.key)).toEqual(["retry", "fix", "skip"]);
    expect(withFixItem(items, { state: "perched", title: "CI checks" }).map((i) => i.key)).toEqual(["retry", "skip"]);
    const verify = [items[0], { key: "fix", label: "Let the agent fix it", title: "" }, items[1]] as Parameters<typeof withFixItem>[0];
    expect(withFixItem(verify, { state: "halted", title: "Verify" }).filter((i) => i.key === "fix")).toHaveLength(1);
  });
});

describe("a stop's subtitle in the vocabulary's words", () => {
  it("Verify says who checks in words, never `who: both`", () => {
    expect(stopSummary(st("a", "verify", "Verify"))).toBe("Verify on this branch · agent, then you · right away");
    expect(stopSummary(st("a", "verify", "Verify", { who: "you" }))).toBe("Verify on this branch · agent, then your sign-off · right away");
    // A legacy `wait_live` reads as where "live", ready "on_branch".
    expect(stopSummary(st("a", "verify", "Verify", { who: "agent", wait_live: true }))).toBe("Verify on live · agent only · once on live");
  });

  it("Finish (`land`) says what it does once merged, never `land`", () => {
    expect(stopSummary(st("a", "land", "Merge when green"))).toBe("once merged: deletes the session");
    expect(stopSummary(st("a", "land", "Finish", { close: "close", delete_branch: false }))).toBe("once merged: closes the session · keeps the branch");
  });

  it("a kind with no words says nothing rather than its id", () => {
    expect(stopSummary(st("a", "mystery_kind", "Something"))).toBe("");
  });

  it("a subtitle that only echoes the title is dropped", () => {
    expect(stopSub("Push", "push")).toBe("");
    expect(stopSub("Open PR", "open pr")).toBe("");
    expect(stopSub("Commit", "commit")).toBe("");
    expect(stopSub("Ship", "open pr · asks first")).toBe("open pr · asks first");
    const d = flightDiagram(flight({ steps: [st("s1", "ship", "Commit", { lane: "commit" }), st("s2", "ship", "Push", { lane: "push" })] } as Partial<Flight>), KINDS);
    expect(d.nodes.filter((n) => n.kind === "stop").map((n) => n.sub)).toEqual(["", ""]);
  });
});

describe("the route ⋯ never says “from undefined”", () => {
  it("a source with no name has no name, and the item hides", () => {
    expect(L.intakeSourceName({})).toBe("");
    expect(L.intakeSourceName({ label: "Tickets · Shortcut", target: "sc" })).toBe("Shortcut");
    expect(L.intakeSourceName({ label: "", target: "o/r" })).toBe("o/r");
    expect(code("components/flights/useSessionLine.tsx")).toContain("if (!src || !intakeSourceName(src)) return null;");
  });

  it("it says route, not these stops", () => {
    expect(L.promoteWords({ kind: "tickets", label: "Tickets · Shortcut", target: "sc" }).label).toBe("Use this route for every ticket from Shortcut");
  });
});

describe("a route built on a session starts on ▶ Run only", () => {
  it("the first stop, a saved route dropped on a session: start_on run, never the next prompt", () => {
    expect(L.NEW_ROUTE_STARTS_ON).toBe("run");
    const src = code("components/flights/useSessionLine.tsx");
    expect(src).toContain("tackOnNew(title, [step], NEW_ROUTE_STARTS_ON)");
    expect(src).not.toContain('start_on: "prompt"');
    expect(src).not.toContain('tackOnNew(title, [step], "prompt")');
  });
});
