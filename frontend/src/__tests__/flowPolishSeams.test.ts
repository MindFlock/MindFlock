/** Flow polish, the seams between this round's pieces: the halt point's ⏸
 * pressed only on a halt point, the canvas's words. */

/// <reference types="vite/client" />
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { FlightStep, StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/errorPop", () => ({ errorPop: vi.fn() }));

const { FlywayCanvas } = await import("../components/flights/FlywayCanvas");
const { flywayDiagram } = await import("../lib/flightDiagram");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const step = (id: string, kind: string, title: string, over: Partial<FlightStep> = {}): FlightStep => ({ id, kind, title, config: {}, on_fail: "halt", ...over });
const KINDS: StepKindDef[] = [
  { kind: "agent", label: "Agent", actor: "robot", summary: "The agent works", fields: [], default_title: "Agent works" },
  { kind: "ship", label: "Ship", actor: "robot", summary: "Commit, push, PR or merge", fields: [], default_title: "Ship" },
];
const render = (steps: FlightStep[]) =>
  renderToStaticMarkup(
    createElement(FlywayCanvas, { label: "Stops", vertical: false, diagram: flywayDiagram(steps, KINDS), onSelect: () => {}, onToggleHalt: () => {}, onRemove: () => {} } as never),
  );

describe("the halt point's ⏸ on the canvas", () => {
  it("is pressed only on a stop that is a halt point", () => {
    const html = render([step("s1", "agent", "Work"), step("s2", "ship", "Push", { halt: true })]);
    const halts = html.match(/<button[^>]*fl-tool-halt[^>]*>/g) || [];
    expect(halts).toHaveLength(2);
    const add = halts.find((b) => b.includes("Add halt point"))!;
    expect(add).not.toContain("fl-tool-on");
    expect(add).toContain('aria-pressed="false"');
    const remove = halts.find((b) => b.includes("Remove halt point"))!;
    expect(remove).toContain("fl-tool-on");
    expect(remove).toContain('aria-pressed="true"');
  });

  it("no stop that is not a halt point looks pressed", () => {
    const html = render([step("s1", "agent", "Work"), step("s2", "ship", "Push")]);
    expect(html).not.toContain("fl-tool-on");
    expect(html).not.toContain('aria-pressed="true"');
  });
});
