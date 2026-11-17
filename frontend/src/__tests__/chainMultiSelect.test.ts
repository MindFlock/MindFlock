/** Ctrl/⌘ and Shift clicks on a chain's stops, and moving the selection
 * together (docs/flights.md "Selecting stops in a chain"): the selection
 * model (lib/skySelect `clickSelect`, `chainOrder`, `rangeOf`) and the move
 * math (`moveFirstOpen`, `dropBefore`, `blockMove`, `movesToOrder`,
 * `undoMoves`), plus the wiring the Sky and the pane's Route tab carry. */

import { describe, expect, it } from "vitest";
import { code, css } from "./sourceText";
import {
  NO_SELECTION,
  blockMove,
  chainOrder,
  clickSelect,
  dropBefore,
  inOrder,
  moveFirstOpen,
  movesToOrder,
  rangeOf,
  selectLines,
  undoMoves,
  type SkySelection,
  type StepMove,
} from "../lib/skySelect";
import { skyBoxFocused } from "../lib/keymap";

const nodesOf = (s: SkySelection, line = "L") => [...(s.stops.get(line) || [])].sort();

// A chain: s1, then a group g (s2, s3), then s4, s5 — the group box first-indexed at 1.
const ORDER = chainOrder([
  { id: "head", index: undefined },
  { id: "stop:s5", index: 4 },
  { id: "group:g", index: 1 },
  { id: "stop:s1", index: 0 },
  { id: "child:w1", index: 2 },
  { id: "stop:s4", index: 3 },
]);

describe("chainOrder / rangeOf", () => {
  it("lists only stop and group boxes, in flying order", () => {
    expect(ORDER).toEqual(["stop:s1", "group:g", "stop:s4", "stop:s5"]);
  });
  it("a range is inclusive, either way round, and empty off the chain", () => {
    expect(rangeOf(ORDER, "stop:s1", "stop:s4")).toEqual(["stop:s1", "group:g", "stop:s4"]);
    expect(rangeOf(ORDER, "stop:s5", "group:g")).toEqual(["group:g", "stop:s4", "stop:s5"]);
    expect(rangeOf(ORDER, "stop:s4", "stop:s4")).toEqual(["stop:s4"]);
    expect(rangeOf(ORDER, "stop:s1", "stop:nope")).toEqual([]);
  });
});

