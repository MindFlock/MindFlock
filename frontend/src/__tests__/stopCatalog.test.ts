/** The shelf's catalog (docs/flights.md "As built: round 12 shelf"):
 * lib/stopCatalog — the default blocks (DEFAULT_KINDS: their order, a kind
 * the server doesn't list skipped), every kind's order, the filter, the rows,
 * what a row's drag carries — and StopCatalog, the All blocks… Modal: Blocks
 * and Multi-step, a filter with the focus, rows draggable with their chip's
 * MIME and an Add, ↑ / ↓ / Enter, and the Modal letting a drag through. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code, css as cssOf } from "./sourceText";
import type { FlightPlan, FlightStep, StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));

const { STOP_MIME } = await import("../lib/flights");
const { FLYWAY_MIME } = await import("../lib/skyLayout");
const { DEFAULT_KINDS, catalogKinds, catalogMatch, catalogRows, defaultKinds, filterKinds, filterPlans, planPath, savedDragData, stopDragData } = await import("../lib/stopCatalog");
const { StopCatalog } = await import("../components/flights/StopCatalog");

const kind = (k: string, label: string, actor: "robot" | "you" = "robot", summary = label + " does its thing"): StepKindDef => ({
  kind: k,
  label,
  actor,
  summary,
  fields: [],
  default_title: label,
});
// The server's registry order.
const KINDS = [
  kind("agent", "Agent", "robot", "Give the session's agent a prompt"),
  kind("split", "Split"),
  kind("ship", "Ship", "robot", "Commit, push, open the PR"),
  kind("verify", "Verify"),
  kind("you", "You", "you", "Wait for you to look"),
  kind("wait", "Wait"),
  kind("notify", "Notify", "robot", "Send a message to Slack"),
];
const step = (i: number, k = "ship", title = "Step " + i): FlightStep => ({ id: "s" + i, kind: k, title, config: {}, on_fail: "halt" });
const plan = (id: string, name: string, steps: FlightStep[], builtin = false, description = ""): FlightPlan => ({
  id,
  name,
  description,
  builtin,
  trigger: { kind: "manual", schedule: null, paused: false },
  session: { repo_path: "", program: "", model: "", in_place: false },
  steps,
  updated: 1,
});
const TICKET = plan("builtin:ticket", "Ticket to merge", [step(1, "agent", "Work the ticket"), step(2, "ship", "Push"), step(3, "you", "Approve")], true, "From a ticket to a merged PR");
const MINE = plan("p_mine", "Mine", [step(1, "agent", "Fix")]);

const render = (props: Record<string, unknown> = {}) =>
  renderToStaticMarkup(createElement(StopCatalog, { kinds: KINDS, plans: [TICKET, MINE], onClose() {}, portal: false, ...props } as never));

describe("the default blocks (DEFAULT_KINDS)", () => {
  it("Agent, Perch, Ship, Verify, Wait — in that order, whatever the registry's", () => {
    expect([...DEFAULT_KINDS]).toEqual(["agent", "you", "ship", "verify", "wait"]);
    expect(defaultKinds(KINDS).map((k) => k.kind)).toEqual(["agent", "you", "ship", "verify", "wait"]);
    expect(defaultKinds([...KINDS].reverse()).map((k) => k.kind)).toEqual(["agent", "you", "ship", "verify", "wait"]);
  });

  it("a kind the server doesn't list is skipped", () => {
    expect(defaultKinds(KINDS.filter((k) => k.kind !== "verify" && k.kind !== "you")).map((k) => k.kind)).toEqual(["agent", "ship", "wait"]);
    expect(defaultKinds([kind("notify", "Notify")])).toEqual([]);
    expect(defaultKinds([])).toEqual([]);
  });

  it("the catalog lists every kind: the defaults first, then the rest in registry order", () => {
    expect(catalogKinds(KINDS).map((k) => k.kind)).toEqual(["agent", "you", "ship", "verify", "wait", "split", "notify"]);
  });
});

describe("the filter", () => {
  it("every word, case-blind, in one of the texts; empty matches all", () => {
    expect(catalogMatch("", "x")).toBe(true);
    expect(catalogMatch("  ", "x")).toBe(true);
    expect(catalogMatch("PUSH pr", "Commit, push, open the PR")).toBe(true);
    expect(catalogMatch("push merge", "Commit, push, open the PR")).toBe(false);
    expect(catalogMatch("ship", undefined, "Ship")).toBe(true);
  });

  it("kinds by label (Perch for you), kind and summary", () => {
    const kinds = catalogKinds(KINDS);
    expect(filterKinds(kinds, "perch").map((k) => k.kind)).toEqual(["you"]);
    expect(filterKinds(kinds, "notify").map((k) => k.kind)).toEqual(["notify"]);
    expect(filterKinds(kinds, "slack").map((k) => k.kind)).toEqual(["notify"]);
    expect(filterKinds(kinds, "prompt").map((k) => k.kind)).toEqual(["agent"]);
    expect(filterKinds(kinds, "")).toHaveLength(7);
  });

  it("saved stops by name and description", () => {
    expect(filterPlans([TICKET, MINE], "mine").map((p) => p.id)).toEqual(["p_mine"]);
    expect(filterPlans([TICKET, MINE], "merged").map((p) => p.id)).toEqual(["builtin:ticket"]);
    expect(filterPlans([TICKET, MINE], "nothing")).toEqual([]);
  });

  it("the rows ↑ / ↓ walk: the kinds, then the saved stops; a saved stop's path", () => {
    expect(catalogRows([KINDS[0]], [TICKET])).toEqual([{ kind: "agent" }, { planId: "builtin:ticket" }]);
    expect(planPath(TICKET)).toBe("Work the ticket → Push → Approve");
    expect(planPath(plan("x", "X", []))).toBe("");
  });
});

describe("a row's drag carries its chip's MIME", () => {
  it("a kind: STOP_MIME = the kind; a saved stop: FLYWAY_MIME = the id; the name as text", () => {
    expect(stopDragData(KINDS[2])).toEqual([
      [STOP_MIME, "ship"],
      ["text/plain", "Ship"],
    ]);
    expect(stopDragData(KINDS[4])).toEqual([
      [STOP_MIME, "you"],
      ["text/plain", "Perch"],
    ]);
    expect(savedDragData(TICKET)).toEqual([
      [FLYWAY_MIME, "builtin:ticket"],
      ["text/plain", "Ticket to merge"],
    ]);
    const src = code("components/flights/StopCatalog.tsx");
    expect(src).toContain("onDragStart={(e) => onDragStart(e, stopDragData(k))}");
    expect(src).toContain("onDragStart={(e) => onDragStart(e, savedDragData(p))}");
    expect(src).toMatch(/for \(const \[type, value\] of data\) e\.dataTransfer\.setData\(type, value\);\s*e\.dataTransfer\.effectAllowed = "copy";/);
  });
});

describe("StopCatalog", () => {
  const html = render({ onAdd() {} });

  it("a Modal dialog titled All blocks, its filter focused, a Close", () => {
    expect(html).toMatch(/^<div id="sky-catalog" role="dialog" aria-modal="true" aria-labelledby="sky-catalog-title" tabindex="-1" class="modal sky-catalog-modal">/);
    expect(html).toContain('<h2 id="sky-catalog-title">All blocks</h2>');
    expect(html).toContain('<button type="button" class="fl-icon-btn sky-catalog-close" aria-label="Close" title="Close (Esc)">×</button>');
    expect(html).toMatch(/<input class="fl-input sky-catalog-filter" type="text" placeholder="Filter blocks…" aria-label="Filter blocks" aria-controls="sky-catalog-list" aria-activedescendant="sky-catalog-row-0"/);
    expect(code("components/flights/StopCatalog.tsx")).toMatch(/<input[\s\S]*?autoFocus[\s\S]*?\/>/);
  });

  it("Blocks: every kind — icon, name, robot / you badge, its summary — the defaults first", () => {
    const blocks = html.slice(html.indexOf('data-section="blocks"'), html.indexOf('data-section="multi"'));
    expect(blocks).toContain('<h3 class="sky-catalog-label">Blocks</h3>');
    expect([...blocks.matchAll(/data-kind="([^"]+)"/g)].map((m) => m[1])).toEqual(["agent", "you", "ship", "verify", "wait", "split", "notify"]);
    expect(blocks).toContain('<span class="sky-catalog-name">Agent<span class="sky-catalog-badge actor-robot">robot</span></span><span class="sky-catalog-summary">Give the session&#x27;s agent a prompt</span>');
    const perch = blocks.slice(blocks.indexOf('data-kind="you"'));
    expect(blocks).toMatch(/class="sky-catalog-row perch" data-kind="you" draggable="true"/);
    expect(perch).toContain('<span class="sky-catalog-name">Perch<span class="sky-catalog-badge actor-you">you</span></span>');
    expect(perch).toContain("fl-stop-icon-perch");
  });

  it("Multi-step: every saved stop — a box per step, the name, built-in, its path", () => {
    const multi = html.slice(html.indexOf('data-section="multi"'));
    expect(multi).toContain('<h3 class="sky-catalog-label">Multi-step</h3>');
    expect([...multi.matchAll(/data-plan="([^"]+)"/g)].map((m) => m[1])).toEqual(["builtin:ticket", "p_mine"]);
    expect(multi).toContain('<span class="sky-catalog-boxes" aria-hidden="true"><i class=""></i><i class=""></i><i class="perch"></i></span>');
    expect(multi).toContain('<span class="sky-catalog-name">Ticket to merge<span class="sky-catalog-badge builtin">built-in</span></span><span class="sky-catalog-summary">Work the ticket → Push → Approve</span>');
    expect(multi).toContain('<span class="sky-catalog-name">Mine</span>');
  });

  it("each row draggable, the first picked, with an Add (the secondary tier)", () => {
    expect(html.match(/role="option"/g)).toHaveLength(9);
    expect(html.match(/draggable="true"/g)).toHaveLength(9);
    expect(html).toContain('<div id="sky-catalog-row-0" role="option" aria-selected="true" class="sky-catalog-row active" data-kind="agent" draggable="true"');
    expect(html).toContain('id="sky-catalog-row-7" role="option" aria-selected="false" class="sky-catalog-row sky-catalog-plan" data-plan="builtin:ticket"');
    expect(html).toContain('<button type="button" class="test-btn sky-catalog-add" tabindex="-1" aria-label="Add “Perch”">Add</button>');
    expect(html).toContain('aria-label="Add “Ticket to merge”"');
    expect(html.match(/sky-catalog-add/g)).toHaveLength(9);
  });

  it("without onAdd the rows only drag", () => {
    const drag = render();
    expect(drag).not.toContain("sky-catalog-add");
    expect(drag).toContain('title="Drag onto a line or the sky"');
    expect(drag).toContain("Drag a block onto a line or the sky · Esc closes");
  });

  it("nothing at all: says so", () => {
    const none = render({ kinds: [], plans: [] });
    expect(none).toContain('<p class="sky-catalog-empty muted">No blocks yet — this server may not know Flights.</p>');
    expect(none).not.toContain("data-section");
  });

  it("↑ / ↓ move the pick from the filter, Enter adds it; Add and Enter call onAdd({kind} | {planId})", () => {
    const src = code("components/flights/StopCatalog.tsx");
    expect(src).toMatch(/if \(e\.key === "ArrowDown" \|\| e\.key === "ArrowUp"\) \{\s*e\.preventDefault\(\);/);
    expect(src).toMatch(/\} else if \(e\.key === "Enter"\) \{\s*e\.preventDefault\(\);\s*if \(at >= 0\) add\(rows\[at\]\);/);
    expect(src).toContain("{addButton({ kind: k.kind }, name)}");
    expect(src).toContain("{addButton({ planId: p.id }, p.name)}");
    // A key typed in the catalog stays there (Escape still reaches the Modal).
    expect(src).toContain('onKeyDown={(e) => e.key === "Escape" || e.stopPropagation()}');
  });

  it("a drag goes through: the Modal turns see-through a tick after dragstart; a drop that landed closes it", () => {
    const src = code("components/flights/StopCatalog.tsx");
    expect(src).toContain("setTimeout(() => setDragging(true), 0);");
    expect(src).toMatch(/className=\{"sky-catalog-modal" \+ \(dragging \? " is-dragging" : ""\)\}/);
    expect(src).toMatch(/setDragging\(false\);\s*if \(e\.dataTransfer && e\.dataTransfer\.dropEffect !== "none"\) onClose\(\);/);
    const css = cssOf("components/flights/StopCatalog.css");
    expect(css).toMatch(/\.modal\.sky-catalog-modal\.is-dragging \{\s*background: transparent;\s*pointer-events: none;\s*\}/);
    expect(css).toMatch(/\.sky-catalog-modal\.is-dragging \.sky-catalog \{\s*opacity: 0\.35;/);
  });

  it("no native dialogs", () => {
    expect(code("components/flights/StopCatalog.tsx")).not.toMatch(/\bwindow\.(prompt|alert|confirm)\(/);
  });
});
