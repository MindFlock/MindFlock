/** Round 11 — a box edits in a DIALOG (StopDialog, on the app's Modal), not
 * an inline panel: a stop that can change (its kind read-only, Title, its
 * kind's fields, If it fails, its halt point; Save · Cancel · Remove ·
 * Duplicate), one that can't (read-only, its state and note, only what it
 * can do), a group (its name, its sub-steps each with ✎ and 🗑, Ungroup, Save
 * to the shelf, Remove); the session line's wiring (no inline inspector, the
 * gestures that open it, the toasts after a remove, the saved stop a group
 * saves into), and never a native confirm / prompt / alert. */

/// <reference types="vite/client" />
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";
import type { FlightPlan, FlightStep, StepKindDef } from "../lib/flights";
import type { DiagramNode } from "../lib/flightDiagram";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/errorPop", () => ({ errorPop: vi.fn() }));

const { StopDialog } = await import("../components/flights/StopDialog");
const { removedToast, shelfPlanFor } = await import("../components/flights/useSessionLine");
const StopParts = await import("../components/flights/StopParts");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const SHIP: StepKindDef = {
  kind: "ship",
  label: "Ship",
  actor: "robot",
  summary: "Commit, push, PR or merge",
  fields: [{ name: "lane", type: "select", label: "Lane", options: ["commit", "push", "pr", "merge"] }],
  default_title: "Ship",
};
const YOU: StepKindDef = { kind: "you", label: "Perch", actor: "you", summary: "Waits for you to press Done", fields: [{ name: "text", type: "textarea", label: "What to do" }], default_title: "Check it" };
const push: FlightStep = { id: "s2", kind: "ship", title: "Push", config: { lane: "push" }, on_fail: "halt" };
const stop = (over: Partial<DiagramNode> = {}): DiagramNode => ({ id: "stop:s2", kind: "stop", row: "main", title: "Push", sub: "push", state: "pending", icon: "ship", step: push, index: 1, editable: true, ...over });
const render = (props: Record<string, unknown>) => renderToStaticMarkup(createElement(StopDialog, { portal: false, onClose: () => {}, ...props } as never));
const buttons = (html: string) => [...html.matchAll(/<button type="(?:button|submit)"[^>]*>([^<]+)<\/button>/g)].map((m) => m[1]);

describe("a stop that can change", () => {
  const html = render({ node: stop(), def: SHIP, onSave: () => {}, onRemove: () => {}, onDuplicate: () => {} });

  it("is a Modal with the house shape: the backdrop, an h2, .modal-actions", () => {
    expect(html).toMatch(/^<div id="stop-dialog" role="dialog" aria-modal="true" aria-label="Edit “Push”" tabindex="-1" class="modal stop-dialog-modal">/);
    expect(html).toContain("<h2>Edit stop</h2>");
    expect(html).toContain('class="modal-actions stop-dialog-actions"');
  });

  it("its kind read-only, its Title, its kind's fields, If it fails and its halt point", () => {
    expect(html).toContain("<strong>Ship</strong>");
    expect(html).toContain("Commit, push, PR or merge");
    expect(html).toContain('aria-label="Stop title"');
    expect(html).toContain('value="Push"');
    expect(html).toContain(">Lane<");
    expect(html).toContain(">If it fails<");
    expect(html).toMatch(/class="fl-toggle"[^>]*aria-pressed="false"/);
    expect(html).toContain("Halt point — the flight stops here, before doing this stop");
    // No inline inspector panel.
    expect(html).not.toContain('class="fl-inspector');
  });

  it("Remove · Duplicate on the left, Cancel · Save (nothing to save yet)", () => {
    expect(buttons(html).filter((b) => ["Remove", "Duplicate", "Cancel", "Save"].includes(b))).toEqual(["Remove", "Duplicate", "Cancel", "Save"]);
    expect(html).toMatch(/<button type="submit" class="btn-primary" disabled="">Save<\/button>/);
    expect(html).toContain('class="test-btn stop-dialog-danger"');
  });

  it("a halt point starts pressed", () => {
    expect(render({ node: stop({ step: { ...push, halt: true } }), def: SHIP, onSave: () => {} })).toMatch(/class="fl-toggle on"[^>]*aria-pressed="true"/);
  });
});