describe("clickSelect", () => {
  it("Ctrl toggles a box in and out, and the box becomes the anchor", () => {
    const a = clickSelect(NO_SELECTION, null, "L", "stop:s1", { ctrl: true }, ORDER);
    expect(nodesOf(a.selection)).toEqual(["stop:s1"]);
    expect(a.anchor).toEqual({ line: "L", node: "stop:s1" });
    const b = clickSelect(a.selection, a.anchor, "L", "stop:s4", { ctrl: true }, ORDER);
    expect(nodesOf(b.selection)).toEqual(["stop:s1", "stop:s4"]);
    expect(b.anchor).toEqual({ line: "L", node: "stop:s4" });
    const c = clickSelect(b.selection, b.anchor, "L", "stop:s1", { ctrl: true }, ORDER);
    expect(nodesOf(c.selection)).toEqual(["stop:s4"]);
    // Emptied: nothing selected.
    expect(clickSelect(c.selection, c.anchor, "L", "stop:s4", { ctrl: true }, ORDER).selection).toBe(NO_SELECTION);
  });

  it("Shift takes the range from the anchor — across a group box — and keeps the anchor", () => {
    const a = clickSelect(NO_SELECTION, null, "L", "stop:s1", {}, ORDER); // a plain click: the anchor
    expect(a.selection).toBe(NO_SELECTION);
    const b = clickSelect(a.selection, a.anchor, "L", "stop:s4", { shift: true }, ORDER);
    expect(nodesOf(b.selection)).toEqual(["group:g", "stop:s1", "stop:s4"]);
    expect(b.anchor).toEqual({ line: "L", node: "stop:s1" });
    // Shift again from the same anchor: the range is replaced, not added to.
    const c = clickSelect(b.selection, b.anchor, "L", "group:g", { shift: true }, ORDER);
    expect(nodesOf(c.selection)).toEqual(["group:g", "stop:s1"]);
    // Backwards works too.
    const d = clickSelect(NO_SELECTION, { line: "L", node: "stop:s5" }, "L", "group:g", { shift: true }, ORDER);
    expect(nodesOf(d.selection)).toEqual(["group:g", "stop:s4", "stop:s5"]);
  });

  it("Ctrl+Shift adds the range to what is selected", () => {
    const a = clickSelect(NO_SELECTION, null, "L", "stop:s5", { ctrl: true }, ORDER);
    const b = clickSelect(a.selection, { line: "L", node: "stop:s1" }, "L", "group:g", { ctrl: true, shift: true }, ORDER);
    expect(nodesOf(b.selection)).toEqual(["group:g", "stop:s1", "stop:s5"]);
  });

  it("Shift with no anchor on this line toggles like Ctrl", () => {
    const a = clickSelect(NO_SELECTION, { line: "other", node: "stop:x" }, "L", "stop:s4", { shift: true }, ORDER);
    expect(nodesOf(a.selection)).toEqual(["stop:s4"]);
    expect(a.anchor).toEqual({ line: "L", node: "stop:s4" });
  });

  it("a plain click clears a stop selection (lines stay), sets the anchor", () => {
    const a = clickSelect(NO_SELECTION, null, "L", "stop:s1", { ctrl: true }, ORDER);
    const b = clickSelect(a.selection, a.anchor, "L", "stop:s5", {}, ORDER);
    expect(b.selection).toBe(NO_SELECTION);
    expect(b.anchor).toEqual({ line: "L", node: "stop:s5" });
    const lines = selectLines(["L", "M"]);
    expect(clickSelect(lines, null, "L", "stop:s1", {}, ORDER).selection).toBe(lines);
  });

  it("a selection is one line's: a box of another line starts over there", () => {
    const a = clickSelect(NO_SELECTION, null, "L", "stop:s1", { ctrl: true }, ORDER);
    const b = clickSelect(a.selection, a.anchor, "M", "stop:t1", { ctrl: true }, ["stop:t1"]);
    expect([...b.selection.stops.keys()]).toEqual(["M"]);
  });

  it("a node that is not a stop or a group changes nothing", () => {
    const a = clickSelect(NO_SELECTION, null, "L", "stop:s1", { ctrl: true }, ORDER);
    const b = clickSelect(a.selection, a.anchor, "L", "child:w1", { ctrl: true }, ORDER);
    expect(b.selection).toBe(a.selection);
    expect(b.anchor).toBe(a.anchor);
  });
});

// --- The move math -----------------------------------------------------------

const ids = (...xs: string[]) => xs.map((id) => ({ id }));
/** The server's `op_move_step`: pop, then insert at `at` of the final list. */
const apply = (list: readonly string[], moves: readonly StepMove[]) => {
  const out = list.slice();
  for (const m of moves) {
    const i = out.indexOf(m.step);
    out.splice(i, 1);
    out.splice(Math.min(m.at, out.length), 0, m.step);
  }
  return out;
};

describe("moveFirstOpen", () => {
  it("anywhere on a route not flying yet, else after the current stop and its parallel group", () => {
    expect(moveFirstOpen({ state: "grounded", cursor: 0, steps: [] })).toBe(0);
    expect(moveFirstOpen({ state: "ready", cursor: 0, steps: [] })).toBe(0);
    expect(moveFirstOpen({ state: "running", cursor: 1, steps: [{ id: "a" }, { id: "b" }, { id: "c" }] as never })).toBe(2);
    const par = [{ id: "a" }, { id: "b", parallel: { id: "p" } }, { id: "c", parallel: { id: "p" } }, { id: "d" }];
    expect(moveFirstOpen({ state: "running", cursor: 1, steps: par as never })).toBe(3);
  });
});

