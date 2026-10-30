/** The shelf's stops row (StopShelf): a chip per stop kind the server lists
 * (the flock's stops in registry order, then the perches), each draggable with
 * its kind under STOP_MIME; a click tacks it on through `onTack`, without it a
 * chip only drags; zero kinds say the list didn't load. Server-rendered with
 * the kinds seeded in a QueryClient, like skyFlyways.test.ts. */

import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code, css as cssOf } from "./sourceText";
import type { StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { STOP_MIME } = await import("../lib/flights");
const { StopShelf, stopChipOrder, stopDragData } = await import("../components/flights/StopShelf");
const { FlywayShelf } = await import("../components/flights/FlywayShelf");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const kind = (k: string, label: string, actor: "robot" | "you" = "robot"): StepKindDef => ({
  kind: k,
  label,
  actor,
  summary: label + " does its thing",
  fields: [],
  default_title: label,
});
// A perch listed mid-registry: the shelf still puts it last.
const KINDS = [kind("agent", "Agent"), kind("you", "You", "you"), kind("ship", "Ship"), kind("split", "Split"), kind("join", "Join")];
const NO_SPLIT = KINDS.filter((k) => k.kind !== "split" && k.kind !== "join");

function render(el: ReactElement, kinds: StepKindDef[] | null | undefined = KINDS) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  if (kinds !== undefined) c.setQueryData(["flight-kinds"], kinds);
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}
const kindsIn = (html: string) => [...html.matchAll(/data-kind="([^"]+)"/g)].map((m) => m[1]);

describe("stopChipOrder", () => {
  it("the flock's stops in registry order, perches last", () => {
    expect(stopChipOrder(KINDS).map((k) => k.kind)).toEqual(["agent", "ship", "split", "join", "you"]);
    expect(stopChipOrder([kind("you", "You", "you"), kind("you2", "Sign off", "you"), kind("agent", "Agent")]).map((k) => k.kind)).toEqual(["agent", "you", "you2"]);
    expect(stopChipOrder([])).toEqual([]);
  });
});

describe("the stops row", () => {
  it("a draggable chip per kind, its kind in data-kind, the perch gold and named Perch after a divider", () => {
    const html = render(createElement(StopShelf, { onTack() {} }));
    expect(html).toContain('aria-label="Stops"');
    expect(html).toContain('<strong>Stops</strong> <span class="sky-stops-hint">— drag onto a line, or into a flyway</span>');
    expect(kindsIn(html)).toEqual(["agent", "ship", "split", "join", "you"]);
    expect(html.match(/draggable="true"/g)).toHaveLength(5);
    const perch = html.slice(html.indexOf('data-kind="you"'));
    expect(html).toMatch(/class="sky-stop-chip perch" data-kind="you"/);
    expect(perch).toContain('<span class="sky-stop-name">Perch</span>');
    expect(perch).toContain("fl-stop-icon-perch");
    expect(html).not.toContain(">You<");
    // The divider sits between the flock's last stop and the perch.
    expect(html.indexOf("sky-stops-divider")).toBeGreaterThan(html.indexOf('data-kind="join"'));
    expect(html.indexOf("sky-stops-divider")).toBeLessThan(html.indexOf('data-kind="you"'));
    // Tooltip: the kind's summary.
    expect(html).toContain('title="Ship does its thing\nClick to tack it onto the focused line, or drag it onto a line"');
    expect(html).not.toMatch(/\b(node|plane)\b/i);
  });

  it("Split and Join only when the server lists them", () => {
    const html = render(createElement(StopShelf, {}), NO_SPLIT);
    expect(kindsIn(html)).toEqual(["agent", "ship", "you"]);
    expect(html).not.toContain("Split");
    expect(html).not.toContain("Join");
  });

  it("with onTack a chip is a button; without, it only drags and says so", () => {
    const tack = render(createElement(StopShelf, { onTack() {} }));
    expect(tack).toMatch(/<button type="button" class="sky-stop-chip" data-kind="agent" draggable="true"/);
    expect(tack).toContain('aria-label="Tack “Ship” onto the focused line"');
    const drag = render(createElement(StopShelf, {}));
    expect(drag).not.toContain("<button");
    expect(drag).toMatch(/<span class="sky-stop-chip drag-only" data-kind="agent" draggable="true" title="Agent does its thing\nDrag onto a line"/);
    const src = code("components/flights/StopShelf.tsx");
    expect(src).toMatch(/onClick=\{\(\) => onTack\(def\.kind\)\}/);
  });

  it("zero kinds: a muted line that the stop list didn't load", () => {
    for (const none of [[], null]) {
      const html = render(createElement(StopShelf, {}), none);
      expect(html).toContain('class="sky-stops-empty muted"');
      expect(html).toContain("The stop list didn&#x27;t load");
      expect(html).not.toContain("sky-stop-chip");
    }
    // Still loading: the row's head, no chips and no complaint yet.
    const loading = render(createElement(StopShelf, {}), undefined);
    expect(loading).toContain("Stops");
    expect(loading).not.toContain("didn't load");
  });

  it("a chip's drag carries the kind under STOP_MIME, the name as text, as a copy", () => {
    expect(STOP_MIME).toBe("application/x-mindflock-stop");
    expect(stopDragData(kind("ship", "Ship"))).toEqual([
      ["application/x-mindflock-stop", "ship"],
      ["text/plain", "Ship"],
    ]);
    expect(stopDragData(kind("you", "You", "you"))).toEqual([
      ["application/x-mindflock-stop", "you"],
      ["text/plain", "Perch"],
    ]);
    const src = code("components/flights/StopShelf.tsx");
    expect(src).toMatch(/for \(const \[type, value\] of stopDragData\(def\)\) e\.dataTransfer\.setData\(type, value\)/);
    expect(src).toMatch(/e\.dataTransfer\.effectAllowed = "copy"/);
  });

  it("rides under the flyways on the shelf, hands onTack down, and folds with it", () => {
    const open = render(createElement(FlywayShelf, { plans: [], onNew() {}, onFly() {}, onEdit() {}, onTack() {} }));
    expect(open.indexOf('class="sky-stops"')).toBeGreaterThan(open.indexOf("+ New flyway"));
    expect(open).toContain('aria-label="Tack “Agent” onto the focused line"');
    const folded = render(createElement(FlywayShelf, { plans: [], onNew() {}, onFly() {}, onEdit() {}, collapsed: true }));
    expect(folded).not.toContain("sky-stops");
  });

  it("a phone hides the drag hint; the row scrolls sideways instead of wrapping", () => {
    const css = cssOf("components/flights/FlywayShelf.css");
    expect(css).toMatch(/@media \(hover: none\) \{\s*\.sky-stops-hint \{\s*display: none;/);
    expect(css).toMatch(/\.sky-stops-chips \{[^}]*overflow-x: auto;[^}]*scrollbar-width: thin;/);
    expect(css).toMatch(/\.sky-stop-chip \{[^}]*height: 26px;/);
  });
});
