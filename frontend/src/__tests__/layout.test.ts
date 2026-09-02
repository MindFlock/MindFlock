import { describe, it, expect } from "vitest";
import type { Instance } from "../api/types";
import {
  MAX_VISIBLE,
  DROP_PH,
  viewCap,
  computeVisible,
  computeVisibleSlots,
  balancedRows,
  reconcileGridRows,
  placeInGrid,
  previewRowsFor,
  dropSideFor,
  type VisibleOpts,
} from "../components/grid/layout";

/** computeVisible/orderedInstances only read `.title`. */
const insts = (...titles: string[]): Instance[] =>
  titles.map((title) => ({ title }) as unknown as Instance);

const opts = (o: Partial<VisibleOpts>): VisibleOpts => ({
  hidden: new Set(),
  viewMode: "auto",
  mru: [],
  order: [],
  ...o,
});

const rect = (w: number, h: number): DOMRect =>
  ({ left: 0, top: 0, width: w, height: h }) as DOMRect;

describe("viewCap", () => {
  it("auto is unbounded, numeric views parse", () => {
    expect(viewCap("auto")).toBe(Infinity);
    expect(viewCap("2")).toBe(2);
    expect(viewCap("9")).toBe(9);
  });
});

describe("computeVisible", () => {
  it("returns every non-hidden instance under a fixed cap", () => {
    const out = computeVisible(insts("a", "b", "c"), opts({ hidden: new Set(["b"]) }));
    expect(out.map((i) => i.title)).toEqual(["a", "c"]);
  });

  it("caps to N by MRU, but renders survivors in stable order", () => {
    // 4 shown, cap 2, MRU prefers c then a -> {a,c}; displayed a before c.
    const out = computeVisible(
      insts("a", "b", "c", "d"),
      opts({ viewMode: "2", mru: ["c", "a"] })
    );
    expect(out.map((i) => i.title)).toEqual(["a", "c"]);
  });

  it("fills the cap from stable order when MRU is short", () => {
    const out = computeVisible(insts("a", "b", "c", "d"), opts({ viewMode: "2", mru: ["d"] }));
    // MRU=[d], rest fills [a,b,c] -> chosen first 2 of [d,a,b,c] = {d,a}; stable order a,d.
    expect(out.map((i) => i.title)).toEqual(["a", "d"]);
  });

  it("never exceeds MAX_VISIBLE even in auto", () => {
    const many = insts(...Array.from({ length: 12 }, (_, i) => `w${i}`));
    const out = computeVisible(many, opts({ viewMode: "auto" }));
    expect(out).toHaveLength(MAX_VISIBLE);
  });
});

describe("computeVisibleSlots", () => {
  // The sentinels the grid uses for non-session windows (assistant, logs, an
  // extension's table). Their shape does not matter here — only that they
  // compete for the same slots.
  const CHAT = "\u0000chat";
  const DB = "\u0000ext:dbclient:table";

  it("counts a window against the cap, like a session", () => {
    // Before, specials were appended AFTER the cap: "view: 1" meant one session
    // PLUS every open window.
    expect(computeVisibleSlots(insts("a", "b"), [CHAT], opts({ viewMode: "1" }))).toEqual(["a"]);
    expect(
      computeVisibleSlots(insts("a", "b"), [CHAT, DB], opts({ viewMode: "2" }))
    ).toEqual(["a", "b"]);
  });

  it("gives a window the slot when it is the one most recently picked", () => {
    expect(
      computeVisibleSlots(insts("a", "b"), [CHAT], opts({ viewMode: "1", mru: [CHAT] }))
    ).toEqual([CHAT]);
    expect(
      computeVisibleSlots(insts("a", "b"), [CHAT, DB], opts({ viewMode: "2", mru: [DB, "b"] }))
    ).toEqual(["b", DB]);
  });

  it("keeps the display order whatever the MRU says", () => {
    // The MRU decides WHO is on screen, never WHERE — a window must not hop
    // around the grid because you clicked it.
    expect(
      computeVisibleSlots(insts("a", "b"), [CHAT], opts({ viewMode: "auto", mru: [CHAT, "b"] }))
    ).toEqual(["a", "b", CHAT]);
  });

  it("shows everything when uncapped, and never more than MAX_VISIBLE", () => {
    const many = insts(...Array.from({ length: MAX_VISIBLE + 4 }, (_, i) => "s" + i));
    expect(computeVisibleSlots(many, [CHAT], opts({ viewMode: "auto" })).length).toBe(MAX_VISIBLE);
    expect(computeVisibleSlots(insts("a"), [CHAT], opts({ viewMode: "auto" }))).toEqual(["a", CHAT]);
  });

  it("leaves hidden windows out", () => {
    expect(
      computeVisibleSlots(insts("a", "b"), [CHAT], opts({ hidden: new Set(["b", CHAT]) }))
    ).toEqual(["a"]);
  });

  it("drag order decides who fills the cap when the MRU is short", () => {
    // The window sits first in the saved order and the MRU says nothing: it
    // fills the cap ahead of session b — where a window sits is now
    // something a drag can change.
    expect(
      computeVisibleSlots(insts("a", "b"), [CHAT], opts({ viewMode: "2", order: [CHAT, "a"] }))
    ).toEqual([CHAT, "a"]);
  });

  it("interleaves windows by the saved drag order, like the rail", () => {
    // A window dragged between two sessions keeps that slot here too; one the
    // order has never seen still lands after the sessions (the old append).
    expect(
      computeVisibleSlots(insts("a", "b"), [CHAT, DB], opts({ order: ["a", CHAT, "b"] }))
    ).toEqual(["a", CHAT, "b", DB]);
  });
});