describe("dropBefore", () => {
  const G = { id: "g", name: "G", plan_id: "" };
  const steps = [{ id: "a" }, { id: "b", group: G }, { id: "c", group: G }, { id: "d", parallel: { id: "p" } }, { id: "e", parallel: { id: "p" } }, { id: "f" }];
  it("the head is the front of what can move; a box: right after it", () => {
    expect(dropBefore("", steps, 0)).toBe(0);
    expect(dropBefore("", steps, 2)).toBe(2);
    expect(dropBefore("stop:a", steps, 0)).toBe(1);
    expect(dropBefore("stop:f", steps, 0)).toBe(6);
  });
  it("a group box: after its last sub-step; a parallel stop: after its whole group", () => {
    expect(dropBefore("group:g", steps, 0)).toBe(3);
    expect(dropBefore("stop:d", steps, 0)).toBe(5);
  });
  it("a box not on the route: -1", () => {
    expect(dropBefore("stop:zz", steps, 0)).toBe(-1);
  });
});

describe("blockMove", () => {
  const S = ids("a", "b", "c", "d", "e", "f");
  const list = S.map((s) => s.id);

  it("moves a scattered selection together, in its order, right after the target", () => {
    // b and e dropped after f (before index 6 = the end).
    const r = blockMove(S, [1, 4], 6, 0);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.order).toEqual(["a", "c", "d", "f", "b", "e"]);
    expect(apply(list, r.moves)).toEqual(r.order);
    expect(r.moves.length).toBe(2);
  });

  it("moving earlier: to the front", () => {
    const r = blockMove(S, [3, 5], 0, 0);
    if (!r.ok) throw new Error(r.why);
    expect(r.order).toEqual(["d", "f", "a", "b", "c", "e"]);
    expect(apply(list, r.moves)).toEqual(r.order);
    expect(r.moves).toEqual([
      { step: "d", at: 0 },
      { step: "f", at: 1 },
    ]);
  });

  it("dropping into the middle of the selection's own span closes it up", () => {
    // a, c, e dropped after c (before index 3): b slides out of their way.
    const r = blockMove(S, [0, 2, 4], 3, 0);
    if (!r.ok) throw new Error(r.why);
    expect(r.order).toEqual(["b", "a", "c", "e", "d", "f"]);
    expect(apply(list, r.moves)).toEqual(r.order);
  });

  it("a block already in place needs no move", () => {
    const r = blockMove(S, [1, 2], 3, 0);
    if (!r.ok) throw new Error(r.why);
    expect(r.order).toEqual(list);
    expect(r.moves).toEqual([]);
  });

  it("a group moves whole with its neighbours (every sub-step's index is picked)", () => {
    const r = blockMove(S, [1, 2, 5], 1, 0); // group b+c and f, to right after a
    if (!r.ok) throw new Error(r.why);
    expect(r.order).toEqual(["a", "b", "c", "f", "d", "e"]);
    expect(apply(list, r.moves)).toEqual(r.order);
    expect(r.moves).toEqual([{ step: "f", at: 3 }]);
  });

  it("refuses a stop at or before where the route is, and a place before it", () => {
    expect(blockMove(S, [1, 4], 6, 2)).toEqual({ ok: false, why: "A stop that has run, or is running, can't move" });
    expect(blockMove(S, [3, 4], 1, 2)).toEqual({ ok: false, why: "Stops can only move after the one the route is at" });
    expect(blockMove(S, [], 3, 0).ok).toBe(false);
  });

  it("every move it makes stays at or after the first open index", () => {
    const r = blockMove(S, [2, 5], 2, 2);
    if (!r.ok) throw new Error(r.why);
    expect(r.order).toEqual(["a", "b", "c", "f", "d", "e"]);
    expect(r.moves.every((m) => m.at >= 2)).toBe(true);
    expect(apply(list, r.moves)).toEqual(r.order);
  });

  it("part of a parallel group can't leave it; the block never lands inside one", () => {
    const P = [{ id: "a" }, { id: "b", parallel: { id: "p" } }, { id: "c", parallel: { id: "p" } }, { id: "d" }];
    expect(blockMove(P, [1], 4, 0)).toEqual({ ok: false, why: "Pick every stop of a side-by-side group, or none of them" });
    const whole = blockMove(P, [1, 2], 4, 0);
    expect(whole.ok && whole.order).toEqual(["a", "d", "b", "c"]);
    expect(blockMove(P, [3], 2, 0)).toEqual({ ok: false, why: "Stops can't land inside a side-by-side group" });
  });
});

