/** Selecting on the Sky (docs/flights.md "As built: round 11 selection"): the
 * marquee's pure geometry (lib/skySelect), the one-line-stops rule, the
 * consecutive check Group needs, how a group box stands for its sub-steps,
 * and the wiring SkyView / SkyLine / GroundedLine / FlywayCanvas carry. */

import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { code, css } from "./sourceText";
import {
  NO_SELECTION,
  WHOLE_LINE,
  addSelection,
  consecutive,
  isStopNode,
  lineTouched,
  overlapArea,
  planeToScreen,
  pruneSelection,
  rectFrom,
  removals,
  screenToPlane,
  select,
  selectLines,
  selectedIndexes,
  selectionEmpty,
  stepIndexesOf,
  stopSelection,
  toggleLine,
  type SelectLine,
  type SelectStop,
} from "../lib/skySelect";
import { NEW_STOPS, SkySelectionBar, groupHint } from "../components/flights/SkySelection";
import type { Flight, FlightStep } from "../lib/flights";

/** Two lines side by side, a 34px bar each, two stops on each. */
const LINES: SelectLine[] = [
  { id: "a", rect: { x: 0, y: 0, w: 600, h: 200 }, heads: [{ x: 0, y: 0, w: 600, h: 34 }] },
  { id: "b", rect: { x: 0, y: 300, w: 600, h: 200 }, heads: [{ x: 0, y: 300, w: 600, h: 34 }] },
];
const STOPS: SelectStop[] = [
  { line: "a", node: "stop:s1", rect: { x: 20, y: 60, w: 150, h: 80 } },
  { line: "a", node: "group:g1", rect: { x: 200, y: 60, w: 150, h: 80 } },
  { line: "a", node: "stop:s5", rect: { x: 380, y: 60, w: 150, h: 80 } },
  { line: "b", node: "stop:s1", rect: { x: 20, y: 360, w: 150, h: 80 } },
  { line: "b", node: "stop:s2", rect: { x: 200, y: 360, w: 150, h: 80 } },
];
const items = { lines: LINES, stops: STOPS };

const step = (id: string, group?: string): FlightStep => ({ id, kind: "agent", title: id, config: {}, on_fail: "halt", ...(group ? { group: { id: group, name: "G " + group, plan_id: "" } } : {}) });
// s1, [s2 s3 s4 in g1], s5, [s6 s7 in g2]
const STEPS = [step("s1"), step("s2", "g1"), step("s3", "g1"), step("s4", "g1"), step("s5"), step("s6", "g2"), step("s7", "g2")];

describe("the geometry", () => {
  it("a rect from two corners, any way round; overlap is area, edges don't count", () => {
    expect(rectFrom({ x: 10, y: 50 }, { x: 0, y: 20 })).toEqual({ x: 0, y: 20, w: 10, h: 30 });
    expect(overlapArea({ x: 0, y: 0, w: 10, h: 10 }, { x: 5, y: 5, w: 10, h: 10 })).toBe(25);
    expect(overlapArea({ x: 0, y: 0, w: 10, h: 10 }, { x: 10, y: 0, w: 10, h: 10 })).toBe(0);
  });

  it("screen ⇄ plane takes the camera's pan and zoom off and puts them back", () => {
    const cam = { x: 100, y: -40, z: 2 };
    const r = { x: 300, y: 60, w: 80, h: 40 };
    expect(screenToPlane(cam, r)).toEqual({ x: 100, y: 50, w: 40, h: 20 });
    expect(planeToScreen(cam, screenToPlane(cam, r))).toEqual(r);
  });

  it("a line is taken whole when a head is crossed or half of it is covered", () => {
    const a = LINES[0];
    // A sliver across the bar.
    expect(lineTouched({ x: 590, y: 10, w: 40, h: 4 }, a)).toBe(true);
    // Inside the canvas, under the bar, small: no.
    expect(lineTouched({ x: 10, y: 50, w: 200, h: 100 }, a)).toBe(false);
    // Half the line's area, without the bar.
    expect(WHOLE_LINE).toBe(0.5);
    expect(lineTouched({ x: 0, y: 40, w: 600, h: 100 }, { ...a, heads: [] })).toBe(true);
    expect(lineTouched({ x: 0, y: 40, w: 600, h: 99 }, { ...a, heads: [] })).toBe(false);
  });
});

