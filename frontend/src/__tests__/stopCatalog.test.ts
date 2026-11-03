/** The shelf's catalog (docs/flights.md "As built: round 12 shelf", "As
 * built: round 13 palette"): lib/stopCatalog — the default blocks
 * (DEFAULT_KINDS: their order, a kind the server doesn't list skipped), every
 * kind's order, the filter, the cards (catalogCards), the arrows over the
 * grids (gridMove), a saved stop's ⋯ (cardMenu), what a card's drag carries —
 * and StopCatalogBody (round 14: the floating palette's whole body, and the
 * All blocks… Modal's): Blocks and Multi-step as grids of cards, a filter,
 * cards draggable with their chip's MIME, a click or a double-click placing
 * once, an Add, the arrows / Enter, the ⋯'s Rename / Takes off… / Delete /
 * Reset / On the shelf; StopCatalog, the Modal around it, dialog-sized and
 * letting a drag through. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code, css as cssOf } from "./sourceText";
import type { FlightPlan, FlightStep, StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));

const { STOP_MIME } = await import("../lib/flights");
const { FLYWAY_MIME } = await import("../lib/skyLayout");
const { DEFAULT_KINDS, cardMenu, catalogCards, catalogKinds, catalogMatch, catalogRows, defaultKinds, filterKinds, filterPlans, gridMove, planPath, renamable, savedDragData, stopDragData } = await import("../lib/stopCatalog");
const { StopCatalog, StopCatalogBody } = await import("../components/flights/StopCatalog");

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
    expect([...DEFAULT_KINDS]).toEqual(["agent", "you", "ship", "verify", "wait", "loop"]);
    expect(defaultKinds(KINDS).map((k) => k.kind)).toEqual(["agent", "you", "ship", "verify", "wait"]);
    expect(defaultKinds([...KINDS].reverse()).map((k) => k.kind)).toEqual(["agent", "you", "ship", "verify", "wait"]);
  });

  it("round 16: Loop after Wait, once the server lists it", () => {
    const withLoop = [...KINDS, kind("loop", "Loop", "robot", "Repeat from an earlier stop")];
    expect(defaultKinds(withLoop).map((k) => k.kind)).toEqual(["agent", "you", "ship", "verify", "wait", "loop"]);
    expect(defaultKinds([withLoop[withLoop.length - 1], ...KINDS]).map((k) => k.kind)).toEqual(["agent", "you", "ship", "verify", "wait", "loop"]);
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

  it("kinds by label (Wait for me for you), kind and summary", () => {
    const kinds = catalogKinds(KINDS);
    expect(filterKinds(kinds, "for me").map((k) => k.kind)).toEqual(["you"]);
    expect(filterKinds(kinds, "notify").map((k) => k.kind)).toEqual(["notify"]);
    expect(filterKinds(kinds, "slack").map((k) => k.kind)).toEqual(["notify"]);
    expect(filterKinds(kinds, "prompt").map((k) => k.kind)).toEqual(["agent"]);
    expect(filterKinds(kinds, "")).toHaveLength(7);
  });

  it("saved stops by name, description and their steps' titles", () => {
    expect(filterPlans([TICKET, MINE], "mine").map((p) => p.id)).toEqual(["p_mine"]);
    expect(filterPlans([TICKET, MINE], "approve").map((p) => p.id)).toEqual(["builtin:ticket"]);
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
      ["text/plain", "Wait for me"],
    ]);
    expect(savedDragData(TICKET)).toEqual([
      [FLYWAY_MIME, "builtin:ticket"],
      ["text/plain", "Ticket to merge"],
    ]);
    const src = code("components/flights/StopCatalog.tsx");
    expect(src).toContain("onDragStart={(e) => onDragStart(e, c.key, plan ? savedDragData(plan) : stopDragData(def as StepKindDef))}");
    expect(src).toMatch(/for \(const \[type, value\] of data\) e\.dataTransfer\.setData\(type, value\);\s*e\.dataTransfer\.effectAllowed = "copy";/);
  });
});

describe("the cards (catalogCards)", () => {
  it("Blocks: every kind, the defaults first; Multi-step: the saved stops as given; `all` in the arrows' order", () => {
    const c = catalogCards(KINDS, [TICKET, MINE], "");
    expect(c.blocks.map((k) => k.key)).toEqual(["kind:agent", "kind:you", "kind:ship", "kind:verify", "kind:wait", "kind:split", "kind:notify"]);
    expect(c.multi.map((k) => k.key)).toEqual(["plan:builtin:ticket", "plan:p_mine"]);
    expect(c.all.map((k) => k.drop)).toEqual(catalogRows(catalogKinds(KINDS), [TICKET, MINE]));
    const perch = c.blocks[1];
    expect(perch).toMatchObject({ section: "blocks", name: "Wait for me", perch: true, drop: { kind: "you" } });
    expect(c.multi[0]).toMatchObject({ section: "multi", name: "Ticket to merge", builtin: true, path: "Work the ticket → Push → Approve", drop: { planId: "builtin:ticket" } });
    expect(c.multi[1]).toMatchObject({ builtin: false, path: "Fix" });
  });

  it("the filter narrows both sections", () => {
    const c = catalogCards(KINDS, [TICKET, MINE], "push");
    expect(c.blocks.map((k) => k.drop)).toEqual([{ kind: "ship" }]);
    expect(c.multi.map((k) => k.drop)).toEqual([{ planId: "builtin:ticket" }]);
    expect(catalogCards(KINDS, [TICKET, MINE], "zzz").all).toEqual([]);
  });
});

describe("the arrows over the grids (gridMove)", () => {
  // Blocks: 7 cards, 3 a row → [0 1 2] [3 4 5] [6]; Multi-step: 2 → [7 8].
  const sizes = [7, 2];
  it("← / → step through every card, stopping at the ends", () => {
    expect(gridMove(0, "ArrowRight", sizes, 3)).toBe(1);
    expect(gridMove(6, "ArrowRight", sizes, 3)).toBe(7);
    expect(gridMove(8, "ArrowRight", sizes, 3)).toBe(8);
    expect(gridMove(0, "ArrowLeft", sizes, 3)).toBe(0);
    expect(gridMove(7, "ArrowLeft", sizes, 3)).toBe(6);
  });
  it("↓ / ↑ a row, into the next section's first row and back, keeping the column", () => {
    expect(gridMove(1, "ArrowDown", sizes, 3)).toBe(4);
    expect(gridMove(5, "ArrowDown", sizes, 3)).toBe(6); // the short last row: its last card
    expect(gridMove(6, "ArrowDown", sizes, 3)).toBe(7);
    expect(gridMove(2, "ArrowDown", [3, 2], 3)).toBe(4); // column 2 → the next section's last
    expect(gridMove(8, "ArrowDown", sizes, 3)).toBe(8);
    expect(gridMove(4, "ArrowUp", sizes, 3)).toBe(1);
    expect(gridMove(8, "ArrowUp", sizes, 3)).toBe(6); // column 1, but the last row has one
    expect(gridMove(7, "ArrowUp", sizes, 3)).toBe(6);
    expect(gridMove(1, "ArrowUp", sizes, 3)).toBe(1);
  });
  it("an empty section is skipped; one column is a list; nothing is -1", () => {
    expect(gridMove(1, "ArrowDown", [2, 0, 3], 2)).toBe(3);
    expect(gridMove(2, "ArrowUp", [2, 0, 3], 2)).toBe(0);
    expect(gridMove(0, "ArrowDown", [2, 2], 1)).toBe(1);
    expect(gridMove(0, "ArrowDown", [0, 0], 3)).toBe(-1);
    expect(gridMove(-1, "ArrowDown", [2], 3)).toBe(0);
    expect(gridMove(1, "Home", [2], 3)).toBe(1);
  });
});

describe("a saved-stop card's ⋯ (cardMenu)", () => {
  it("Rename, What starts it…, On the shelf (★ / ☆), then Reset to default (built-in) or Delete (yours)", () => {
    expect(cardMenu(TICKET, false).map((i) => i.label)).toEqual(["Rename", "What starts it…", "☆ On the shelf", "Reset to default"]);
    expect(cardMenu(MINE, true).map((i) => i.label)).toEqual(["Rename", "What starts it…", "★ On the shelf", "Delete"]);
    expect(cardMenu(MINE, true)[3].danger).toBe(true);
  });
  it("a built-in may be renamed (the server keeps it a built-in); a depth cut is virtual, so it can't", () => {
    expect(renamable(TICKET)).toBe(true);
    expect(renamable({ id: "builtin:ticket@push" })).toBe(false);
    expect(cardMenu({ id: "builtin:ticket@push", builtin: true }, false).map((i) => i.key)).toEqual(["takeoff", "shelf", "reset"]);
  });
});

describe("StopCatalog", () => {
  const html = render({ onAdd() {} });

  it("a Modal dialog titled All stops, its filter focused, a Close", () => {
    expect(html).toMatch(/^<div id="sky-catalog" role="dialog" aria-modal="true" aria-labelledby="sky-catalog-title" tabindex="-1" class="modal sky-catalog-modal">/);
    expect(html).toContain('<h2 id="sky-catalog-title">All stops</h2>');
    expect(html).toContain('<button type="button" class="fl-icon-btn sky-catalog-close" aria-label="Close" title="Close (Esc)">×</button>');
    expect(html).toMatch(/<input class="fl-input sky-catalog-filter" type="text" placeholder="Filter stops…" aria-label="Filter stops" aria-controls="sky-catalog-list" aria-activedescendant="sky-catalog-row-0"/);
    // The focus, without scrolling anything (the palette floats in the sky).
    const src = code("components/flights/StopCatalog.tsx");
    expect(src).toContain("if (autoFocus) filterRef.current?.focus({ preventScroll: true });");
    expect(src).toMatch(/<StopCatalogBody\s+\{\.\.\.body\}\s+autoFocus/);
  });

  it("round 14: dialog-sized, not the window — the board shows around it; the cards in a grid as wide as it holds", () => {
    const css = cssOf("components/flights/StopCatalog.css");
    expect(css).toMatch(/\.sky-catalog \{[^}]*width: min\(920px, 70vw\);[^}]*height: min\(680px, 72vh\);/);
    expect(css).not.toContain("92vw");
    expect(css).toMatch(/\.sky-catalog-grid \{\s*display: grid;\s*grid-template-columns: repeat\(auto-fill, minmax\(230px, 1fr\)\);/);
    expect(html.match(/<div class="sky-catalog-grid">/g)).toHaveLength(2);
    // The list scrolls itself, not the sky under it.
    expect(html).toContain('class="sky-catalog-list sky-own-scroll" id="sky-catalog-list" role="listbox"');
  });

  it("Stops: a card per kind — icon, name, robot / you badge, its summary — the defaults first", () => {
    const blocks = html.slice(html.indexOf('data-section="blocks"'), html.indexOf('data-section="multi"'));
    expect(blocks).toContain('<h3 class="sky-catalog-label">Stops</h3>');
    expect([...blocks.matchAll(/data-kind="([^"]+)"/g)].map((m) => m[1])).toEqual(["agent", "you", "ship", "verify", "wait", "split", "notify"]);
    expect(blocks).toContain(
      '<span class="sky-catalog-name"><span class="sky-catalog-title">Agent</span><span class="sky-catalog-badge actor-robot">robot</span></span></div><span class="sky-catalog-summary">Give the session&#x27;s agent a prompt</span>'
    );
    const perch = blocks.slice(blocks.indexOf('data-kind="you"'));
    expect(blocks).toMatch(/class="sky-catalog-card perch" data-kind="you" draggable="true"/);
    expect(perch).toContain('<span class="sky-catalog-name"><span class="sky-catalog-title">Wait for me</span><span class="sky-catalog-badge actor-you">you</span></span>');
    expect(perch).toContain("fl-stop-icon-perch");
    // A kind card has no ⋯ (only the ★, when the host passes the pick).
    expect(blocks).not.toContain("vf-menu");
  });

  it("Saved chains: a card per saved chain — the name, built-in, its step boxes, its path, a ⋯", () => {
    const multi = html.slice(html.indexOf('data-section="multi"'));
    expect(multi).toContain('<h3 class="sky-catalog-label">Saved chains</h3>');
    expect([...multi.matchAll(/data-plan="([^"]+)"/g)].map((m) => m[1])).toEqual(["builtin:ticket", "p_mine"]);
    expect(multi).toContain('<span class="sky-catalog-boxes" aria-hidden="true"><i class=""></i><i class=""></i><i class="perch"></i></span><span class="sky-catalog-summary">Work the ticket → Push → Approve</span>');
    expect(multi).toContain('<span class="sky-catalog-name"><span class="sky-catalog-title">Ticket to merge</span><span class="sky-catalog-badge builtin">built-in</span></span>');
    expect(multi).toContain('<span class="sky-catalog-name"><span class="sky-catalog-title">Mine</span></span>');
    // The ⋯: the house OverflowMenu (built-in: Reset; yours: Delete).
    expect(multi).toContain('<summary aria-label="“Ticket to merge” — rename, delete, shelf"');
    const ticket = multi.slice(0, multi.indexOf('data-plan="p_mine"'));
    expect(ticket).toContain('<button type="button" class="vf-menu-item">Rename</button><button type="button" class="vf-menu-item">What starts it…</button><button type="button" class="vf-menu-item">Reset to default</button>');
    expect(multi.slice(multi.indexOf('data-plan="p_mine"'))).toContain('<button type="button" class="vf-menu-item vf-menu-danger">Delete</button>');
  });

  it("with the shelf's pick: a ★ / ☆ on every card, and On the shelf in a saved stop's ⋯", () => {
    const on = render({ onAdd() {}, shelf: (d: { kind?: string; planId?: string }) => d.kind === "agent" || d.planId === "p_mine", onShelfToggle() {} });
    expect(on.match(/sky-catalog-shelf/g)).toHaveLength(9);
    expect(on).toContain('<button type="button" class="fl-icon-btn sky-catalog-shelf on" tabindex="-1" aria-pressed="true" aria-label="Take “Agent” off the shelf"');
    expect(on).toContain('aria-label="Put “Ticket to merge” on the shelf"');
    expect(on).toContain('<button type="button" class="vf-menu-item">☆ On the shelf</button>');
    expect(on).toContain('<button type="button" class="vf-menu-item">★ On the shelf</button>');
  });

  it("each card draggable, the first picked, with an Add (the secondary tier)", () => {
    expect(html.match(/role="option"/g)).toHaveLength(9);
    expect(html.match(/draggable="true"/g)).toHaveLength(9);
    expect(html).toContain('<div id="sky-catalog-row-0" role="option" aria-selected="true" class="sky-catalog-card active" data-kind="agent" draggable="true"');
    expect(html).toContain('id="sky-catalog-row-7" role="option" aria-selected="false" class="sky-catalog-card sky-catalog-plan" data-plan="builtin:ticket"');
    expect(html).toContain('<button type="button" class="test-btn sky-catalog-add" tabindex="-1" aria-label="Add “Wait for me”">Add</button>');
    expect(html).toContain('aria-label="Add “Ticket to merge”"');
    expect(html.match(/sky-catalog-add/g)).toHaveLength(9);
  });

  it("place: the words say a click puts it on the board", () => {
    const placing = render({ onAdd() {}, place: true });
    expect(placing).toContain('title="Drag onto a chain or the sky, or click to put it on the board"');
    expect(placing).toContain("a click or a double-click puts it on the board · the arrows pick, Enter puts it on the board");
    expect(placing).not.toContain("focused chain");
    expect(html).toContain("a click or a double-click adds it to the focused chain");
  });

  it("without onAdd the cards only drag", () => {
    const drag = render();
    expect(drag).not.toContain("sky-catalog-add");
    expect(drag).toContain('title="Drag onto a chain or the sky"');
    expect(drag).toContain('<p class="sky-catalog-hint muted">Drag a stop onto a chain or the sky</p>');
  });

  it("nothing at all: says so", () => {
    const none = render({ kinds: [], plans: [] });
    expect(none).toContain('<p class="sky-catalog-empty muted">No stops yet — this server may be too old for the Sky.</p>');
    expect(none).not.toContain("data-section");
  });

  it("the arrows move the pick from the filter (← / → only from its edge), Enter / Add / a double-click call onAdd({kind} | {planId})", () => {
    const src = code("components/flights/StopCatalog.tsx");
    expect(src).toContain("gridMove(at + leads, e.key, [...groups.map((g) => g.items.length + leadIn(g.family)), cards.multi.length], gridCols(listRef.current))");
    expect(src).toMatch(/e\.key === "ArrowLeft" \? el\.selectionStart === 0 && el\.selectionEnd === 0 : e\.key === "ArrowRight" \? el\.selectionStart === el\.value\.length : true/);
    expect(src).toMatch(/\} else if \(e\.key === "Enter"\) \{\s*e\.preventDefault\(\);\s*if \(at >= 0\) add\(rows\[at\]\.drop\);/);
    expect(src).toContain("onClick={() => add(c.drop)}");
    // A card's click or double-click places ONCE (clickOrDouble, one per card
    // kept across renders); a press on its ★ / ⋯ / field / Add is theirs.
    expect(src).toContain("if (!c) clicks.current.set(key, (c = clickOrDouble(() => addRef.current?.(drop))));");
    expect(src).toContain("onClick={(e) => mine(e) && cd.onClick(e)}");
    expect(src).toContain("onDoubleClick={(e) => mine(e) && cd.onDoubleClick()}");
    expect(src).toContain("const mine = (e: MouseEvent) => !!onAdd && !naming && !asking && !inner(e.target);");
    expect(src).toContain('closest?.("button, input, summary, .vf-menu, .set-confirm")');
    // A drag drops a waiting click.
    expect(src).toMatch(/const onDragStart = \(e: DragEvent, key: string, data: Array<\[string, string\]>\) => \{\s*clicks\.current\.get\(key\)\?\.cancel\(\);/);
    // A key typed in the catalog stays there (Escape still reaches the Modal).
    expect(src).toContain('onKeyDown={(e) => e.key === "Escape" || e.stopPropagation()}');
  });

  it("the ⋯ acts through the plan routes: Rename on the card (PlanNameField → savePlan), Reset, Delete asked on the card", () => {
    const src = code("components/flights/StopCatalog.tsx");
    expect(src).toContain("<PlanNameField plan={plan} plans={plans} onDone={() => setRenaming(\"\")} />");
    expect(src).toContain("await savePlan({ ...plan, name: c.name });");
    expect(src).toContain("await resetPlan(plan.id);");
    expect(src).toContain("await deletePlan(plan.id);");
    expect(src).toMatch(/<InlineConfirm\s+title=\{"Delete “" \+ plan\.name \+ "”\?"\}/);
    expect(src).toContain('else if (it.key === "takeoff") setTakeoff(plan);');
    expect(src).toContain("{takeoff && <TakeoffDialog plan={takeoff} onClose={() => setTakeoff(null)} />}");
    // Esc answers an open delete question before it closes the catalog.
    expect(src).toMatch(/escRef\.current = \(\) => \{[\s\S]*?if \(!deleting\) return false;\s*setDeleting\(""\);\s*return true;/);
    expect(src).toContain("onEscape={() => esc.current?.() ?? false}");
    // Renaming or asking: the card stops dragging.
    expect(src).toContain("draggable={!naming && !asking}");
  });

  it("a drag goes through: the Modal turns see-through a tick after dragstart; a drop that landed closes it", () => {
    const src = code("components/flights/StopCatalog.tsx");
    expect(src).toContain("if (onDrag) setTimeout(() => onDrag(true, false), 0);");
    expect(src).toContain('const onDragEnd = (e: DragEvent) => onDrag?.(false, !!e.dataTransfer && e.dataTransfer.dropEffect !== "none");');
    expect(src).toMatch(/className=\{"sky-catalog-modal" \+ \(dragging \? " is-dragging" : ""\)\}/);
    expect(src).toMatch(/onDrag=\{\(on, landed\) => \{\s*setDragging\(on\);\s*if \(!on && landed\) onClose\(\);/);
    const css = cssOf("components/flights/StopCatalog.css");
    expect(css).toMatch(/\.modal\.sky-catalog-modal\.is-dragging \{\s*background: transparent;\s*pointer-events: none;\s*\}/);
    expect(css).toMatch(/\.sky-catalog-modal\.is-dragging \.sky-catalog \{\s*opacity: 0\.35;/);
  });

  it("no native dialogs", () => {
    expect(code("components/flights/StopCatalog.tsx")).not.toMatch(/\bwindow\.(prompt|alert|confirm)\(/);
  });
});

describe("StopCatalogBody (round 14: the palette's body)", () => {
  const body = (props: Record<string, unknown> = {}) =>
    renderToStaticMarkup(
      createElement(StopCatalogBody, {
        kinds: KINDS,
        plans: [TICKET, MINE],
        head: (filter: unknown) => createElement("header", { className: "h" }, filter as never),
        ...props,
      } as never)
    );

  it("hands the filter to the surface's head, then the list; no Modal of its own", () => {
    const html = body({ onAdd() {}, place: true });
    expect(html).toMatch(/^<header class="h"><input class="fl-input sky-catalog-filter"/);
    expect(html).not.toContain("modal");
    expect(html).toContain('</header><div class="sky-catalog-list sky-own-scroll" id="sky-catalog-list" role="listbox" aria-label="All stops">');
  });

  it("compact: its own ids and list class; icon + name + one line; no badges and no Add (a click places)", () => {
    const html = body({ onAdd() {}, place: true, compact: true, idPrefix: "sky-palette", listClass: "sky-palette-body" });
    expect(html).toContain('<div class="sky-catalog-list sky-own-scroll compact sky-palette-body" id="sky-palette-list" role="listbox"');
    expect(html).toContain('aria-controls="sky-palette-list" aria-activedescendant="sky-palette-row-0"');
    expect(html).toContain('id="sky-palette-row-0"');
    expect(html).not.toContain("sky-catalog-badge");
    expect(html).not.toContain("sky-catalog-add");
    // A kind: its summary; a saved stop: its boxes (its path in the title).
    expect(html).toContain('<span class="sky-catalog-summary">Commit, push, open the PR</span>');
    expect(html).toMatch(/data-plan="builtin:ticket" draggable="true" title="Work the ticket → Push → Approve\nDrag onto a chain or the sky, or click to put it on the board"/);
    expect(html).not.toContain('<span class="sky-catalog-summary">Work the ticket → Push → Approve</span>');
    expect(html).toContain('<span class="sky-catalog-boxes" aria-hidden="true"><i class=""></i><i class=""></i><i class="perch"></i></span>');
    // Every kind and every saved stop.
    expect(html.match(/role="option"/g)).toHaveLength(9);
  });

  it("a lead card goes first in Blocks, for the query; it is no row (the arrows skip it)", () => {
    const html = body({ onAdd() {}, lead: (q: string) => (q ? null : createElement("div", { className: "lead" }, "Session")) });
    const blocks = html.slice(html.indexOf('data-section="blocks"'));
    expect(blocks).toMatch(/<div class="sky-catalog-grid"><div class="lead">Session<\/div><div id="sky-catalog-row-0"/);
    const src = code("components/flights/StopCatalog.tsx");
    expect(src).toContain("setActive(Math.max(0, gridMove(at + leads, e.key, [...groups.map((g) => g.items.length + leadIn(g.family)), cards.multi.length], gridCols(listRef.current)) - leads));");
    // Only the lead: the Blocks section still shows (it holds the lead).
    const lone = body({ kinds: [], plans: [], lead: () => createElement("div", { className: "lead" }, "Session") });
    expect(lone).toContain('data-section="blocks"');
    expect(lone).not.toContain("sky-catalog-empty");
  });

  it("the ★ sets the BOTTOM shelf's pick (the palette shows everything regardless)", () => {
    const html = body({ onAdd() {}, compact: true, shelf: (d: { kind?: string }) => d.kind === "ship", onShelfToggle() {} });
    expect(html).toContain('<button type="button" class="fl-icon-btn sky-catalog-shelf on" tabindex="-1" aria-pressed="true" aria-label="Take “Ship” off the shelf" title="On the bottom shelf — click to take it off">★</button>');
    expect(html).toContain('title="Put it on the bottom shelf">☆</button>');
    expect(html.match(/role="option"/g)).toHaveLength(9);
  });

  it("scrolls only its own list to the card the arrows picked (never the sky around it)", () => {
    const src = code("components/flights/StopCatalog.tsx");
    expect(src).not.toContain("scrollIntoView");
    expect(src).toContain("reveal(listRef.current, document.getElementById(rowId(at)));");
    expect(src).toMatch(/if \(r\.top < l\.top\) list\.scrollTop -= \(l\.top - r\.top\) \/ scale;/);
  });

  it("the compact cards: a denser grid, one line, a saved stop across two columns, the ★ shown on the hovered / picked card", () => {
    const css = cssOf("components/flights/StopCatalog.css");
    expect(css).toMatch(/\.sky-catalog-list\.compact \.sky-catalog-grid \{\s*grid-template-columns: repeat\(auto-fill, minmax\(132px, 1fr\)\);/);
    expect(css).toMatch(/\.sky-catalog-list\.compact \.sky-catalog-plan \{\s*grid-column: span 2;/);
    expect(css).toMatch(/\.sky-catalog-list\.compact \.sky-catalog-summary \{[^}]*-webkit-line-clamp: 1;/);
    expect(css).toMatch(/\.sky-catalog-list\.compact \.sky-catalog-shelf:not\(\.on\) \{\s*opacity: 0;/);
  });
});

describe("what sits on the shelf (the pick)", () => {
  it("defaults to the five blocks and no multi-step block; a stored pick wins; junk falls back", async () => {
    const { readShelf, defaultShelf, DEFAULT_KINDS } = await import("../lib/stopCatalog");
    expect(readShelf(null)).toEqual({ kinds: [...DEFAULT_KINDS], plans: [] });
    expect(readShelf("not json")).toEqual(defaultShelf());
    expect(readShelf(JSON.stringify({ kinds: ["ship"], plans: ["p_1", 3] }))).toEqual({ kinds: ["ship"], plans: ["p_1"] });
    expect(readShelf(JSON.stringify({ kinds: "ship" }))).toEqual(defaultShelf());
  });
  it("toggles a kind or a saved stop on and off, and filters the shelf's chips", async () => {
    const { toggleShelf, onShelf, shelfKinds, shelfPlans, defaultShelf } = await import("../lib/stopCatalog");
    let pick = defaultShelf();
    expect(onShelf(pick, { kind: "wait" })).toBe(true);
    pick = toggleShelf(pick, { kind: "wait" });
    expect(onShelf(pick, { kind: "wait" })).toBe(false);
    pick = toggleShelf(pick, { planId: "builtin:ticket" });
    expect(onShelf(pick, { planId: "builtin:ticket" })).toBe(true);
    pick = toggleShelf(pick, { kind: "split" });
    expect(shelfKinds(pick, [{ kind: "agent" }, { kind: "split" }, { kind: "wait" }]).map((k) => k.kind)).toEqual(["agent", "split"]);
    expect(shelfPlans(pick, [{ id: "builtin:ticket" }, { id: "p_x" }]).map((p) => p.id)).toEqual(["builtin:ticket"]);
  });
});