describe("movesToOrder", () => {
  it("moving later goes last-first, so a stop never passes the Loop that repeats from it", () => {
    // t then its loop L, moved together to the end: L goes first, then t before it.
    const moves = movesToOrder(["t", "L", "x", "y"], ["x", "y", "t", "L"], ["t", "L"]);
    expect(moves.map((m) => m.step)).toEqual(["L", "t"]);
    // Every step on the way keeps t before L.
    let list = ["t", "L", "x", "y"];
    for (const m of moves) {
      list = apply(list, [m]);
      expect(list.indexOf("t")).toBeLessThan(list.indexOf("L"));
    }
    expect(list).toEqual(["x", "y", "t", "L"]);
  });

  it("moving earlier goes first-first, with the same guarantee", () => {
    const moves = movesToOrder(["x", "y", "t", "L"], ["t", "L", "x", "y"], ["t", "L"]);
    expect(moves.map((m) => m.step)).toEqual(["t", "L"]);
    expect(apply(["x", "y", "t", "L"], moves)).toEqual(["t", "L", "x", "y"]);
  });

  it("only the moving ids ever move", () => {
    const moves = movesToOrder(["a", "b", "c", "d", "e"], ["c", "d", "e", "a", "b"], ["a", "b"]);
    expect(moves.map((m) => m.step).sort()).toEqual(["a", "b"]);
  });
});

describe("undoMoves / inOrder", () => {
  it("puts the moved stops back while the route is otherwise unchanged", () => {
    const before = ["a", "b", "c", "d", "e"];
    const r = blockMove(ids(...before), [0, 1], 5, 0);
    if (!r.ok) throw new Error(r.why);
    const back = undoMoves(r.order, before, ["a", "b"]);
    expect(back).not.toBeNull();
    expect(apply(r.order, back!)).toEqual(before);
  });

  it("nothing to undo once the route changed (a stop added, removed, or the others reordered)", () => {
    const before = ["a", "b", "c"];
    expect(undoMoves(["b", "c", "a", "z"], before, ["a"])).toBeNull();
    expect(undoMoves(["b", "a"], before, ["a"])).toBeNull();
    expect(undoMoves(["c", "b", "a"], before, ["a"])).toBeNull();
  });

  it("inOrder shows the move at once, and leaves a list it doesn't match alone", () => {
    const steps = ids("a", "b", "c");
    expect(inOrder(steps, ["c", "a", "b"]).map((s) => s.id)).toEqual(["c", "a", "b"]);
    expect(inOrder(steps, ["c", "a"]).map((s) => s.id)).toEqual(["a", "b", "c"]);
  });
});

// --- The wiring ----------------------------------------------------------------