describe("select: the one-line-stops rule", () => {
  it("stops of exactly one line and no whole line → those stops", () => {
    const s = select({ x: 10, y: 50, w: 250, h: 50 }, items);
    expect([...s.lines]).toEqual([]);
    expect([...s.stops.keys()]).toEqual(["a"]);
    expect([...s.stops.get("a")!]).toEqual(["stop:s1", "group:g1"]);
    expect(stopSelection(s)).toEqual({ line: "a", nodes: ["stop:s1", "group:g1"] });
  });

  it("stops of two lines → both lines, whole, stops ignored", () => {
    const s = select({ x: 10, y: 120, w: 100, h: 280 }, items);
    expect([...s.lines].sort()).toEqual(["a", "b"]);
    expect(s.stops.size).toBe(0);
  });

  it("a bar crossed with stops of another line → lines (the whole one and the stops' one)", () => {
    // Crosses b's bar and a's stops.
    const s = select({ x: 10, y: 100, w: 100, h: 220 }, items);
    expect([...s.lines].sort()).toEqual(["a", "b"]);
  });

  it("a bar crossed on the stops' own line → that line, whole", () => {
    const s = select({ x: 10, y: 10, w: 100, h: 80 }, items);
    expect([...s.lines]).toEqual(["a"]);
    expect(s.stops.size).toBe(0);
  });

  it("empty sky → nothing; nodes that aren't stops never count", () => {
    expect(selectionEmpty(select({ x: 700, y: 0, w: 50, h: 50 }, items))).toBe(true);
    expect(isStopNode("session:x")).toBe(false);
    expect(isStopNode("add")).toBe(false);
    expect(isStopNode("flown")).toBe(false);
    const s = select({ x: 0, y: 50, w: 100, h: 100 }, { lines: LINES, stops: [{ line: "a", node: "session:a", rect: { x: 0, y: 50, w: 90, h: 90 } }] });
    expect(selectionEmpty(s)).toBe(true);
  });
});

describe("adding, toggling, pruning", () => {
  const stopsA = select({ x: 10, y: 50, w: 150, h: 50 }, items);
  const stopsA2 = select({ x: 370, y: 50, w: 100, h: 50 }, items);
  const stopsB = select({ x: 10, y: 350, w: 50, h: 50 }, items);

  it("Shift+drag: more stops of the same line stay stops; another line's turn it into lines", () => {
    const same = addSelection(stopsA, stopsA2);
    expect([...same.stops.get("a")!].sort()).toEqual(["stop:s1", "stop:s5"]);
    const two = addSelection(stopsA, stopsB);
    expect([...two.lines].sort()).toEqual(["a", "b"]);
    expect(two.stops.size).toBe(0);
    expect([...addSelection(NO_SELECTION, stopsB).stops.keys()]).toEqual(["b"]);
  });

  it("Ctrl/⌘+A takes the lines; Shift+click takes one in or out", () => {
    const all = selectLines(["a", "b"]);
    expect([...toggleLine(all, "a").lines]).toEqual(["b"]);
    expect([...toggleLine(stopsA, "b").lines].sort()).toEqual(["a", "b"]);
  });

  it("a line no longer shown drops out (and an untouched selection is the same object)", () => {
    const all = selectLines(["a", "b"]);
    expect(pruneSelection(all, new Set(["a", "b"]))).toBe(all);
    expect([...pruneSelection(all, new Set(["a"])).lines]).toEqual(["a"]);
    expect(pruneSelection(stopsA, new Set(["b"])).stops.size).toBe(0);
  });
});

