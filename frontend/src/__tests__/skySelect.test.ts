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
  deletableNode,
  deleteFor,
  nodeById,
  nodeSession,
  removableBox,
  isStopNode,
  lineTouched,
  overlapArea,
  planeToScreen,
  pruneSelection,
  rectFrom,
  removals,
  removalNeedsAsk,
  screenToPlane,
  sessionRuns,
  stopsRun,
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
import { NEW_STOPS, SkySelectionBar, groupHint, putBackAt, takenStops } from "../components/flights/SkySelection";
import { nodeIndexes } from "../components/flights/useSessionLine";
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

  it("lines: the count, Move's hint, Tidy these, Remove, Clear", () => {
    const html = render({ selection: selectLines(["a", "b"]) });
    expect(html).toContain('aria-label="2 lines selected"');
    expect(html).toContain("Drag a bar to move them");
    expect(html).toContain(">Tidy these</button>");
    expect(html).toContain(">Remove</button>");
    expect(html).toContain(">Clear</button>");
  });

  it("lines' Remove asks only when a session in it is running, names it, and says what happens to each kind of line", () => {
    const working = (id: string) => (id === "me" ? { activity: "working" } : { activity: "idle" });
    const html = render({ selection: selectLines(["me", "flight:f1"]), ask: "remove", rowOf: working });
    expect(html).toContain('role="alertdialog"');
    expect(html).toContain('aria-label="Remove 2 lines? line me is working."');
    expect(html).toContain("1 session closes (line me): its agent stops and it moves to Recently closed.");
    expect(html).toContain("1 line of stops with no session leaves the board (line flight:f1)");
    const asking = render({ selection: selectLines(["me", "you"]), ask: "remove", rowOf: () => ({ activity: "clarify" }) });
    expect(asking).toContain('aria-label="Remove 2 lines? line me, line you are running."');
    // Idle sessions and grounded lines: no question (they go at once, with Undo).
    const idle = render({ selection: selectLines(["me", "flight:f1"]), ask: "remove", rowOf: () => ({ activity: "idle" }) });
    expect(idle).not.toContain('role="alertdialog"');
  });

  it("stops' Remove asks only when the flight is at one of them while it runs", () => {
    const flying = { id: "f1", state: "running", cursor: 4, steps: STEPS, step_states: {} } as unknown as Flight;
    const at = { lines: new Set<string>(), stops: new Map([["me", new Set(["stop:s5"])]]) };
    const html = render({ selection: at, flightOf: () => flying, ask: "remove" });
    expect(html).toContain('role="alertdialog"');
    expect(html).toContain("The flight is at “" + STEPS[4].title + "” right now.");
    const ahead = { lines: new Set<string>(), stops: new Map([["me", new Set(["stop:s1", "group:g1", "stop:s5"])]]) };
    const paused = { ...flying, state: "paused" } as unknown as Flight;
    expect(render({ selection: ahead, flightOf: () => paused, ask: "remove" })).not.toContain('role="alertdialog"');
    const before = { lines: new Set<string>(), stops: new Map([["me", new Set(["stop:s1"])]]) };
    expect(render({ selection: before, flightOf: () => flying, ask: "remove" })).not.toContain('role="alertdialog"');
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
    // Round 17 polish: ONE selection ring (2 screen px, solid accent) for every
    // ringable thing, in one selector list; skyPolish.test.ts pins the rule.
    expect(sheet).toContain("outline: calc(2px / var(--sky-z, 1)) solid var(--accent)");
    expect(sheet).toMatch(/\.skl\.is-selected/);
    expect(sheet).toMatch(/\.fl-box\.fl-box-selected/);
    expect(sheet).toMatch(/\.sky-marquee \{[^}]*pointer-events: none/);
  });
});

