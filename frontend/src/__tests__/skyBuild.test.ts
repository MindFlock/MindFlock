/** Sky and routes clarity, round 2 — building and editing a route: a stop's
 * dialog reads back what the stop will DO before what a failure does; a
 * Wait or a Wait for me (which cannot fail) offers no "If it fails"; the
 * catalog says two lines of what each stop does; the shelf says "Stops"
 * once; the right-click "Add a stop…" box stays in view above the shelf. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { FlightStep, StepKindDef } from "../lib/flights";
import { css } from "./sourceText";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/errorPop", () => ({ errorPop: vi.fn() }));

const E = await import("../lib/stopEditing");
const { StopForm } = await import("../components/flights/StopParts");
const { menuSpot, QUICK_ADD_SIZE } = await import("../lib/skyLayout");

const step = (kind: string, config: Record<string, unknown> = {}, extra: Partial<FlightStep> = {}): FlightStep =>
  ({ id: "s1", kind, title: kind, config, on_fail: "halt", ...extra }) as FlightStep;
const def = (kind: string, label: string, fields: StepKindDef["fields"] = []): StepKindDef =>
  ({ kind, label, actor: kind === "you" ? "you" : "robot", summary: label + " things", fields, default_title: label }) as StepKindDef;

describe("the read-back says what the stop does", () => {
  it("an Agent stop: with a prompt, without one, in a new session", () => {
    expect(E.stopDoes(step("agent", { prompt: "" }))).toBe("The session's agent carries on with its task; the stop is done once its turn has settled, and every worker it started has finished.");
    expect(E.stopDoes(step("agent", { prompt: "Fix the flaky test", join: false }))).toBe("The session's agent is sent “Fix the flaky test”; the stop is done once its turn has settled.");
    expect(E.stopDoes(step("agent", { prompt: "x", fresh: true, join: false }))).toMatch(/^A new session in this repo is sent “x”/);
  });

  it("a Ship stop: as far as it goes, and whether it asks first", () => {
    expect(E.stopDoes(step("ship", { lane: "commit" }))).toBe("Commits the work.");
    expect(E.stopDoes(step("ship", { lane: "push" }))).toBe("Commits the work and pushes the branch.");
    expect(E.stopDoes(step("ship", { lane: "pr", ask_first: true }))).toBe("Commits, pushes and opens the pull request. It asks you first, before anything leaves this machine.");
    expect(E.stopDoes(step("ship", { lane: "merge" }))).toBe("Commits, pushes, opens the pull request and merges it once CI is green.");
    // Commit never leaves the machine: nothing to ask.
    expect(E.stopDoes(step("ship", { lane: "commit", ask_first: true }))).toBe("Commits the work.");
  });

  it("CI checks, Wait, Wait for me, Command, Notify; an unknown kind says nothing", () => {
    expect(E.stopDoes(step("checks", { timeout_minutes: 90 }))).toBe("Waits up to 90 min for the PR's CI checks to pass.");
    expect(E.stopDoes(step("checks", { timeout_minutes: 240 }))).toBe("Waits up to 4 h for the PR's CI checks to pass.");
    expect(E.stopDoes(step("wait", { seconds: 600 }))).toBe("Waits 10 min, then goes on.");
    expect(E.stopDoes(step("wait", { until: "18:00" }))).toBe("Waits until 18:00, then goes on.");
    expect(E.stopDoes(step("you"))).toBe("Waits until you press Done (or Skip).");
    expect(E.stopDoes(step("command", { command: "make test", timeout_minutes: 30 }))).toBe("Runs `make test` in the session's folder, for up to 30 min.");
    expect(E.stopDoes(step("command", { command: "" }))).toBe("Runs a shell command — none is set yet.");
    expect(E.stopDoes(step("notify"))).toMatch(/^Sends a notification/);
    expect(E.stopDoes(step("judge"))).toBe("");
  });

  it("the dialog leads with it, then the failure part", () => {
    const html = renderToStaticMarkup(createElement(StopForm, { step: step("checks", { timeout_minutes: 60 }), def: def("checks", "CI checks"), onChange() {} }));
    expect(html).toContain("Waits up to 60 min for the PR&#x27;s CI checks to pass. If it fails, the route halts, red.");
  });
});

describe("a stop that cannot fail offers no failure choice", () => {
  it("Wait and Wait for me: no If it fails, no failure sentence", () => {
    expect(E.canFail("wait")).toBe(false);
    expect(E.canFail("you")).toBe(false);
    expect(E.canFail("checks")).toBe(true);
    expect(E.stopSentence(step("wait"))).toBe("");
    expect(E.stopSentence(step("wait", {}, { halt: true }))).toBe("The route pauses here first (a halt point) — Carry on to run it.");
    for (const [kind, label] of [["wait", "Wait"], ["you", "Wait for me"]]) {
      const html = renderToStaticMarkup(createElement(StopForm, { step: step(kind), def: def(kind, label), onChange() {} }));
      expect(html, kind).not.toContain("If it fails");
    }
    const ship = renderToStaticMarkup(createElement(StopForm, { step: step("ship", { lane: "push" }), def: def("ship", "Ship"), onChange() {} }));
    expect(ship).toContain("If it fails");
  });
});

describe("the catalog and the shelf", () => {
  it("a compact card says two lines of what the stop does", () => {
    expect(css("components/flights/StopCatalog.css")).toMatch(/\.sky-catalog-list\.compact \.sky-catalog-summary \{[^}]*-webkit-line-clamp: 2;/);
  });
});

describe("the right-click Add a stop box stays in view", () => {
  const room = { w: 1100, h: 780 };
  it("beside the pointer when it fits", () => {
    expect(menuSpot(100, 100, QUICK_ADD_SIZE, room)).toEqual({ x: 100, y: 100 });
  });
  it("low on the sky it opens upward; at the right edge, leftward", () => {
    expect(menuSpot(600, 700, QUICK_ADD_SIZE, room)).toEqual({ x: 600, y: 320 });
    expect(menuSpot(1000, 100, QUICK_ADD_SIZE, room)).toEqual({ x: 720, y: 100 });
  });
  it("a room too small for either side holds it against the edge", () => {
    expect(menuSpot(100, 200, QUICK_ADD_SIZE, { w: 1100, h: 420 })).toEqual({ x: 100, y: 32 });
  });
});