describe("stops → the flight's steps", () => {
  it("a stop is its step; a group box is its run of sub-steps (a later run by @index)", () => {
    expect(stepIndexesOf("stop:s5", STEPS)).toEqual([4]);
    expect(stepIndexesOf("group:g1", STEPS)).toEqual([1, 2, 3]);
    expect(stepIndexesOf("group:g2@5", STEPS)).toEqual([5, 6]);
    expect(stepIndexesOf("group:g2@4", STEPS)).toEqual([]);
    expect(stepIndexesOf("stop:nope", STEPS)).toEqual([]);
    expect(stepIndexesOf("session:x", STEPS)).toEqual([]);
  });

  it("the selection's indexes are sorted, once each; Group needs them side by side", () => {
    expect(selectedIndexes(["stop:s5", "group:g1", "stop:s1"], STEPS)).toEqual([0, 1, 2, 3, 4]);
    expect(consecutive([4, 3, 2])).toBe(true);
    expect(consecutive([0, 2])).toBe(false);
    expect(consecutive([])).toBe(false);
    expect(consecutive(selectedIndexes(["stop:s1", "stop:s5"], STEPS))).toBe(false);
  });

  it("removal: a group box by its first step with ?group, a stop by itself", () => {
    expect(removals(["group:g1", "stop:s5", "add"], STEPS)).toEqual([
      { step: "s2", group: true },
      { step: "s5", group: false },
    ]);
  });
});