describe("a stop that can't change", () => {
  it("flown: read-only, its state and note, Remove and Close only", () => {
    const html = render({ node: stop({ state: "done", editable: false }), def: SHIP, note: "This stop has flown — it can't change now.", onRemove: () => {} });
    expect(html).toContain("<h2>Stop</h2>");
    expect(html).toMatch(/class="stop-dialog-state" role="status"><strong class="stop-dialog-chip fl-st-done">Done<\/strong><span>This stop has flown — it can&#x27;t change now\.<\/span>/);
    const acts = html.slice(html.indexOf('class="modal-actions'));
    expect(buttons(acts)).toEqual(["Remove", "Close"]);
    expect(html).not.toContain("Halt point");
    expect(html).not.toContain(">Save<");
  });

  it("the current one: what the menu offers — Done on a perch, Skip; Retry when halted; its halt point set at once", () => {
    const perch = stop({ state: "perched", editable: false, step: { ...push, kind: "you", title: "Approve" }, icon: "you" });
    const html = render({ node: perch, def: YOU, onRemove: () => {}, onDone: () => {}, onSkip: () => {}, onToggleHalt: () => {} });
    expect(html).toContain("<strong>Perch</strong>");
    expect(html).toContain("stop-dialog-perch");
    expect(html).toContain(">Perched on you<");
    expect(buttons(html).filter((b) => ["Remove", "Done", "Retry", "Skip", "Close"].includes(b))).toEqual(["Remove", "Done", "Skip", "Close"]);
    expect(html).toContain("Halt point — the flight stops here");
    const halted = render({ node: stop({ state: "halted", editable: false }), def: SHIP, onRetry: () => {}, onSkip: () => {} });
    expect(buttons(halted)).toContain("Retry");
  });
});

describe("a group's dialog", () => {
  const subs: DiagramNode[] = [
    stop({ id: "stop:s2", state: "done", editable: false }),
    stop({ id: "stop:s3", title: "Open the PR", state: "current", editable: false, step: { ...push, id: "s3", title: "Open the PR" }, index: 2 }),
    stop({ id: "stop:s4", title: "Reviews settle", step: { ...push, id: "s4", kind: "reviews", title: "Reviews settle" }, icon: "reviews", index: 3 }),
  ];
  const group: DiagramNode = {
    id: "group:g1",
    kind: "group",
    row: "main",
    title: "Ticket to merge",
    sub: "Open the PR",
    state: "current",
    icon: "group",
    index: 1,
    editable: false,
    group: { id: "g1", name: "Ticket to merge", plan_id: "builtin:ticket", steps: subs.map((s) => s.step!), nodes: subs, done: 1, total: 3, current: subs[1] },
  };
  const html = render({ node: group, kindOf: new Map([["ship", SHIP]]), onRename: () => {}, onOpenSub: () => {}, onRemoveSub: () => {}, onUngroup: () => {}, onSaveShelf: () => {}, onRemove: () => {} });

  it("its name, and what it is", () => {
    expect(html).toContain('aria-label="Stop name"');
    expect(html).toContain('value="Ticket to merge"');
    expect(html).toContain("A stop with 3 sub-steps");
    expect(html).toContain("1 of 3 done");
  });

  it("a row per sub-step: its kind's icon, its title, its state, ✎ and 🗑", () => {
    expect(html.match(/<li class="stop-dialog-sub /g)).toHaveLength(3);
    expect(html).toMatch(/<li class="stop-dialog-sub fl-st-current">.*Open the PR.*Under way/);
    expect(html).toContain('aria-label="Edit “Reviews settle”"');
    expect(html).toContain('aria-label="Remove “Open the PR”"');
  });

  it("Remove · Ungroup · Save to the shelf, then Cancel · Save (the rename)", () => {
    expect(buttons(html).filter((b) => /^[A-Z]/.test(b))).toEqual(["Remove", "Ungroup", "Save to the shelf", "Cancel", "Save"]);
  });

  it("without a rename to make (nothing ahead can change): Close", () => {
    expect(buttons(render({ node: group, onRemove: () => {} })).filter((b) => /^[A-Z]/.test(b))).toEqual(["Remove", "Close"]);
  });
});

describe("the session line's wiring", () => {
  const src = code("components/flights/useSessionLine.tsx");
  const canvas = code("components/flights/FlywayCanvas.tsx");

  it("the dialog IS the line's inspector output; no inline inspector any more", () => {
    expect(src).toContain("<StopDialog");
    expect(src).not.toContain("<StopInspector");
    // The exports another file still imports keep working.
    expect(typeof StopParts.StopInspector).toBe("function");
    expect(typeof StopParts.DescribeField).toBe("function");
    expect(typeof StopParts.StopForm).toBe("function");
  });

  it("✎, Enter / Space, a double-click and the menu's Edit open it; a click only selects", () => {
    expect(canvas).toContain("onDoubleClick");
    expect(canvas).toMatch(/if \(p\.onEdit && \(n\.kind === "stop" \|\| n\.kind === "group"\)\) edit\(n\);/);
    expect(canvas).toContain('if (key === "edit") edit(n);');
    expect(src).toContain("onEdit: (n) => onEdit(own(n)),");
  });

  it("Remove on the stop the flight is at says where the flight now waits", () => {
    expect(removedToast("Push", false, "Merge")).toBe("Removed “Push” from the chain");
    expect(removedToast("Push", true, "Merge")).toBe("Removed “Push” — the chain is paused before “Merge”");
    expect(removedToast("Merge", true, undefined)).toBe("Removed “Merge” — the chain is paused at the end");
    expect(src).toMatch(/toast\(removedToast\(step\.title, wasHere, next\), \{\s+action: \{\s+label: "Undo"/);
  });

  it("Save to the shelf: the saved stop the group came from, else a new one", () => {
    const steps: FlightStep[] = [push, { ...push, id: "s3", title: "Open the PR", config: { lane: "pr", _driver: 1 } }];
    const was: FlightPlan = {
      id: "builtin:ticket",
      name: "Ticket to merge",
      description: "Push, PR",
      builtin: true,
      trigger: { kind: "ticket", schedule: null, paused: false },
      session: { repo_path: "/r", program: "", model: "", in_place: false },
      steps: [],
      updated: 42,
    };
    const upd = shelfPlanFor("Ticket to merge", steps, "builtin:ticket", [was]);
    expect(upd).toMatchObject({ id: "builtin:ticket", updated: 42, builtin: true, description: "Push, PR", trigger: was.trigger, session: was.session });
    expect(upd.steps.map((s) => [s.id, s.title, s.config])).toEqual([
      ["s1", "Push", { lane: "push" }],
      ["s2", "Open the PR", { lane: "pr" }],
    ]);
    const fresh = shelfPlanFor("My stops", steps, "", [was]);
    expect(fresh).toMatchObject({ id: "", updated: 0, name: "My stops", builtin: false });
    // A plan id the shelf no longer has: a new one.
    expect(shelfPlanFor("Gone", steps, "p_gone", [was]).id).toBe("");
  });

  it("never a native confirm / prompt / alert (the desktop app has none)", () => {
    for (const f of ["StopDialog", "useSessionLine", "FlywayCanvas", "StopParts"])
      expect(code("components/flights/" + f + ".tsx"), f).not.toMatch(/\b(window\.)?(confirm|prompt|alert)\(/);
  });
});

describe("round 16: a Loop's dialog", () => {
  const LOOP: StepKindDef = {
    kind: "loop",
    label: "Loop",
    actor: "robot",
    summary: "Repeat from an earlier stop",
    default_title: "Loop ×3",
    fields: [
      { name: "back_to", type: "select", label: "Repeat from", options: [], dynamic: "stops_before", default: "" },
      { name: "times", type: "int", label: "Runs in all", default: 3 },
      { name: "wait_limits", type: "bool", label: "Wait out usage limits", default: true },
      { name: "fresh", type: "bool", label: "Clear the agent's context before each run", default: false },
      { name: "until", type: "text", label: "Until", placeholder: "e.g. checks green" },
    ],
  };
  const g = { id: "g1", name: "Polish", plan_id: "" };
  const line: FlightStep[] = [
    { id: "a", kind: "agent", title: "Improve the sky", config: {}, on_fail: "halt" },
    { id: "b", kind: "command", title: "Screenshot", config: {}, on_fail: "halt", group: g },
    { id: "c", kind: "agent", title: "Judge it", config: {}, on_fail: "halt", group: g },
    { id: "l", kind: "loop", title: "Loop ×3", config: { back_to: "c", times: 3 }, on_fail: "halt" },
    { id: "z", kind: "ship", title: "Push", config: {}, on_fail: "halt" },
  ];
  const node = stop({ id: "stop:l", title: "Loop ×3", icon: "loop", step: line[3], index: 3 });
  const html = render({ node, def: LOOP, lineSteps: line, onSave: () => {} });
  const radios = (h: string) => [...h.matchAll(/role="radio" aria-checked="(true|false)" class="[^"]*"(?: title="[^"]*")? data-value="([^"]*)">([^<]+)</g)].map((m) => [m[2], m[3], m[1]]);

  it("“Repeat from”: the first stop, then every stop before the loop by title — none after it", () => {
    const field = html.slice(html.indexOf('data-dynamic="stops_before"'));
    const chips = radios(field.slice(0, field.indexOf(">Runs in all<")));
    expect(chips).toEqual([
      ["", "the first stop", "false"],
      ["a", "Improve the sky", "false"],
      ["b", "Screenshot", "false"],
      ["c", "Judge it", "true"],
    ]);
    expect(html).not.toContain('data-value="z"');
    expect(html).not.toContain("<select");
  });

  it("a group's sub-steps sit in a row under the group's name", () => {
    expect(html).toContain('<div class="fl-stops-before-run fl-stops-before-group"><span class="fl-stops-before-name">Polish</span>');
    expect(html).toContain('aria-label="Repeat from — Polish"');
  });

  it("“Until” is the when editor (mono, ⓘ), with the server's placeholder", () => {
    expect(html).toMatch(/<input type="text" class="fl-input fl-mono fl-when-input" aria-label="Until" maxLength="\d+" placeholder="e.g. checks green"/);
    // …and the stop's own "Only when" is still there.
    expect(html).toContain('aria-label="Only when"');
  });

  it("without the line: only “the first stop”; a stale choice stays visible", () => {
    const bare = render({ node: stop({ id: "stop:l", step: { ...line[3], config: { back_to: "gone" } } }), def: LOOP, onSave: () => {} });
    expect(radios(bare.slice(bare.indexOf('data-dynamic="stops_before"'))).slice(0, 2)).toEqual([
      ["", "the first stop", "false"],
      ["gone", "a stop no longer before this one", "true"],
    ]);
  });

  it("both line hooks hand the dialog the line's steps", () => {
    expect(code("components/flights/useSessionLine.tsx")).toContain("lineSteps={flight?.steps}");
    expect(code("components/flights/useGroundedLine.tsx")).toContain("lineSteps={flight.steps}");
  });
});