describe("Delete on the sky (round 12): deleteFor", () => {
  const stops = { lines: new Set<string>(), stops: new Map([["a", new Set(["stop:s1"])]]) };
  const box = (line: string, node: string) => ({ line, node });

  it("a selection is the bar's, wherever the keyboard is", () => {
    expect(deleteFor(stops, box("b", "stop:s2"), null)).toEqual({ kind: "selection" });
    expect(deleteFor(selectLines(["a"]), null, null)).toEqual({ kind: "selection" });
  });

  it("a focused stop or group box leaves its line; a session's, a worker's or a branch's box closes that session", () => {
    expect(deleteFor(NO_SELECTION, box("a", "stop:s1"), null)).toEqual({ kind: "box", line: "a", node: "stop:s1" });
    expect(deleteFor(NO_SELECTION, box("a", "group:g1@2"), null)).toEqual({ kind: "box", line: "a", node: "group:g1@2" });
    expect(deleteFor(NO_SELECTION, box("a", "session:a"), null)).toEqual({ kind: "box", line: "a", node: "session:a" });
    expect(deleteFor(NO_SELECTION, box("a", "child:a-w1"), null)).toEqual({ kind: "box", line: "a", node: "child:a-w1" });
    expect(deleteFor(NO_SELECTION, box("flight:f_1", "stop:s1"), null)).toEqual({ kind: "box", line: "flight:f_1", node: "stop:s1" });
  });

  it("a line itself (its bar, a grounded line's head) asks about the line", () => {
    expect(deleteFor(NO_SELECTION, box("a", ""), null)).toEqual({ kind: "line", line: "a" });
    expect(deleteFor(NO_SELECTION, box("flight:f_1", ""), null)).toEqual({ kind: "line", line: "flight:f_1" });
  });

  it("the +, a rung, the flown pill, the intake lane: taken and nothing done — never the keymap's close", () => {
    for (const n of ["add", "rung:push", "flown"]) expect(deleteFor(NO_SELECTION, box("a", n), null)).toEqual({ kind: "swallow" });
    expect(deleteFor(NO_SELECTION, box("intake", ""), null)).toEqual({ kind: "swallow" });
  });

  it("on the sky's empty space: the focused line's selected box, else the keymap's Delete as before", () => {
    expect(deleteFor(NO_SELECTION, null, box("a", "stop:s3"))).toEqual({ kind: "box", line: "a", node: "stop:s3" });
    expect(deleteFor(NO_SELECTION, null, null)).toEqual({ kind: "keymap" });
    // The keyboard on a box wins over the line's selected one.
    expect(deleteFor(NO_SELECTION, box("a", "stop:s1"), box("a", "stop:s3"))).toEqual({ kind: "box", line: "a", node: "stop:s1" });
  });

  it("the helpers: which nodes Delete acts on, the session a box stands for, a box by id, what may leave", () => {
    expect(["stop:s1", "group:g", "session:a", "child:w", "branch:w"].every(deletableNode)).toBe(true);
    expect(["add", "rung:pr", "flown", "session:"].some(deletableNode)).toBe(false);
    expect(nodeSession("session:sc-12")).toBe("sc-12");
    expect(nodeSession("branch:a::b")).toBe("a::b");
    expect(nodeSession("stop:s1")).toBe("");
    const sub = { id: "stop:s2", kind: "stop" };
    const nodes = [{ id: "session:a", kind: "session" }, { id: "group:g", kind: "group", group: { nodes: [sub] } }];
    expect(nodeById(nodes, "group:g")?.kind).toBe("group");
    expect(nodeById(nodes, "stop:s2")).toBe(sub);
    expect(nodeById(nodes, "stop:nope")).toBeNull();
    expect(removableBox({ kind: "stop" })).toBe(true);
    expect(removableBox({ kind: "group", editable: true })).toBe(true);
    expect(removableBox({ kind: "stop", editable: false })).toBe(false);
    expect(removableBox({ kind: "session" })).toBe(false);
    expect(removableBox(null)).toBe(false);
  });
});

