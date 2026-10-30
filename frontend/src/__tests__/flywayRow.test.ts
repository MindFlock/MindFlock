/** A flyway on the shelf AS its stops (FlywayRow): the row's name, its stops
 * as boxes on the canvas, Fly, the ⋯, the built-in tag; the empty row that
 * starts a flyway; and the pure rules behind them (lib/flywayEdit: the row
 * order, the drop on the empty row, a rename, putting a stop in, taking one
 * off and Undo). Server-rendered with a mocked API and the kinds seeded, like
 * skyFlyways.test.ts. */

import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code, css } from "./sourceText";
import type { FlightPlan, FlightStep, StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { FlywayRow, EmptyFlywayRow } = await import("../components/flights/FlywayRow");
const { NAME_MAX, canRemoveStop, checkRename, copyOfFlyway, freshFlywayName, flywayFromDrop, insertStop, restoreStep, rowOrder, undeleted } = await import("../lib/flywayEdit");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const step = (id: string, kind: string, title: string): FlightStep => ({ id, kind, title, config: {}, on_fail: "halt" });
const KINDS: StepKindDef[] = [
  { kind: "agent", label: "Agent", actor: "robot", summary: "The agent works", fields: [{ name: "prompt", type: "textarea", label: "Prompt", default: "Go" }], default_title: "Agent works" },
  { kind: "ship", label: "Ship", actor: "robot", summary: "Commit, push, PR or merge", fields: [], default_title: "Ship" },
  { kind: "you", label: "Perch", actor: "you", summary: "Waits for you", fields: [], default_title: "Check it" },
];
const STEPS = [step("s1", "agent", "Work it"), step("s2", "you", "Approve"), step("s3", "ship", "Merge")];
const plan = (id: string, name: string, builtin = false, steps: FlightStep[] = STEPS): FlightPlan => ({
  id,
  name,
  description: "",
  builtin,
  trigger: { kind: "manual", schedule: null, paused: false },
  session: { repo_path: "", program: "", model: "", in_place: false },
  steps,
  updated: 1,
});
const MINE = plan("p_mine", "My review");
const BUILTIN = plan("builtin:ticket", "Ticket to merge", true);

function render(el: ReactElement) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  c.setQueryData(["flight-kinds"], KINDS);
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}
const row = (p: FlightPlan, extra: Record<string, unknown> = {}) =>
  render(createElement(FlywayRow, { plan: p, plans: [MINE, BUILTIN], onFly() {}, onEdit() {}, onDelete() {}, onCopy() {}, ...extra }));
const empty = (naming = 0) => render(createElement(EmptyFlywayRow, { naming, busy: false, onCreate() {}, onEditor() {}, onCancelName() {} }));

