/** Round 21b "ink" (JUDGE-R21-3 new findings 3, 5, 7): colour on the Sky
 * means STATE. A route's wires, its head's port and icon, and a
 * sub-session's frame are drawn in a neutral ink (theme tokens, light and
 * dark); the session's identity colour is only a dot. */

/// <reference types="vite/client" />
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { css } from "./sourceText";
import type { Flight, FlightStep, StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), instApi: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { atHaltPoint, flightDiagram, HALT_POINT_REASON } = await import("../lib/flightDiagram");
const { NODE_H, chainColor, chainGraph } = await import("../lib/skyGraph");
const C = await import("../components/flights/SkyChain");
const { liveBoxParts, nextIsHaltPoint, stopMenuItems } = await import("../components/flights/FlightStrip");

const SKY = css("components/flights/SkyChain.css");

/** The body of the first rule whose selector is exactly `sel`. */
const rule = (sheet: string, sel: string): string => {
  const at = sheet.indexOf(sel + " {");
  if (at < 0) throw new Error("no rule " + sel);
  return sheet.slice(at, sheet.indexOf("}", at));
};

describe("finding 3: the identity hue is only a dot", () => {
  it("a route's own wires are the neutral ink, never an inline session colour", () => {
    expect(rule(SKY, ".sky-wire-line,\n.sky-wire-head")).toContain("stroke: var(--sky-ink)");
  });

  it("the ink is a theme token, set for dark and for light", () => {
    expect(rule(SKY, ".sky-chain")).toMatch(/--sky-ink: color-mix\(in srgb, var\(--text\)/);
    expect(rule(SKY, ".light .sky-chain")).toMatch(/--sky-ink: color-mix\(in srgb, var\(--text\)/);
    expect(rule(SKY, ".sky-chain")).toMatch(/--sky-head-ink:/);
    expect(rule(SKY, ".light .sky-chain")).toMatch(/--sky-head-ink:/);
  });

  it("the head's icon, out port and a trigger's border are neutral", () => {
    expect(rule(SKY, ".sky-head .sky-tile-icon")).toContain("var(--sky-head-ink)");
    expect(rule(SKY, ".sky-head .sky-port-out")).not.toContain("--sky-head-color");
    expect(rule(SKY, ".sky-head-trigger .sky-tile")).not.toContain("--sky-head-color");
  });

  it("only the dots wear the identity colour", () => {
    const uses = SKY.split("}").filter((r) => /--sky-(head|sub)-color/.test(r)).map((r) => r.slice(0, r.indexOf("{")).trim());
    expect(uses.sort()).toEqual([".sky-head-dot", ".sky-sub-dot"]);
  });
});

// --- finding 5 ---------------------------------------------------------------

const KINDS: StepKindDef[] = [{ kind: "agent", label: "Agent", actor: "robot", summary: "The agent works", fields: [], default_title: "Agent works" }];
const st = (id: string, kind: string, title: string, extra: Partial<FlightStep> = {}): FlightStep => ({ id, kind, title, config: {}, on_fail: "halt", ...extra }) as FlightStep;
const rec = (state: string, over: Record<string, unknown> = {}) => ({ state, since: 1, note: "", detail: {}, rounds: 0, ...over });
const STEPS = [st("s1", "agent", "Agent works the ticket"), st("s2", "test", "Run the tests"), st("s3", "ship", "Commit", { halt: true }), st("s4", "ship", "Push")];
const flight = (over: Partial<Flight>): Flight =>
  ({ id: "f_h", plan_id: "adhoc", plan_name: "Ticket to PR", session: "sc-25355", state: "running", cursor: 0, steps: STEPS, step_states: {}, reason: "", history: [], started: 1, updated: 1, ...over }) as unknown as Flight;

/** What the server makes of a reached halt point (`_hit_halt_point`). */
const PAUSED_AT_HALT = flight({ state: "paused", cursor: 2, reason: HALT_POINT_REASON, step_states: { s1: rec("done"), s2: rec("done"), s3: rec("pending", { detail: { halt_hit: true } }) } } as Partial<Flight>);
/** The same, as the judge's fixture drew it: halted, nothing failed. */
const HALTED_AT_HALT = flight({ state: "halted", cursor: 2, reason: "Halt here: before Commit", step_states: { s1: rec("done"), s2: rec("done") } } as Partial<Flight>);
/** A stop that failed: Retry stays. */
const FAILED = flight({ state: "halted", cursor: 1, reason: "3 tests failed", step_states: { s1: rec("done"), s2: rec("failed", { note: "3 tests failed" }) } } as Partial<Flight>);

function sky(f: Flight) {
  const d = flightDiagram(f, KINDS);
  const g = chainGraph(d, { head: { kind: "session", title: "Claude", sub: "", session: f.session, color: chainColor(f.session) }, key: f.session, flightId: f.id, big: C.stopTileBoxes(d, f) });
  const parts = liveBoxParts(f, true, () => {}, { verifyInPlace: true, inTile: true });
  const html = renderToStaticMarkup(createElement(C.SkyChain, { graph: g, zoom: 1, renderInBox: parts.renderInBox, onEdit: () => {} }));
  return { d, g, html };
}
const tileOf = (html: string, id: string) => {
  const at = html.indexOf('data-diagram-node="' + id + '"');
  const next = html.indexOf('role="listitem"', at + 1);
  return html.slice(at, next < 0 ? undefined : next);
};

describe("finding 5: a reached halt point says Carry on, not Retry", () => {
  it("atHaltPoint: the server's paused-at-a-halt-point, or a halted halt point that never failed; never a failure or a plain pause", () => {
    expect(atHaltPoint(PAUSED_AT_HALT)).toBe(true);
    expect(atHaltPoint(HALTED_AT_HALT)).toBe(true);
    expect(atHaltPoint(FAILED)).toBe(false);
    expect(atHaltPoint({ ...PAUSED_AT_HALT, reason: "" })).toBe(false); // Halt now
    expect(atHaltPoint({ ...HALTED_AT_HALT, step_states: { ...HALTED_AT_HALT.step_states, s3: rec("failed") } } as unknown as Flight)).toBe(false);
    expect(atHaltPoint(null)).toBe(false);
  });

  for (const [name, f] of [["paused (the server's shape)", PAUSED_AT_HALT], ["halted, nothing failed", HALTED_AT_HALT]] as const) {
    it("on the Sky, " + name + ": its name, then Carry on · Skip — no Retry, no Let the agent fix it", () => {
      const { g, html } = sky(f);
      const t = tileOf(html, "stop:s3");
      expect(t).toContain('class="sky-node-title">Commit</div>');
      expect(t).toContain(">Carry on</button>");
      expect(t).toContain(">Skip</button>");
      expect(t.indexOf(">Carry on</button>")).toBeLessThan(t.indexOf(">Skip</button>"));
      expect(t).not.toContain(">Retry</button>");
      expect(t).not.toContain("Let the agent fix it");
      expect(t).toContain('class="btn-primary fl-btn fl-carry"');
      const n = g.nodes.find((x) => x.node.id === "stop:s3")!;
      expect(n.h).toBe(NODE_H + 40);
      expect(n.w).toBe(C.STOP_TILE_W);
    });
  }

  it("the server's shape says it is your halt point, in a calm (not red) line", () => {
    const t = tileOf(sky(PAUSED_AT_HALT).html, "stop:s3");
    expect(t).toContain('<div class="sky-node-reason" title="' + C.HALT_POINT_LINE + '">' + C.HALT_POINT_LINE + "</div>");
    expect(rule(SKY, ".sky-node.st-paused .sky-node-reason")).toContain("color: var(--muted)");
  });

  it("a failed stop keeps Retry · Skip · Let the agent fix it", () => {
    const t = tileOf(sky(FAILED).html, "stop:s2");
    expect(t).toContain(">Retry</button>");
    expect(t).toContain(">Skip</button>");
    expect(t).toContain(">Let the agent fix it</button>");
    expect(t).not.toContain("fl-carry");
  });

  it("the stop's ⋯ at a halt point: Carry on, no Retry; a failed stop keeps Retry", () => {
    const keys = (f: Flight, i: number) => stopMenuItems(f, flightDiagram(f, KINDS).nodes.find((n) => n.index === i)!, () => {}).map((x) => x.key);
    expect(keys(HALTED_AT_HALT, 2)).toEqual(["resume", "cancel"]);
    expect(keys(PAUSED_AT_HALT, 2)).toEqual(["resume", "cancel"]);
    expect(keys(FAILED, 1)).toEqual(["retry", "resume", "cancel"]);
  });

  it("Skip at a halt point carries on too, unless the next stop is a halt point itself", () => {
    expect(nextIsHaltPoint(PAUSED_AT_HALT)).toBe(false);
    const twoHalts = { ...PAUSED_AT_HALT, steps: STEPS.map((s) => (s.id === "s4" ? { ...s, halt: true } : s)) } as Flight;
    expect(nextIsHaltPoint(twoHalts)).toBe(true);
    // A stop already skipped is passed over: the one after it decides.
    expect(nextIsHaltPoint({ ...twoHalts, step_states: { ...twoHalts.step_states, s4: rec("skipped") } } as unknown as Flight)).toBe(false);
  });
});