describe("balancedRows", () => {
  it("lays out squarish grids", () => {
    expect(balancedRows([])).toEqual([]);
    expect(balancedRows(["a", "b"])).toEqual([["a", "b"]]);
    expect(balancedRows(["a", "b", "c", "d"])).toEqual([
      ["a", "b"],
      ["c", "d"],
    ]);
  });
});

describe("reconcileGridRows", () => {
  it("swaps a replacement into the exact cell it vacated", () => {
    const out = reconcileGridRows(
      [
        ["a", "b"],
        ["c", "d"],
      ],
      ["a", "x", "c", "d"]
    );
    expect(out).toEqual([
      ["a", "x"],
      ["c", "d"],
    ]);
  });

  it("appends a genuinely new window without moving the rest", () => {
    expect(reconcileGridRows([["a"]], ["a", "b"])).toEqual([["a", "b"]]);
  });

  it("falls back to a balanced grid when nothing carries over", () => {
    expect(reconcileGridRows([], ["a", "b"])).toEqual([["a", "b"]]);
  });
});

describe("placeInGrid", () => {
  it("is a no-op when dropped on itself or with no drag", () => {
    const prev = [["a", "b"]];
    expect(placeInGrid(prev, "a", "a", "left")).toBe(prev);
    expect(placeInGrid(prev, "", "a", "left")).toBe(prev);
  });

  it("inserts left/right in the target's row", () => {
    expect(placeInGrid([["a", "b", "c"]], "c", "a", "left")).toEqual([["c", "a", "b"]]);
    expect(placeInGrid([["a", "b", "c"]], "a", "c", "right")).toEqual([["b", "c", "a"]]);
  });

  it("inserts top/bottom as a new stacked row", () => {
    expect(placeInGrid([["a", "b"]], "b", "a", "top")).toEqual([["b"], ["a"]]);
    expect(placeInGrid([["a", "b"]], "b", "a", "bottom")).toEqual([["a"], ["b"]]);
  });

  it("appends a new row when the target is gone", () => {
    expect(placeInGrid([["a"]], "x", "missing", "left")).toEqual([["a"], ["x"]]);
  });
});

describe("previewRowsFor", () => {
  it("inserts the DROP_PH placeholder beside the target", () => {
    expect(previewRowsFor([["a", "b"]], "a", "b", "right")).toEqual([["b", DROP_PH]]);
    expect(previewRowsFor([["a", "b"]], "a", "b", "top")).toEqual([[DROP_PH], ["b"]]);
  });

  it("has no self-drop guard (unlike placeInGrid)", () => {
    expect(previewRowsFor([["a"]], "a", "a", "left")).toEqual([[DROP_PH]]);
  });
});

describe("dropSideFor", () => {
  it("picks the nearest of the four sides", () => {
    expect(dropSideFor(rect(100, 100), 10, 50)).toBe("left");
    expect(dropSideFor(rect(100, 100), 90, 50)).toBe("right");
    expect(dropSideFor(rect(100, 100), 50, 10)).toBe("top");
    expect(dropSideFor(rect(100, 100), 50, 90)).toBe("bottom");
  });
});
