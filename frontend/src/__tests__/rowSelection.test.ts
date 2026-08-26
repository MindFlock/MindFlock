/** Checkbox multi-select behind the Recently-closed and Workspaces dialogs. */

import { describe, it, expect } from "vitest";
import {
  applyKeys,
  previewList,
  rangeBetween,
  selectAllState,
  selectedInOrder,
} from "../lib/rowSelection";

describe("selectedInOrder", () => {
  const rows = ["a", "b", "c", "d"];

  it("returns list order, not click order", () => {
    expect(selectedInOrder(new Set(["d", "b"]), rows)).toEqual(["b", "d"]);
  });

  it("drops keys whose row has vanished — a reload must shrink the count", () => {
    expect(selectedInOrder(new Set(["b", "gone"]), rows)).toEqual(["b"]);
  });
});

describe("applyKeys", () => {
  it("adds and removes, returning a new Set", () => {
    const before = new Set(["a"]);
    const on = applyKeys(before, ["b", "c"], true);
    expect([...on].sort()).toEqual(["a", "b", "c"]);
    expect(on).not.toBe(before);
    expect([...applyKeys(on, ["a"], false)]).toEqual(["b", "c"]);
  });

  it("returns the SAME set when nothing moved, so React can skip the render", () => {
    const before = new Set(["a"]);
    expect(applyKeys(before, ["a"], true)).toBe(before);
    expect(applyKeys(before, ["zz"], false)).toBe(before);
  });
});

describe("selectAllState", () => {
  it("reports none / some / all over the visible rows", () => {
    const vis = ["a", "b", "c"];
    expect(selectAllState(new Set(), vis)).toBe("none");
    expect(selectAllState(new Set(["b"]), vis)).toBe("some");
    expect(selectAllState(new Set(["a", "b", "c"]), vis)).toBe("all");
  });

  it("ignores selections the filter is hiding", () => {
    // All of the two visible rows are picked, plus one filtered-out row: the
    // header box is "all", because it only ever speaks for what's on screen.
    expect(selectAllState(new Set(["a", "b", "hidden"]), ["a", "b"])).toBe("all");
  });

  it("is 'none' for an empty list rather than a ticked box over nothing", () => {
    expect(selectAllState(new Set(["x"]), [])).toBe("none");
  });
});

describe("rangeBetween", () => {
  const vis = ["a", "b", "c", "d", "e"];

  it("is inclusive and direction-agnostic", () => {
    expect(rangeBetween(vis, "b", "d")).toEqual(["b", "c", "d"]);
    expect(rangeBetween(vis, "d", "b")).toEqual(["b", "c", "d"]);
  });

  it("is just the row itself when the anchor is filtered away", () => {
    expect(rangeBetween(vis, "zz", "c")).toEqual(["c"]);
  });

  it("selects nothing when the target itself is not visible", () => {
    expect(rangeBetween(vis, "a", "zz")).toEqual([]);
  });
});

describe("previewList", () => {
  it("lists the names a destructive confirm is about to act on", () => {
    expect(previewList(["one", "two"])).toBe("  • one\n  • two");
  });

  it("caps the list so a 40-row confirm stays readable", () => {
    const lines = previewList(["a", "b", "c", "d"], 2).split("\n");
    expect(lines).toEqual(["  • a", "  • b", "  • …and 2 more"]);
  });
});