describe("a flyway row", () => {
  it("its name (a rename button), its stops as boxes, Fly and its ⋯", () => {
    const html = row(MINE);
    expect(html).toMatch(/^<div role="listitem" class="sky-flyway-row" data-plan="p_mine">/);
    expect(html).toContain('<button type="button" class="sky-flyway-name" aria-label="Rename the flyway “My review”"');
    expect(html).toContain(">My review</button>");
    expect(html.match(/data-node="stop:/g)).toHaveLength(3);
    expect([...html.matchAll(/data-node="(stop:[^"]+)"/g)].map((m) => m[1])).toEqual(["stop:s1", "stop:s2", "stop:s3"]);
    // The canvas: compact, the head of each box alone, never top to bottom.
    expect(html).toMatch(/class="fl-canvas fl-canvas-compact fl-lod-far sky-flyway-canvas"/);
    expect(html).toContain('aria-label="“My review”&#x27;s stops — drag a stop');
    // The end + takes stops too.
    expect(html).toContain('data-node="add"');
    expect(html).toContain('aria-label="Fly “My review” on a line"');
    expect(html).toContain(">Fly</button>");
    expect(html).toContain('aria-label="More for “My review”"');
    expect(html).not.toContain("built-in");
    expect(html).not.toContain("Saved");
  });

  it("a built-in wears a small tag", () => {
    const html = row(BUILTIN);
    expect(html).toMatch(/class="sky-flyway-row builtin" data-plan="builtin:ticket"/);
    expect(html).toContain('<span class="sky-flyway-tag">built-in</span>');
  });

  it("its head (grip + name) drags the whole flyway; renaming, it doesn't", () => {
    expect(row(MINE)).toMatch(/<div class="sky-flyway-row-head" draggable="true" title="Drag onto a line to fly “My review” on it">/);
    const naming = row(MINE, { naming: true });
    expect(naming).toMatch(/<div class="sky-flyway-row-head" draggable="false"/);
    expect(naming).toMatch(/<input type="text" class="fl-input sky-flyway-rename" aria-label="Flyway name"[^>]*value="My review"/);
    expect(naming).not.toContain('class="sky-flyway-name"');
  });

  it("its ⋯: Edit details…, Duplicate, then Delete (yours) or Reset (a built-in) — and no confirm()", () => {
    const src = code("components/flights/FlywayRow.tsx");
    expect(src).toContain('label: "Edit details…"');
    expect(src).toContain('label: "Duplicate flyway"');
    expect(src).toMatch(/shown\.builtin\s*\?\s*\{ key: "reset", label: "Reset to default"/);
    expect(src).toContain("const p = await resetPlan(shown.id);");
    expect(src).toMatch(/\{ key: "delete", label: "Delete flyway", danger: true, onSelect: \(\) => onDelete\(shown\) \}/);
    expect(src).not.toMatch(/\b(confirm|prompt|alert)\(/);
    expect(src).not.toMatch(/<select/);
    // The shelf deletes with an Undo toast, never a confirm.
    const shelf = code("components/flights/FlywayShelf.tsx");
    expect(shelf).toMatch(/toast\("Deleted the flyway “" \+ p\.name \+ "”", \{\s*action: \{\s*label: "Undo"/);
    expect(shelf).not.toMatch(/\bconfirm\(/);
  });

  it("every gesture on the canvas saves at once: drop, remove (with Undo), move, duplicate", () => {
    const src = code("components/flights/FlywayRow.tsx");
    expect(src).toMatch(/onDropStop=\{\(kind, at\) => insert\(kindOf\.get\(kind\), at\)\}/);
    expect(src).toMatch(/onRemove=\{\(n\) => remove\(idx\(n\)\)\}/);
    expect(src).toMatch(/onDuplicate=\{\(n\) => setSteps\(duplicateStep\(cur\.current\.steps, idx\(n\)\)\)\}/);
    expect(src).toMatch(/setSteps\(moveStep\(cur\.current\.steps, from, to\)\)/);
    expect(src).toContain("const saved = await savePlan(next);");
    // A refusal: back to what the server last had, and its sentence.
    expect(src).toMatch(/setLocal\(confirmed\.current\);\s*toast\("Couldn't save “" \+ next\.name \+ "”: " \+ errMsg\(err\), \{ error: true \}\)/);
    expect(src).toMatch(/action: \{ label: "Undo", onClick: \(\) => setSteps\(restoreStep\(cur\.current\.steps, step, i\)\) \}/);
  });

  it("the Saved tick fades by itself", () => {
    const sheet = css("components/flights/FlywayRow.css");
    expect(sheet).toMatch(/\.sky-flyway-saved \{[^}]*animation: sky-flyway-saved 1\.8s ease-out forwards;/);
    expect(sheet).toMatch(/@keyframes sky-flyway-saved/);
  });
});

describe("the empty row", () => {
  it("says to drop stops here, and offers a pick and the editor", () => {
    const html = empty();
    expect(html).toMatch(/^<div role="listitem" class="sky-flyway-row sky-flyway-empty">/);
    expect(html).toContain("Drop stops here to start a flyway");
    expect(html).toContain(">Pick a stop</button>");
    expect(html).toContain(">Editor…</button>");
    expect(html).not.toContain("<input");
  });

  it("+ New flyway: a name field first", () => {
    const html = empty(1);
    expect(html).toMatch(/<input type="text" class="fl-input sky-flyway-rename" aria-label="New flyway&#x27;s name" placeholder="Name the new flyway"/);
  });
});

describe("lib/flywayEdit", () => {
  it("rows: built-ins first, then yours, each as listed", () => {
    expect(rowOrder([MINE, BUILTIN, plan("p_2", "Two")]).map((p) => p.id)).toEqual(["builtin:ticket", "p_mine", "p_2"]);
    expect(rowOrder([])).toEqual([]);
  });

  it("a fresh name is never one the shelf has", () => {
    expect(freshFlywayName([])).toBe("New flyway");
    expect(freshFlywayName([{ name: "New flyway" }, { name: " new flyway 2" }])).toBe("New flyway 3");
    expect(freshFlywayName([{ name: "My review copy" }], "My review copy")).toBe("My review copy 2");
  });

  it("a stop dropped on the empty row: a manual flyway with that one stop", () => {
    const p = flywayFromDrop("agent", KINDS, [{ name: "New flyway" }])!;
    expect(p.id).toBe("");
    expect(p.name).toBe("New flyway 2");
    expect(p.builtin).toBe(false);
    expect(p.trigger.kind).toBe("manual");
    expect(p.steps).toEqual([{ id: "s1", kind: "agent", title: "Agent works", config: { prompt: "Go" }, on_fail: "halt" }]);
    // The name typed first, trimmed.
    expect(flywayFromDrop("ship", KINDS, [], "  Nightly ship ")!.name).toBe("Nightly ship");
    // A kind the server doesn't list makes nothing.
    expect(flywayFromDrop("nope", KINDS, [])).toBeNull();
  });

  it("a rename to nothing is refused; so is another flyway's name; the same one is fine", () => {
    const plans = [MINE, BUILTIN];
    expect(checkRename("", plans, "p_mine")).toEqual({ ok: false, error: "A flyway needs a name" });
    expect(checkRename("   ", plans, "p_mine").ok).toBe(false);
    expect(checkRename("ticket to MERGE", plans, "p_mine")).toEqual({ ok: false, error: "Another flyway is already called “Ticket to merge”" });
    expect(checkRename(" My review ", plans, "p_mine")).toEqual({ ok: true, name: "My review" });
    expect(checkRename("Quick fix", plans, "p_mine")).toEqual({ ok: true, name: "Quick fix" });
    expect(checkRename("x".repeat(NAME_MAX + 1), plans, "p_mine").ok).toBe(false);
  });

  it("a stop put in where it was dropped, or at the end", () => {
    const ship = KINDS[1];
    expect(insertStop(STEPS, ship, 1).map((s) => s.kind)).toEqual(["agent", "ship", "you", "ship"]);
    expect(insertStop(STEPS, ship, null).map((s) => s.id)).toEqual(["s1", "s2", "s3", "s4"]);
    expect(insertStop(STEPS, ship, 99).at(-1)!.kind).toBe("ship");
    expect(insertStop([], ship, 0)).toHaveLength(1);
  });

  it("the last stop stays (a flyway needs one); Undo puts a removed stop back at its place", () => {
    expect(canRemoveStop(STEPS)).toBe(true);
    expect(canRemoveStop([STEPS[0]])).toBe(false);
    const without = STEPS.filter((s) => s.id !== "s2");
    expect(restoreStep(without, STEPS[1], 1)).toEqual(STEPS);
    // The line got shorter meanwhile: at the end.
    expect(restoreStep([STEPS[0]], STEPS[2], 2).map((s) => s.id)).toEqual(["s1", "s3"]);
    // Its id was taken meanwhile: a fresh one.
    const taken = [STEPS[0], step("s2", "ship", "Other")];
    expect(restoreStep(taken, STEPS[1], 1).map((s) => s.id)).toEqual(["s1", "s3", "s2"]);
  });

  it("a copy is one of yours under a fresh name; an undone delete saves it again", () => {
    const c = copyOfFlyway(BUILTIN, [BUILTIN]);
    expect(c).toMatchObject({ id: "", builtin: false, name: "Ticket to merge copy" });
    expect(c.steps).toEqual(BUILTIN.steps);
    expect(c.steps).not.toBe(BUILTIN.steps);
    expect(undeleted(MINE)).toMatchObject({ id: "", builtin: false, name: "My review", steps: STEPS });
  });
});