describe("Delete wiring (round 12)", () => {
  const sky = code("components/flights/SkyView.tsx");
  const line = code("components/flights/SkyLine.tsx");
  const grounded = code("components/flights/GroundedLine.tsx");

  it("SkyView routes it before the keymap, through each line's registered handle", () => {
    expect(sky).toContain("const d = deleteFor(skyKeysOwn(el) ? sel : NO_SELECTION, at === \"sky\" ? null : at, at === \"sky\" ? selectedBox() : null);");
    expect(sky).toContain('else if (d.kind === "box" || d.kind === "line") deleters.current.get(d.line)?.(d.kind === "box" ? d.node : "");');
    expect(sky.match(/registerDelete=\{at \? registerFor\(id\) : undefined\}/g)?.length).toBe(2);
    // The keymap sees the sky's selection on the viewport.
    expect(sky).toContain('data-sky-box={selectionEmpty(sel) ? undefined : ""}');
  });

  it("a session line: a stop through the canvas's Remove, a session's box or the line asks inline to close", () => {
    expect(line).toContain("shown.canvas.onRemove(n!);");
    expect(line).toContain("const t = node ? nodeSession(node) : p.title;");
    expect(line).toMatch(/<InlineConfirm\s+title=\{"Close “" \+ windowName\(closing\) \+ "”\?"\}/);
    // Round 14: an idle session closes at once (undoable); a running one asks.
    expect(line).toContain('if (removalNeedsAsk({ kind: "session", row: p.rows.find((r) => r.title === t) })) setClosing(t);');
    expect(line).toContain("else void closeLine(t);");
    expect(line).toContain("await closeLine(closing);");
    expect(line).not.toMatch(/window\.(confirm|prompt|alert)/);
    // A press on the bar focuses the line, so Delete lands there.
    expect(line).toMatch(/rootRef\.current\?\.focus\(\{ preventScroll: true \}\);\s*if \(p\.draggable === false\) return;\s*p\.onDragStart\?\.\(e\);/);
    expect(line).toContain("data-sky-box");
  });

  it("a grounded line: a stop through its canvas's Remove, anything else removes the line (at once, undoable)", () => {
    expect(grounded).toContain("line.canvas.onRemove(n);");
    expect(grounded).toMatch(/void remove\(\);\s*return true;/);
    expect(grounded).toMatch(/if \(p\.draggable === false\) return;\s*p\.onDragStart\?\.\(e\);/);
  });

  it("the selection bar never removes the intake lane", () => {
    const bar = code("components/flights/SkySelection.tsx");
    expect(bar).toContain("const removable = lines.filter((id) => id !== INTAKE_KEY);");
    expect(bar).toContain("for (const id of removable) {");
  });
});

describe("Shift+click on a box (toggleStop)", () => {
  it("starts a stop selection of that line, adds and removes boxes, and clears when empty", async () => {
    const { NO_SELECTION, toggleStop } = await import("../lib/skySelect");
    const a = toggleStop(NO_SELECTION, "l1", "stop:s1");
    expect([...a.stops.get("l1")!]).toEqual(["stop:s1"]);
    expect(a.lines.size).toBe(0);
    const b = toggleStop(a, "l1", "group:g1");
    expect([...b.stops.get("l1")!]).toEqual(["stop:s1", "group:g1"]);
    const c = toggleStop(b, "l1", "stop:s1");
    expect([...c.stops.get("l1")!]).toEqual(["group:g1"]);
    expect(toggleStop(c, "l1", "group:g1")).toBe(NO_SELECTION);
  });
  it("a box of another line, or a line selection, gives way to the new line's stops", async () => {
    const { toggleStop, selectLines, NO_SELECTION } = await import("../lib/skySelect");
    const one = toggleStop(NO_SELECTION, "l1", "stop:s1");
    const other = toggleStop(one, "l2", "stop:s9");
    expect([...other.stops.keys()]).toEqual(["l2"]);
    const fromLines = toggleStop(selectLines(["l1", "l2"]), "l1", "stop:s1");
    expect(fromLines.lines.size).toBe(0);
    expect([...fromLines.stops.get("l1")!]).toEqual(["stop:s1"]);
  });
});