describe("the wiring", () => {
  it("SkyChain: Ctrl/⌘ or Shift on a stop is the selection's; a selected tile drags the selection", () => {
    const chain = code("components/flights/SkyChain.tsx");
    expect(chain).toContain("const mods = { ctrl: e.ctrlKey || e.metaKey, shift: e.shiftKey };");
    expect(chain).toContain("if (box && (mods.ctrl || mods.shift)) {");
    expect(chain).toContain("const group = p.highlighted?.has(n.id) ?");
    expect(chain).toContain("if (d.group && d.over && p.onSelectionDrop) {");
    expect(chain).toContain("moveNodes(p.graph, spotsOf(drag))");
  });

  it("the Sky and the Route tab both hand the chain onModSelect and onSelectionDrop", () => {
    const sky = code("components/flights/SkyView.tsx");
    expect(sky.match(/onModSelect=\{at \? modSelectFor\(id\) : undefined\}/g)?.length).toBe(2);
    expect(sky.match(/onSelectionDrop=\{at \? dropSelFor\(id\) : undefined\}/g)?.length).toBe(2);
    expect(sky).toContain("void moveSelectedStops(f, nodes, target?.id || \"\", kinds);");
    for (const f of ["components/flights/SkyLine.tsx", "components/flights/GroundedLine.tsx"]) {
      const src = code(f);
      expect(src).toContain("onModSelect={p.onModSelect}");
      expect(src).toContain("onSelectionDrop={p.onSelectionDrop}");
      expect(src).not.toContain("onShiftSelect");
    }
    const route = code("components/flights/FlightChain.tsx");
    expect(route).toContain("highlighted={sel.stops.get(title)}");
    expect(route).toContain("onModSelect={onModSelect}");
    expect(route).toContain("<SkySelectionBar\n          docked");
    expect(route).toContain('setAsk("remove");');
    expect(route).not.toMatch(/window\.(confirm|prompt|alert)/);
  });

  it("the move is ONE Undo, refused for stops that ran (as a single stop's move is)", () => {
    const bar = code("components/flights/SkySelection.tsx");
    expect(bar).toContain('const label = "Move " + plural(n, "stop");');
    expect(bar).toContain('if (idx.some((i) => !stops[i]?.editable)) return "A stop that has run, or is running, can\'t move";');
  });

  it("a multi-selected tile wears the accent", () => {
    expect(css("components/flights/SkyChain.css")).toMatch(/\.sky-node\.is-highlighted \.sky-tile \{[^}]*outline: 2px solid var\(--accent\)/);
    expect(css("components/flights/SkySelection.css")).toMatch(/\.sky-selbar\.is-docked \{[^}]*position: sticky/);
  });
});

describe("Delete on a Route tab tile is the chain's, not the keymap's close", () => {
  const el = (chain: Array<{ sel: string; node?: string }>) => {
    const make = (from: number): unknown => ({
      tagName: "DIV",
      getAttribute: (k: string) => (k === "data-node" ? chain[from]?.node ?? null : null),
      closest: (sel: string) => {
        const parts = sel.split(",").map((x) => x.trim());
        const i = chain.findIndex((c, k) => k >= from && parts.includes(c.sel));
        return i < 0 ? null : make(i);
      },
    });
    return make(0) as Element;
  };
  const VIEW = { sel: ".fl-chain-view" };
  it("a stop or group tile, or the chain holding a selection", () => {
    expect(skyBoxFocused(el([{ sel: "[data-node]", node: "stop:s1" }, VIEW, { sel: ".pane" }]))).toBe(true);
    expect(skyBoxFocused(el([{ sel: "[data-node]", node: "group:g1" }, VIEW]))).toBe(true);
    expect(skyBoxFocused(el([{ sel: "[data-node]", node: "head" }, VIEW]))).toBe(false);
    expect(skyBoxFocused(el([{ sel: "[data-node]", node: "head" }, { sel: ".fl-chain-view[data-sky-box]" }, VIEW]))).toBe(true);
    // Outside a Route tab and the Sky: still the keymap's.
    expect(skyBoxFocused(el([{ sel: "[data-node]", node: "stop:s1" }, { sel: ".pane" }]))).toBe(false);
  });
});
