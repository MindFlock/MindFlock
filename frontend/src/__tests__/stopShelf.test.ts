/** The Sky's one shelf row, Stops (StopShelf), in three regions: **Blocks** —
 * the Session chip first, then the default kinds the server lists (Agent,
 * Perch, Ship, Verify, Wait), each draggable with its kind under STOP_MIME;
 * **Multi-step** (the saved stops: stopShelfSaved.test.ts); then **All
 * blocks…** (the catalog: stopCatalog.test.ts). A click tacks a kind on
 * through `onTack`, without it a chip only drags; zero kinds say the list
 * didn't load; the shelf folds to a handle. Server-rendered with the kinds
 * seeded in a QueryClient. */

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
const { StopShelf, sessionDragData } = await import("../components/flights/StopShelf");
const { stopDragData } = await import("../lib/stopCatalog");

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
// The registry's order (the server's); the shelf shows its own.
const KINDS = [kind("agent", "Agent"), kind("split", "Split"), kind("join", "Join"), kind("ship", "Ship"), kind("verify", "Verify"), kind("you", "You", "you"), kind("wait", "Wait"), kind("notify", "Notify")];
const NO_VERIFY = KINDS.filter((k) => k.kind !== "verify");

function render(el: ReactElement, kinds: StepKindDef[] | null | undefined = KINDS) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  if (kinds !== undefined) c.setQueryData(["flight-kinds"], kinds);
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}
const kindsIn = (html: string) => [...html.matchAll(/data-kind="([^"]+)"/g)].map((m) => m[1]);

describe("the stops row", () => {
  it("Blocks: Session, then the default kinds in their order, the perch gold and named Perch", () => {
    const html = render(createElement(StopShelf, { onTack() {} }));
    expect(html).toContain('aria-label="Stops"');
    expect(html).toContain('<strong>Stops</strong> <span class="sky-stops-hint">— drag onto the sky or a line</span>');
    expect(kindsIn(html)).toEqual(["agent", "you", "ship", "verify", "wait"]);
    // The five kinds and the Session chip (All blocks… opens a list, it doesn't drag).
    expect(html.match(/draggable="true"/g)).toHaveLength(6);
    const perch = html.slice(html.indexOf('data-kind="you"'));
    expect(html).toMatch(/class="sky-stop-chip perch" data-kind="you"/);
    expect(perch).toContain('<span class="sky-stop-name">Perch</span>');
    expect(perch).toContain("fl-stop-icon-perch");
    expect(html).not.toContain(">You<");
    // One region, its tiny label first, the Session chip, then the kinds.
    const blocks = html.slice(html.indexOf('data-region="blocks"'));
    expect(html).toContain('<div class="sky-stops-region" data-region="blocks" role="list" aria-label="Blocks"><span class="sky-stops-label" aria-hidden="true">Blocks</span>');
    expect(blocks.indexOf("sky-session-chip")).toBeLessThan(blocks.indexOf('data-kind="agent"'));
    expect(html.indexOf('data-kind="wait"')).toBeLessThan(html.indexOf("sky-catalog-chip"));
    // Tooltip: the kind's summary.
    expect(html).toContain('title="Ship does its thing\nClick to tack it onto the focused line, or drag it onto a line or the sky"');
    expect(html).not.toMatch(/\b(node|plane)\b/i);
  });

  it("the other kinds live in the catalog; a default the server doesn't list is skipped", () => {
    const html = render(createElement(StopShelf, {}));
    for (const k of ["Split", "Join", "Notify"]) expect(html).not.toContain(">" + k + "<");
    expect(kindsIn(render(createElement(StopShelf, {}), NO_VERIFY))).toEqual(["agent", "you", "ship", "wait"]);
  });

  it("then the All blocks… chip: the secondary chip, a ⊞, opens the catalog (no saved stops: no Multi-step region)", () => {
    const html = render(createElement(StopShelf, { onTack() {} }));
    expect(html).toContain(
      '<button type="button" class="sky-stop-chip sky-catalog-chip" aria-haspopup="dialog" aria-expanded="false" title="Every block and every multi-step block, in a list you can filter"><span class="sky-stop-icon" aria-hidden="true">⊞</span><span class="sky-stop-name">All blocks…</span></button>'
    );
    expect(html).not.toContain('data-region="multi"');
    // A thin divider between Blocks and the chip; the catalog isn't open.
    expect(html.lastIndexOf("sky-stops-divider")).toBeGreaterThan(html.indexOf('data-kind="wait"'));
    expect(html.lastIndexOf("sky-stops-divider")).toBeLessThan(html.indexOf("sky-catalog-chip"));
    expect(html).not.toContain("sky-catalog-modal");
    const src = code("components/flights/StopShelf.tsx");
    expect(src).toContain("onClick={() => setCatalog(true)}");
    expect(src).toContain("{catalog && <StopCatalog kinds={kinds || []} plans={saved} onAdd={onTack || onTackPlan ? add : undefined} onClose={() => setCatalog(false)} />}");
    // Add goes through the chips' own props.
    expect(src).toMatch(/if \("kind" in d\) onTack\?\.\(d\.kind\);/);
    expect(src).toMatch(/if \(plan\) onTackPlan\?\.\(plan\);/);
  });

  it("with onTack a chip is a button; without, it only drags and says so", () => {
    const tack = render(createElement(StopShelf, { onTack() {} }));
    expect(tack).toMatch(/<button type="button" class="sky-stop-chip" data-kind="agent" draggable="true"/);
    expect(tack).toContain('aria-label="Tack “Ship” onto the focused line"');
    const drag = render(createElement(StopShelf, {}));
    // Only All blocks… is still a button: it opens the catalog, it doesn't tack.
    expect(drag.match(/<button type="button" class="sky-stop-chip[^"]*"/g)).toEqual(['<button type="button" class="sky-stop-chip sky-catalog-chip"']);
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
    // The regions' labels: tiny and muted.
    expect(css).toMatch(/\.sky-stops-label \{[^}]*color: var\(--fl-ink-muted, var\(--muted\)\);[^}]*font-size: 9\.5px;/);
  });
});