describe("round 14: removal asks only when something runs (removalNeedsAsk)", () => {
  const running = { state: "running", cursor: 2 };

  it("a session whose agent works or asks you something; never an idle, limited or offline one", () => {
    for (const a of ["working", "clarify"]) expect(removalNeedsAsk({ kind: "session", row: { activity: a } })).toBe(true);
    expect(removalNeedsAsk({ kind: "session", row: { activity: "idle", asking: { kind: "dialog" } } })).toBe(true);
    for (const a of ["idle", "limit", "offline", ""]) expect(removalNeedsAsk({ kind: "session", row: { activity: a } })).toBe(false);
    expect(removalNeedsAsk({ kind: "session", row: undefined })).toBe(false);
    expect(sessionRuns({ activity: "idle", asking: null })).toBe(false);
  });

  it("the stop a running (or waiting-on-you) flight is at; not one ahead, not a flown one, not a paused or halted flight", () => {
    expect(removalNeedsAsk({ kind: "stops", indexes: [2], flight: running })).toBe(true);
    expect(removalNeedsAsk({ kind: "stops", indexes: [1, 2, 3], flight: { state: "waiting_you", cursor: 2 } })).toBe(true);
    expect(removalNeedsAsk({ kind: "stops", indexes: [3], flight: running })).toBe(false);
    expect(removalNeedsAsk({ kind: "stops", indexes: [0, 1], flight: running })).toBe(false);
    for (const state of ["paused", "halted", "grounded", "done"]) expect(stopsRun([2], { state, cursor: 2 })).toBe(false);
    expect(removalNeedsAsk({ kind: "stops", indexes: [2], flight: null })).toBe(false);
  });

  it("a grounded line never; a selection when anything in it runs", () => {
    expect(removalNeedsAsk({ kind: "grounded" })).toBe(false);
    const idle = { kind: "session" as const, row: { activity: "idle" } };
    expect(removalNeedsAsk({ kind: "selection", items: [idle, { kind: "grounded" }] })).toBe(false);
    expect(removalNeedsAsk({ kind: "selection", items: [idle, { kind: "session", row: { activity: "working" } }] })).toBe(true);
    expect(removalNeedsAsk({ kind: "selection", items: [] })).toBe(false);
  });

  it("a box's step indexes: a stop its own, a group every sub-step", () => {
    expect(nodeIndexes({ kind: "stop", index: 4 })).toEqual([4]);
    expect(nodeIndexes({ kind: "group", index: 1, group: { total: 3 } as never })).toEqual([1, 2, 3]);
    expect(nodeIndexes({ kind: "add" })).toEqual([]);
  });
});

describe("round 14: the selection's Remove puts the lot back (one Undo)", () => {
  it("what was taken, first index first: a group by all its steps and its name, a stop alone", () => {
    const t = takenStops(["stop:s5", "group:g1", "stop:s1"], STEPS);
    expect(t.map((x) => [x.index, x.steps.map((s) => s.id), x.group])).toEqual([
      [0, ["s1"], ""],
      [1, ["s2", "s3", "s4"], "G g1"],
      [4, ["s5"], ""],
    ]);
    expect(takenStops(["add", "stop:nope"], STEPS)).toEqual([]);
  });

  it("each goes back at its old index while that is still ahead of the flight and inside the line growing back, else at the end", () => {
    // s1 [g1: s2 s3 s4] s5 [g2: s6 s7]; g1 and s5 taken leaves s1 s6 s7.
    const taken = takenStops(["group:g1", "stop:s5"], STEPS);
    // Cursor still at s1: both where they were (the group at 1, then s5 at 4).
    expect(putBackAt(taken, { cursor: 0, length: 3 })).toEqual([1, 4]);
    // The flight has reached index 1 meanwhile: the group goes to the end,
    // and s5 after it (at 4 it would land inside the group).
    expect(putBackAt(taken, { cursor: 1, length: 3 })).toEqual([undefined, undefined]);
    // A lone stop at the very end of the line: the end.
    expect(putBackAt(takenStops(["stop:s1"], STEPS), { cursor: -1, length: 0 })).toEqual([undefined]);
  });

  it("the bar pushes ONE entry for the lot and its toast takes that entry", () => {
    const bar = code("components/flights/SkySelection.tsx");
    expect(bar.match(/pushUndo\(/g)?.length).toBe(2);
    expect(bar).toContain('label: "Remove " + plural(n, "line"),');
    expect(bar).toContain('label: "Remove " + plural(n, "stop"),');
    expect(bar).toContain("await reopenClosed(closed);");
    expect(bar).toMatch(/action: undoAction\(entry, "Undo: put " \+ plural\(n, "stop"\) \+ " back"\)/);
    // The "past 3" question is gone: Remove goes at once unless something runs.
    expect(bar).not.toContain("REMOVE_AT_ONCE");
    expect(bar).toContain('if (p.ask === "remove" && !asks && !busy) void removeNow();');
  });
});
