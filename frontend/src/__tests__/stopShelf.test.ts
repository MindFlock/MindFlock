/** The Sky's one shelf row, Stops (StopShelf): the Session chip first, then
 * a chip per stop kind the server lists (the flock's stops in registry order,
 * then the perches), each draggable with its kind under STOP_MIME; a click
 * tacks it on through `onTack`, without it a chip only drags; zero kinds say
 * the list didn't load; the shelf folds to a handle. Server-rendered with the
 * kinds seeded in a QueryClient. The saved stops: stopShelfSaved.test.ts. */

import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code, css as cssOf } from "./sourceText";
import type { StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { STOP_MIME } = await import("../lib/flights");
const { NEW_SESSION_MIME } = await import("../lib/skyLayout");
const { StopShelf, stopChipOrder, stopDragData, sessionDragData } = await import("../components/flights/StopShelf");

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
    expect(html).toContain('<strong>Stops</strong> <span class="sky-stops-hint">— drag onto the sky or a line</span>');
    expect(kindsIn(html)).toEqual(["agent", "ship", "split", "join", "you"]);
    // The five kinds and the Session chip.
    expect(html.match(/draggable="true"/g)).toHaveLength(6);
    const perch = html.slice(html.indexOf('data-kind="you"'));
    expect(html).toMatch(/class="sky-stop-chip perch" data-kind="you"/);
    expect(perch).toContain('<span class="sky-stop-name">Perch</span>');
    expect(perch).toContain("fl-stop-icon-perch");
    expect(html).not.toContain(">You<");
    // A divider after the Session chip, and one between the flock's last
    // stop and the perch.
    expect(html.indexOf("sky-stops-divider")).toBeLessThan(html.indexOf('data-kind="agent"'));
    expect(html.lastIndexOf("sky-stops-divider")).toBeGreaterThan(html.indexOf('data-kind="join"'));
    expect(html.lastIndexOf("sky-stops-divider")).toBeLessThan(html.indexOf('data-kind="you"'));
    // Tooltip: the kind's summary.
    expect(html).toContain('title="Ship does its thing\nClick to tack it onto the focused line, or drag it onto a line or the sky"');
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
    expect(drag).not.toContain('<button type="button" class="sky-stop-chip');
    expect(drag).toMatch(/<span class="sky-stop-chip drag-only" data-kind="agent" draggable="true" title="Agent does its thing\nDrag onto a line or the sky"/);
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

  it("the Session chip comes first: it drags the new-session type, a click opens the block, and it carries the tour's hook", () => {
    const html = render(createElement(StopShelf, { onSession() {}, onTack() {} }));
    const chips = html.slice(html.indexOf('class="sky-stops-chips"'));
    expect(chips.indexOf("sky-session-chip")).toBeGreaterThan(0);
    expect(chips.indexOf("sky-session-chip")).toBeLessThan(chips.indexOf('data-kind="agent"'));
    expect(html).toMatch(/<button type="button" class="sky-stop-chip sky-session-chip" data-tour="flights-newblock" draggable="true"/);
    expect(html).toContain('aria-label="Start a new session"');
    expect(html).toContain('<span class="sky-stop-name">Session</span>');
    expect(NEW_SESSION_MIME).toBe("application/x-mindflock-new-session");
    expect(sessionDragData()).toEqual([
      [NEW_SESSION_MIME, "session"],
      ["text/plain", "New session"],
    ]);
    // Without onSession it only drags.
    expect(render(createElement(StopShelf, {}))).toMatch(/<span class="sky-stop-chip sky-session-chip drag-only" data-tour="flights-newblock"/);
  });

  it("the shelf is shown unless folded; folded, only its handle", () => {
    const open = render(createElement(StopShelf, { onTack() {} }));
    expect(open).toMatch(/^<section class="sky-shelf open" data-tour="sky-shelf"/);
    expect(open).toContain('<button type="button" class="sky-shelf-fold" aria-expanded="true"');
    const folded = render(createElement(StopShelf, { folded: true }));
    expect(folded).toMatch(/^<section class="sky-shelf folded"/);
    expect(folded).toContain('aria-expanded="false"');
    expect(folded).not.toContain("sky-stops");
    // The sky no longer starts it folded (the old mf_sky_shelf) or has a
    // "Flyways & stops" tab.
    const sky = code("components/flights/SkyView.tsx");
    expect(sky).not.toContain("mf_sky_shelf");
    expect(sky).not.toContain("Flyways &amp; stops");
    expect(sky).toContain('const [shelfFolded, setShelfFolded] = useState(() => readPref(SHELF_FOLD_KEY) === "1");');
  });

  it("a phone hides the drag hint; the row scrolls sideways instead of wrapping", () => {
    const css = cssOf("components/flights/StopShelf.css");
    expect(css).toMatch(/@media \(hover: none\) \{\s*\.sky-stops-hint \{\s*display: none;/);
    expect(css).toMatch(/\.sky-stops-chips \{[^}]*overflow-x: auto;[^}]*scrollbar-width: thin;/);
    expect(css).toMatch(/\.sky-stop-chip \{[^}]*height: var\(--fl-ctl-h, 28px\);/);
  });
});
