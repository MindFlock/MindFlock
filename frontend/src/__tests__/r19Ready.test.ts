/** Round 19 frontend: a ready flight (built, waiting for ▶ Run or the
 * session's next prompt) is drawn on its session's line with every stop
 * editable, and its session tile says how it starts. */
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Instance } from "../api/types";
import type { Flight, FlightStep } from "../lib/flights";
import { isLive, isShown } from "../lib/flights";
import { sessionDiagram } from "../lib/flightDiagram";
import { code, css } from "./sourceText";
import { ReadyHead, startTitle, startWord } from "../components/flights/ReadyHead";

const step = (id: string, kind = "agent"): FlightStep => ({ id, kind, title: id, config: {}, on_fail: "halt" });
const ready = (start_on: "prompt" | "run" = "prompt"): Flight =>
  ({ id: "f1", plan_id: "", plan_name: "Ship it", steps: [step("s1", "ship"), step("s2", "ship")], state: "ready", cursor: 0, step_states: {}, session: "me", start_on, ready_since: 1, history: [] }) as unknown as Flight;
const row = (f: Flight) =>
  ({ title: "me", status: "running", activity: "idle", stage: "agent", task_headline: "", parent: "", autopilot: null, flight: { id: f.id, plan_name: f.plan_name, cursor: 0, total: f.steps.length, state: "ready", step: { id: "s1", kind: "ship", title: "s1", state: "pending", actor: "robot" } } }) as unknown as Instance;

describe("a ready flight", () => {
  it("is shown but not live", () => {
    expect(isShown({ state: "ready" })).toBe(true);
    expect(isLive({ state: "ready" })).toBe(false);
  });
  it("its session's line draws every stop, each one editable, inserts allowed from the first", () => {
    const f = ready();
    const d = sessionDiagram([row(f)], "me", { flights: [f] });
    expect(d.mode).toBe("flight");
    expect(d.insertFrom).toBe(0);
    const stops = d.nodes.filter((n) => n.kind === "stop");
    expect(stops.map((n) => [n.id, n.state, n.editable])).toEqual([
      ["stop:s1", "pending", true],
      ["stop:s2", "pending", true],
    ]);
  });
  it("its session tile: Ready, ▶ Run, and how it starts", () => {
    const html = renderToStaticMarkup(createElement(ReadyHead, { flight: ready() }));
    expect(html).toContain(">Ready<");
    expect(html).toContain("▶ Run");
    expect(html).toContain("starts on next prompt");
    expect(startWord({ start_on: "run" })).toBe("starts on ▶ Run");
    expect(startTitle({ start_on: "run" })).toMatch(/Click to start it on this session's next prompt/);
    expect(startTitle({ start_on: "prompt" })).toMatch(/Click to start it only on ▶ Run/);
  });
  it("one row [Ready] [▶ Run], then one quiet start-on line; the button is not the free chain's .sky-run", () => {
    const html = renderToStaticMarkup(createElement(ReadyHead, { flight: ready("run") }));
    // `.sky-chain .sky-run` is the free chain's hover-only ▶ circle (opacity 0
    // at rest): the head's button must never carry that class.
    expect(html).not.toMatch(/class="[^"]*\bsky-run\b/);
    expect(html).toMatch(/<span class="sky-ready-row"><span class="sky-head-chip tone-ready"[^>]*>Ready<\/span><button type="button" class="sky-ready-run"/);
    // The toggle is a quiet button, not a link (`.sky button.linklike` is accent + underlined).
    expect(html).toMatch(/<button type="button" class="sky-start-on" title="Starts only when you press ▶ Run\.[^"]*">starts on ▶ Run<\/button>/);
    expect(html).not.toContain("linklike");
    const sheet = css("components/flights/SkyChain.css");
    expect(sheet).toMatch(/\.sky-ready-run \{[^}]*border-radius: 999px;[^}]*\}/);
    expect(sheet).toMatch(/\.sky-start-on \{[^}]*white-space: nowrap;[^}]*text-overflow: ellipsis;[^}]*\}/);
    expect(sheet).not.toMatch(/\.sky-start-on \{[^}]*text-decoration: underline/);
    // The session tile's sub: one line, and nothing under the tile shrinks under the next.
    expect(sheet).toMatch(/\.sky-head:has\(\.sky-ready\) > \.sky-node-sub \{[^}]*white-space: nowrap;[^}]*text-overflow: ellipsis;/);
    expect(sheet).toMatch(/\.sky-head > \.sky-head-extra \{\s*flex-shrink: 0;/);
  });
  it("while ready, the tile's sub-line does not repeat it (Sky and the Flight tab)", () => {
    for (const f of ["SkyLine.tsx", "FlightChain.tsx"]) {
      const src = code("components/flights/" + f);
      const headSub = src.split("\n").find((l) => l.includes("const headSub ="));
      expect(headSub, f).toBeTruthy();
      expect(headSub, f).not.toContain("ready ·");
      expect(headSub, f).not.toContain("startWord");
    }
  });
});

describe("a Loop first on a session's chain", () => {
  it("repeats the session's prompt: a wire from the session tile back into it", () => {
    const f = { ...ready(), steps: [{ id: "s1", kind: "loop", title: "Loop ×3", config: { times: 3, back_to: "" }, on_fail: "halt" }, step("s2", "ship")] } as unknown as Flight;
    const d = sessionDiagram([row(f)], "me", { flights: [f] });
    const loops = d.edges.filter((e) => e.kind === "loop");
    expect(loops).toHaveLength(1);
    expect(loops[0]).toMatchObject({ from: "session:me", to: "session:me" });
    expect(loops[0].via?.id).toBe("stop:s1");
    expect(d.nodes.some((n) => n.id === "stop:s1")).toBe(false);
  });
});

describe("a flight that landed", () => {
  it("stays on its session's line, its stops flown, until the next chain", () => {
    const done = { ...ready(), id: "f9", state: "done", finished: 5, step_states: { s1: { state: "done", since: 1, note: "", detail: {}, rounds: 0 }, s2: { state: "done", since: 2, note: "", detail: {}, rounds: 0 } } } as unknown as Flight;
    const r = { ...row(done), flight: null } as unknown as Instance;
    const d = sessionDiagram([r], "me", { flights: [done] });
    expect(d.mode).toBe("flight");
    expect(d.nodes.filter((n) => n.kind === "stop").map((n) => [n.id, n.state, !!n.editable])).toEqual([
      ["stop:s1", "done", false],
      ["stop:s2", "done", false],
    ]);
  });
});
