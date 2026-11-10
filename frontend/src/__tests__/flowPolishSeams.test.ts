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

describe("this round's classes have styles", () => {
  it("the stop dialog's parked-stop pieces, the paused frame, the save-route field", async () => {
    const dialog = (await import("../components/flights/StopDialog.css?raw")).default;
    for (const c of [".stop-dialog-remedies", ".stop-dialog-settings > summary", ".stop-dialog-asks", ".stop-dialog-error", ".stop-dialog-lost", ".stop-dialog-save-hint", ".stop-dialog-hint"]) expect(dialog, c).toContain(c);
    expect(dialog).toMatch(/\.stop-dialog-settings > summary \{[^}]*cursor: pointer/);
    expect(dialog).toMatch(/\.stop-dialog-error,\s*\.stop-dialog-lost \{[^}]*color: var\(--fl-ink-red, var\(--red\)\)/);
    const line = (await import("../components/flights/SkyLine.css?raw")).default;
    expect(line).toContain('.skl-frame[data-frame="paused"] > .skl-frame-box');
    expect(line).toMatch(/\.skl-save-route input \{\s*width: 16em;/);
    const src = (await import("../components/flights/SkyLine.tsx?raw")).default;
    expect(src).not.toContain("frameBoxStyle");
    expect((await import("../components/flights/StopParts.css?raw")).default).toContain(".fl-fld-error");
  });
});

describe("one vocabulary across the pieces", () => {
  it("a ready route says the server's words; the end + says Add a stop", async () => {
    const { startWord } = await import("../components/flights/ReadyHead");
    expect(startWord({ start_on: "run" })).toBe("runs when you press Run");
    expect(startWord({ start_on: "prompt" })).toBe("runs on your next prompt");
    const d = flywayDiagram([step("s1", "agent", "Work")], KINDS);
    const src = (await import("../lib/flightDiagram.ts?raw")).default;
    expect(src).not.toContain('"Tack on a stop"');
    expect(src).toContain('title: "Verify", sub: mark.short');
    expect(d.nodes.length).toBeGreaterThan(0);
  });

  it("the Sky's tools are Select (V) and Pan (H) everywhere", async () => {
    for (const f of ["../components/flights/SkyGuide.tsx?raw", "../components/flights/SkySelection.tsx?raw", "../lib/skyShortcuts.ts?raw"]) {
      const src = (await import(/* @vite-ignore */ f)).default as string;
      expect(src, f).not.toMatch(/✥ Drag|☝ Click|Drag tool|Click tool/);
    }
  });

  it("the empty Sky says what a route is; Verify couldn't start.", async () => {
    const { VISION_BREATH } = await import("../lib/onboarding");
    const sky = (await import("../components/onboarding/SkyEmpty.tsx?raw")).default;
    expect(sky).toContain("{VISION_BREATH[1]} {VISION_BREATH[2]}");
    expect(VISION_BREATH[2]).toMatch(/route/);
    const q = (await import("../state/queries.ts?raw")).default;
    expect(q).toContain(`"Verify couldn't start."`);
    expect(q).not.toContain("verify session couldn't");
  });

  it("halt points and added stops fail in plain words", async () => {
    const src = (await import("../lib/flightsApi.ts?raw")).default;
    expect(src).not.toMatch(/"(Tack on|Halt here|Carry on past here)[^"]*"/);
  });
});

describe("a tile's tooltip names its options", () => {
  it("halt point, Only when and a non-default If it fails go in the tooltip summary, never on the face", async () => {
    const { stopOptionsLine } = await import("../lib/flightDiagram");
    const plain = step("s1", "agent", "Work");
    const opted = step("s2", "ship", "Push", { halt: true, when: "weekday", on_fail: "skip" });
    expect(stopOptionsLine(plain, KINDS)).toBe("");
    const d = flywayDiagram([plain, opted], KINDS);
    const [a, b] = d.nodes.filter((n) => n.kind === "stop");
    expect(a.summary).toBeUndefined();
    expect(b.summary).toBe(
      "The route pauses here first (a halt point) — Carry on to run it. It runs only when weekday — otherwise it is skipped. If it fails, the route goes on without it."
    );
    expect(b.sub).not.toContain("halt point");
    const html = render([plain, opted]);
    expect(html.match(/title="Push[^"]*"/)?.[0]).toContain("Push — The route pauses here first (a halt point)");
  });
});
