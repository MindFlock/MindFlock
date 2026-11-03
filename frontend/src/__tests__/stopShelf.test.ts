/** The Sky's one shelf row, Stops (StopShelf), in three regions: **Blocks** —
 * the Session chip first, then the default kinds the server lists (Agent,
 * Perch, Ship, Verify, Wait), each draggable with its kind under STOP_MIME;
 * **Multi-step** (the saved stops: stopShelfSaved.test.ts); then **All
 * blocks…** (round 14: `onCatalog`, the host floats the palette large; else
 * the catalog as a dialog: stopCatalog.test.ts). A click or a double-click
 * places once (`onPlace`) or tacks a kind on (`onTack`); without either a
 * chip only drags; zero kinds say the list didn't load; the shelf folds to a
 * handle. Server-rendered with the kinds seeded in a QueryClient. */

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
  it("Stops: Session, then the default kinds in their order, the perch gold and named Perch", () => {
    const html = render(createElement(StopShelf, { onTack() {} }));
    expect(html).toContain('aria-label="Stops"');
    expect(html).toContain('<strong>Stops</strong> <span class="sky-stops-hint">— drag onto the sky or a route</span>');
    expect(kindsIn(html)).toEqual(["agent", "you", "ship", "verify", "wait"]);
    // The five kinds and the Session chip (All blocks… opens a list, it doesn't drag).
    expect(html.match(/draggable="true"/g)).toHaveLength(6);
    const perch = html.slice(html.indexOf('data-kind="you"'));
    expect(html).toMatch(/class="sky-stop-chip perch" data-kind="you"/);
    expect(perch).toContain('<span class="sky-stop-name">Wait for me</span>');
    expect(perch).toContain("fl-stop-icon-perch");
    expect(html).not.toContain(">You<");
    // One region, its tiny label first, the Session chip, then the kinds.
    const blocks = html.slice(html.indexOf('data-region="blocks"'));
    expect(html).toContain('<div class="sky-stops-region" data-region="blocks" role="list" aria-label="Stops"><span class="sky-stops-label" aria-hidden="true">Stops</span>');
    expect(blocks.indexOf("sky-session-chip")).toBeLessThan(blocks.indexOf('data-kind="agent"'));
    expect(html.indexOf('data-kind="wait"')).toBeLessThan(html.indexOf("sky-catalog-chip"));
    // Tooltip: the kind's summary.
    expect(html).toContain('title="Ship does its thing\nClick to tack it onto the focused route, or drag it onto a route or the sky — right-click for more"');
    expect(html).not.toMatch(/\b(node|plane)\b/i);
  });

  it("round 16: the server's Loop is a default block after Wait, drawn with ↻", () => {
    const html = render(createElement(StopShelf, { onTack() {} }), [...KINDS, kind("loop", "Loop")]);
    expect(kindsIn(html)).toEqual(["agent", "you", "ship", "verify", "wait", "loop"]);
    const loop = html.slice(html.indexOf('data-kind="loop"'));
    expect(loop).toContain('<span class="sky-stop-name">Loop</span>');
    expect(loop).toContain('<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"></path>');
  });

  it("the other kinds live in the catalog; a default the server doesn't list is skipped", () => {
    const html = render(createElement(StopShelf, {}));
    for (const k of ["Split", "Join", "Notify"]) expect(html).not.toContain(">" + k + "<");
    expect(kindsIn(render(createElement(StopShelf, {}), NO_VERIFY))).toEqual(["agent", "you", "ship", "wait"]);
  });

  it("then the All stops… chip: the secondary chip, a ⊞, opens the catalog (no saved stops: no Saved routes region)", () => {
    const html = render(createElement(StopShelf, { onTack() {} }));
    expect(html).toContain(
      '<button type="button" class="sky-stop-chip sky-catalog-chip" data-tour="sky-catalog" aria-haspopup="dialog" aria-expanded="false" title="Every stop and every saved route, in a list you can filter"><span class="sky-stop-icon" aria-hidden="true">⊞</span><span class="sky-stop-name">All stops…</span></button>'
    );
    expect(html).not.toContain('data-region="multi"');
    // A thin divider between Blocks and the chip; the catalog isn't open.
    expect(html.lastIndexOf("sky-stops-divider")).toBeGreaterThan(html.indexOf('data-kind="wait"'));
    expect(html.lastIndexOf("sky-stops-divider")).toBeLessThan(html.indexOf("sky-catalog-chip"));
    expect(html).not.toContain("sky-catalog-modal");
    const src = code("components/flights/StopShelf.tsx");
    expect(src).toContain("onClick={() => (onCatalog ? onCatalog() : setCatalog(true))}");
    expect(src).toContain("<StopCatalog");
    expect(src).toContain("plans={allSaved}");
    expect(src).toContain("shelf={(d) => onShelf(shelf, d)}");
    expect(src).toContain("onShelfToggle={(d) => changeShelf((p) => toggleShelf(p, d))}");
    // Add goes through the chips' own props.
    expect(src).toMatch(/if \("kind" in d\) onTack\?\.\(d\.kind\);/);
    expect(src).toMatch(/if \(plan\) onTackPlan\?\.\(plan\);/);
  });

  it("with onTack a chip is a button; without, it only drags and says so", () => {
    const tack = render(createElement(StopShelf, { onTack() {} }));
    expect(tack).toMatch(/<button type="button" class="sky-stop-chip" data-kind="agent" draggable="true"/);
    expect(tack).toContain('aria-label="Tack “Ship” onto the focused route"');
    const drag = render(createElement(StopShelf, {}));
    // Only All blocks… is still a button: it opens the catalog, it doesn't tack.
    expect(drag.match(/<button type="button" class="sky-stop-chip[^"]*"/g)).toEqual(['<button type="button" class="sky-stop-chip sky-catalog-chip"']);
    expect(drag).toMatch(/<span class="sky-stop-chip drag-only" data-kind="agent" draggable="true" title="Agent does its thing\nDrag onto a route or the sky — right-click for more"/);
    const src = code("components/flights/StopShelf.tsx");
    expect(src).toContain("const cd = useClickOrDouble(onClick && (() => onClick(def.kind)));");
    expect(src).toContain("const clickKind = onPlace ? (kind: string) => onPlace({ kind }) : onTack;");
  });

  it("round 14 — All stops… with onCatalog: the host floats the palette (no dialog of its own)", () => {
    const html = render(createElement(StopShelf, { onPlace() {}, onCatalog() {} }));
    expect(html).toContain(
      '<button type="button" class="sky-stop-chip sky-catalog-chip" data-tour="sky-catalog" title="Every stop and saved route, in a palette on the Sky you can filter and drag from"><span class="sky-stop-icon" aria-hidden="true">⊞</span><span class="sky-stop-name">All stops…</span></button>'
    );
    expect(html).not.toContain("aria-haspopup");
    const src = code("components/flights/StopShelf.tsx");
    expect(src).toContain("export function StopShelf({ onTack, onTackPlan, onSession, folded = false, onFold, onPlace, onFloat, onCatalog }: StopShelfProps)");
    expect(src).toContain('aria-haspopup={onCatalog ? undefined : "dialog"}');
  });

  it("round 14 — a click OR a double-click places once: every chip that places goes through useClickOrDouble", () => {
    const src = code("components/flights/StopShelf.tsx");
    expect(src).toContain('import { useClickOrDouble } from "../../lib/clickOrDouble";');
    // A kind's chip, a saved stop's, the Session chip: click and dblclick both wired, a drag drops a waiting click.
    expect(src.match(/onClick=\{cd\.onClick\} onDoubleClick=\{cd\.onDoubleClick\}/g)).toHaveLength(3);
    expect(src).toContain("const cd = useClickOrDouble(onClick && (() => onClick(plan)));");
    expect(src).toContain("const cd = useClickOrDouble(onSession);");
    expect(src.match(/onDragStart: \(e: DragEvent\) => \{\s*cd\.cancel\(\);/g)).toHaveLength(3);
    // No chip places straight from a bare onClick any more.
    expect(src).not.toMatch(/onClick=\{\(\) => onClick\(/);
    expect(src).not.toContain("onClick={onSession}");
  });

  it("round 13 — with onPlace a click PUTS IT ON THE BOARD, never onto the focused line", () => {
    const html = render(createElement(StopShelf, { onPlace() {}, onTack() {}, onTackPlan() {} }));
    expect(html).toMatch(/<button type="button" class="sky-stop-chip" data-kind="agent" draggable="true"/);
    expect(html).toContain('aria-label="Put “Ship” on the Sky"');
    expect(html).not.toContain("onto the focused route");
    expect(html).toContain('title="Ship does its thing\nClick to put it on the Sky, or drag it onto a route or the sky — right-click for more"');
    const src = code("components/flights/StopShelf.tsx");
    // The catalog's Add (and Enter) place too; onTack / onTackPlan stay for the host's drops.
    expect(src).toMatch(/const add = \(d: CatalogDrop\) => \{\s*setCatalog\(false\);\s*if \(onPlace\) return onPlace\(d\);/);
    expect(src).toContain("const clickPlan = onPlace ? (plan: FlightPlan) => onPlace({ planId: plan.id }) : onTackPlan;");
    expect(src).toContain("onAdd={onPlace || onTack || onTackPlan ? add : undefined}");
    expect(src).toContain("place={place}");
  });

  it("the fold handle's row: ⤢ Float the stops (quiet, a tab) when the host can float them", () => {
    const html = render(createElement(StopShelf, { onPlace() {}, onFloat() {} }));
    expect(html).toContain(
      '<span class="sky-shelf-tabs"><button type="button" class="sky-shelf-fold" aria-expanded="true" title="Fold the stops away">Stops ▾</button><button type="button" class="sky-shelf-fold sky-shelf-float" aria-label="Float the stops"'
    );
    expect(render(createElement(StopShelf, { folded: true, onFloat() {} }))).toContain("sky-shelf-float");
    expect(render(createElement(StopShelf, {}))).not.toContain("sky-shelf-float");
    expect(code("components/flights/StopShelf.tsx")).toContain("onClick={onFloat}");
    const css = cssOf("components/flights/StopShelf.css");
    expect(css).toMatch(/\.sky-shelf-tabs \{\s*position: absolute;\s*left: 12px;\s*top: -25px;/);
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
      ["text/plain", "Wait for me"],
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