describe("Group's hint", () => {
  const grounded = { id: "f1", state: "grounded", cursor: 0, steps: STEPS, step_states: {} } as unknown as Flight;

  it("is empty for stops side by side that can change", () => {
    expect(groupHint(grounded, [0, 1, 2, 3])).toBe("");
  });

  it("says why otherwise", () => {
    expect(groupHint(grounded, [0, 4])).toBe("Pick stops that sit next to each other");
    expect(groupHint(grounded, [1, 2, 3])).toBe("These already are one stop");
    expect(groupHint(null, [0])).toMatch(/aren't on a flight/);
    const flying = { ...grounded, state: "running", cursor: 2 } as unknown as Flight;
    expect(groupHint(flying, [1, 2])).toMatch(/has flown, or is flying/);
  });
});

describe("the bar", () => {
  const base = { x: 10, y: 20, ask: "" as const, setAsk() {}, nameOf: (id: string) => "line " + id, flightOf: () => null, kinds: [], onTidy() {}, onClear() {} };
  const render = (props: Partial<Parameters<typeof SkySelectionBar>[0]>) => renderToStaticMarkup(createElement(SkySelectionBar, { ...base, selection: NO_SELECTION, ...props }));

  it("is nothing with nothing selected", () => {
    expect(render({})).toBe("");
  });

  it("lines: the count, Move's hint, Tidy these, Remove…, Clear", () => {
    const html = render({ selection: selectLines(["a", "b"]) });
    expect(html).toContain('aria-label="2 lines selected"');
    expect(html).toContain("Drag a bar to move them");
    expect(html).toContain(">Tidy these</button>");
    expect(html).toContain(">Remove…</button>");
    expect(html).toContain(">Clear</button>");
  });

  it("lines' Remove… is an inline confirm that says what happens to each kind of line", () => {
    const html = render({ selection: selectLines(["me", "flight:f1"]), ask: "remove" });
    expect(html).toContain('role="alertdialog"');
    expect(html).toContain("1 session closes (line me): its agent stops and it moves to Recently closed.");
    expect(html).toContain("1 line of stops with no session leaves the board (line flight:f1)");
  });

  it("stops: Group into a stop, Save to the shelf, Remove; Group off with a hint when they aren't side by side", () => {
    const flight = { id: "f1", state: "grounded", cursor: 0, steps: STEPS, step_states: {} } as unknown as Flight;
    const sel = { lines: new Set<string>(), stops: new Map([["flight:f1", new Set(["stop:s1", "stop:s5"])]]) };
    const html = render({ selection: sel, flightOf: () => flight });
    expect(html).toContain('aria-label="2 stops selected"');
    expect(html).toMatch(/disabled=""[^>]*title="Pick stops that sit next to each other"[^>]*>Group into a stop<\/button>/);
    expect(html).toContain(">Save to the shelf</button>");
    expect(html).toContain(">Remove</button>");
    const asking = render({ selection: sel, flightOf: () => flight, ask: "save" });
    expect(asking).toContain('placeholder="' + NEW_STOPS + '"');
    expect(asking).toContain(">Save</button>");
  });

  it("never reaches for window.confirm / prompt / alert", () => {
    const src = code("components/flights/SkySelection.tsx");
    expect(src).not.toMatch(/window\.(confirm|prompt|alert)|\bconfirm\(|\bprompt\(|\balert\(/);
    expect(src).toContain("<InlineConfirm");
  });
});

describe("the wiring", () => {
  const sky = code("components/flights/SkyView.tsx");

  it("a left drag on empty sky is the marquee; touch and the middle button pan; Space+drag still pans anywhere", () => {
    expect(sky).toContain('const g = e.button === 0 ? gestureFor(liveTool, "ground", { shift: e.shiftKey, touch: e.pointerType === "touch" }) : "pan";');
    expect(sky).toContain('if (g === "marquee") startMarquee(e);\n    else startPan(e);');
    expect(sky).toContain("onPointerDownCapture={onDownCapture}");
    expect(sky).toContain("const got = select(area, items);");
    expect(sky).toContain("const next = m.add ? addSelection(sel, got) : got;");
    expect(sky).toContain('className="sky-marquee"');
  });

  it("measures the stop boxes at release, on the plane, never inside an open card", () => {
    expect(sky).toContain("data-slot={l.id}");
    expect(sky).toContain("screenToPlane(c, { x: b.left - o.x, y: b.top - o.y, w: b.width, h: b.height })");
    expect(sky).toContain('if (isStopNode(node) && !el.closest(".skl-card"))');
  });

  it("the shelf's drag and drop is HTML5 DnD, untouched by the marquee's pointer handling", () => {
    expect(sky).toContain("onDrop={onNewDrop}");
    expect(sky).toContain('<main id="sky-view" className="sky" data-tool={liveTool} onDragOver={onSkyDragOver} onDrop={onSkyDrop}>');
  });

  it("dragging a selected line moves every selected line, each pinned", () => {
    expect(sky).toContain("const ids = sel.lines.has(id) ? [...sel.lines] : [id];");
    expect(sky).toContain("onLayout(id, { x: p.x, y: p.y, pinned: true });");
  });

  it("keys: Esc clears, Ctrl/⌘+A takes every line, Delete / Backspace asks the bar to remove", () => {
    expect(sky).toContain("setSel(selectLines(lines.map((l) => l.id)));");
    expect(sky).toContain('setSelAsk("remove");');
    // Delete is taken before the app's keymap (document, capture), where it
    // closes the focused SESSION — never with a selection on the sky.
    expect(sky).toContain('window.addEventListener("keydown", del, true);');
    expect(sky).toContain('e.stopPropagation();\n    if (d.kind === "selection") setSelAsk("remove");');
  });

  it("lines and stops show it: is-selected on the line, fl-box-selected on a box", () => {
    expect(sky).toContain("selected={at ? sel.lines.has(id) : undefined}");
    expect(sky).toContain("highlighted={at ? sel.stops.get(id) : undefined}");
    for (const f of ["components/flights/SkyLine.tsx", "components/flights/GroundedLine.tsx"]) {
      const src = code(f);
      expect(src).toContain('(p.selected ? " is-selected" : "")');
      expect(src).toContain("highlighted={p.highlighted}");
    }
    expect(code("components/flights/FlywayCanvas.tsx")).toContain('(p.highlighted?.has(n.id) ? " fl-box-selected" : "")');
    const sheet = css("components/flights/SkySelection.css");
    expect(sheet).toMatch(/\.skl\.is-selected \{[^}]*outline: 2px dashed var\(--accent\)/);
    expect(sheet).toMatch(/\.fl-box\.fl-box-selected \{[^}]*outline: 2px dashed var\(--accent\)/);
    expect(sheet).toMatch(/\.sky-marquee \{[^}]*pointer-events: none/);
  });
});
