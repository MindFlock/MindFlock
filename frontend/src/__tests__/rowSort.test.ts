/** Sorting behind the Recently-closed and Workspaces dialogs. */

import { describe, it, expect, beforeAll, beforeEach } from "vitest";
import { compareText, loadSortPref, saveSortPref, sortRows } from "../lib/rowSort";

const names = (rows: { n: string }[]) => rows.map((r) => r.n);

describe("compareText", () => {
  it("orders embedded numbers naturally, not lexically", () => {
    expect(compareText("shortcut-9", "shortcut-21018")).toBeLessThan(0);
  });

  it("ignores case so a capitalised branch doesn't jump the list", () => {
    expect(compareText("Alpha", "alpha")).toBe(0);
    expect(compareText("Beta", "alpha")).toBeGreaterThan(0);
  });
});

describe("sortRows", () => {
  const rows = [{ n: "b" }, { n: "C" }, { n: "a" }];

  it("sorts text both ways", () => {
    expect(names(sortRows(rows, (r) => r.n, "asc"))).toEqual(["a", "b", "C"]);
    expect(names(sortRows(rows, (r) => r.n, "desc"))).toEqual(["C", "b", "a"]);
  });

  it("sorts numbers both ways", () => {
    const sized = [{ n: "m", v: 10 }, { n: "s", v: 1 }, { n: "l", v: 100 }];
    expect(names(sortRows(sized, (r) => r.v, "asc"))).toEqual(["s", "m", "l"]);
    expect(names(sortRows(sized, (r) => r.v, "desc"))).toEqual(["l", "m", "s"]);
  });

  it("parks missing values at the BOTTOM in both directions", () => {
    // "Newest first" must not lead with a block of unknown dates, and neither
    // must "oldest first" — a null is "we don't know", not "very old".
    const mixed = [{ n: "none", v: null }, { n: "old", v: 1 }, { n: "new", v: 9 }];
    expect(names(sortRows(mixed, (r) => r.v, "desc"))).toEqual(["new", "old", "none"]);
    expect(names(sortRows(mixed, (r) => r.v, "asc"))).toEqual(["old", "new", "none"]);
  });

  it("treats an empty string as missing too", () => {
    const mixed = [{ n: "blank", v: "" }, { n: "named", v: "zz" }];
    expect(names(sortRows(mixed, (r) => r.v, "asc"))).toEqual(["named", "blank"]);
  });

  it("is stable — ties keep the server's order", () => {
    const dup = [{ n: "first", v: 1 }, { n: "second", v: 1 }, { n: "third", v: 1 }];
    expect(names(sortRows(dup, (r) => r.v, "desc"))).toEqual(["first", "second", "third"]);
  });

  it("does not mutate the input", () => {
    const input = [{ n: "b" }, { n: "a" }];
    sortRows(input, (r) => r.n, "asc");
    expect(names(input)).toEqual(["b", "a"]);
  });
});

describe("sort preference persistence", () => {
  const KEY = "mf_sort_test";
  const fallback = { key: "name", dir: "asc" } as const;

  // These tests run in the node environment (no DOM), so localStorage is stubbed
  // here rather than in the shared setup. Worth stubbing rather than skipping:
  // load/save swallow every storage error, so an untested version of them can
  // silently never persist anything.
  beforeAll(() => {
    const mem = new Map<string, string>();
    (globalThis as Record<string, unknown>).localStorage = {
      getItem: (k: string) => (mem.has(k) ? mem.get(k)! : null),
      setItem: (k: string, v: string) => void mem.set(k, v),
      removeItem: (k: string) => void mem.delete(k),
      clear: () => mem.clear(),
    };
  });

  beforeEach(() => localStorage.clear());

  it("round-trips a choice", () => {
    saveSortPref(KEY, { key: "size", dir: "desc" });
    expect(loadSortPref(KEY, fallback)).toEqual({ key: "size", dir: "desc" });
  });

  it("falls back when unset, malformed, or a bad direction", () => {
    expect(loadSortPref(KEY, fallback)).toEqual(fallback);
    localStorage.setItem(KEY, "{not json");
    expect(loadSortPref(KEY, fallback)).toEqual(fallback);
    localStorage.setItem(KEY, JSON.stringify({ key: "size", dir: "sideways" }));
    expect(loadSortPref(KEY, fallback)).toEqual(fallback);
  });
});
