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

describe("the pane's Verify strip", () => {
  it("is drawn without an inst.verify guard (it returns null itself), so Verify on a never-verified session opens the form", async () => {
    const src = (await import("../components/grid/Pane.tsx?raw")).default;
    expect(src).toContain("<PaneVerifyStrip inst={inst} />");
    expect(src).not.toMatch(/inst\.verify\s*&&\s*<PaneVerifyStrip/);
  });

  it("takes its glyphs from lib/sessionState verifyGlyph", async () => {
    const { verifyStripView } = await import("../components/grid/PaneVerifyStrip");
    const { verifyGlyph } = await import("../lib/sessionState");
    for (const state of ["verifying", "needs_you", "passed", "failed", "blocked"] as const)
      expect(verifyStripView({ state, plan: "p" } as never, "s", 0).glyph).toBe(verifyGlyph(state));
  });
});

describe("the bell's row for a route paused at a halt point", () => {
  it("a calm chip, Carry on and Skip — not a red row with only Open", async () => {
    const { WaitingRow } = await import("../components/outbox/WaitingRow");
    const { withFlightRows } = await import("../lib/needsYou");
    const flight = {
      id: "f_1",
      plan_name: "Ticket to merge",
      state: "paused",
      reason: "halt point",
      session: "",
      cursor: 0,
      steps: [{ id: "s1", kind: "ship", title: "Push", config: {}, on_fail: "halt" }],
    };
    const [w] = withFlightRows([], [], [flight] as never);
    expect(w.kind).toBe("flight_paused");
    const html = renderToStaticMarkup(createElement(WaitingRow, { w, shown: (t: string) => t } as never));
    expect(html).toContain("ob-flight_paused");
    expect(html).toContain('<span class="pr-open-chip" title="Paused at Push — Carry on">Paused at Push</span>');
    expect(html).toMatch(/<button[^>]*>Carry on<\/button>/);
    expect(html).toMatch(/<button[^>]*>Skip<\/button>/);
    expect(html).not.toContain("pr-open-chip bad");
  });

  it("a Verify stop parked on you says Verify, not Behaviors", async () => {
    const src = (await import("../components/outbox/WaitingRow.tsx?raw")).default;
    expect(src).not.toMatch(/>\s*Behaviors\s*</);
    expect(src).toMatch(/>\s*Verify\s*<\/button>/);
  });
});

describe("a refusal under its own field (StopForm errors)", () => {
  const COMMAND: StepKindDef = {
    kind: "command",
    label: "Command",
    actor: "robot",
    summary: "Run a command",
    fields: [
      { name: "command", type: "textarea", label: "Command" },
      { name: "timeout_minutes", type: "int", label: "Timeout (minutes)" },
    ],
    default_title: "Run",
  };
  const cmd = step("s1", "command", "Tests", { config: { command: "make", timeout_minutes: 30 } });

  it("draws each field's error as its hint, in red", async () => {
    const { StopForm } = await import("../components/flights/StopParts");
    for (const name of ["title", "timeout_minutes", "when", "on_fail"]) {
      const html = renderToStaticMarkup(createElement(StopForm, { step: cmd, def: COMMAND, onChange: () => {}, errors: { [name]: "Not that — " + name } }));
      expect(html, name).toContain(name === "when" ? '<span class="fl-when-error" role="alert">Not that — when</span>' : '<span class="fl-fld-error" role="alert">Not that — ' + name + "</span>");
      expect(html.match(/role="alert"/g), name).toHaveLength(1);
    }
    expect(renderToStaticMarkup(createElement(StopForm, { step: cmd, def: COMMAND, onChange: () => {} }))).not.toContain('role="alert"');
  });

  it("formFieldNames: the form's own and its kind's fields; a Verify stop's own choices are drawn by its editor", async () => {
    const { formFieldNames } = await import("../components/flights/StopParts");
    expect([...formFieldNames(cmd, COMMAND)].sort()).toEqual(["command", "on_fail", "timeout_minutes", "title", "when"]);
    const VERIFY: StepKindDef = { kind: "verify", label: "Verify", actor: "robot", summary: "", fields: [{ name: "where", type: "text", label: "Where" }, { name: "who", type: "select", label: "Who" }], default_title: "Verify" };
    expect(formFieldNames(step("v", "verify", "Verify"), VERIFY).has("who")).toBe(false);
  });

  it("the stop dialog keeps the top-of-form line for a field the form doesn't draw", async () => {
    const SD = await import("../components/flights/StopDialog");
    const node = { id: "stop:s1", kind: "stop", row: "main", title: "Tests", sub: "", state: "pending", icon: "command", index: 0, editable: true } as never;
    const bare: StepKindDef = { ...COMMAND, fields: [] };
    const html = renderToStaticMarkup(
      createElement(SD.StopDialog, { node: { ...(node as object), step: step("s1", "command", "Tests", { config: { command: "" } }) }, def: bare, onSave: () => {}, onClose: () => {}, portal: false } as never),
    );
    expect(html).toMatch(/class="fl-note stop-dialog-error" role="alert" data-field="command"/);
  });
});
