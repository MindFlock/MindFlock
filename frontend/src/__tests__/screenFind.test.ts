import { describe, expect, it } from "vitest";
import { buildPattern, rowHits, type FindOptions, type RowCell } from "../lib/screenFind";

const row = (s: string): RowCell[] => Array.from(s).map((ch, x) => ({ ch: ch === " " ? "" : ch, x, w: 1 }));
const OFF: FindOptions = { case: false, word: false, regex: false };
const pat = (t: string, o: Partial<FindOptions> = {}) => {
  const p = buildPattern(t, { ...OFF, ...o });
  if (p instanceof Error) throw p;
  return p;
};
const spans = (s: string, t: string, o: Partial<FindOptions> = {}) =>
  rowHits(row(s), pat(t, o)).map((h) => [h.start, h.len]);

// The same cases as the server's tests/unit/test_find_query.py: painting
// must agree with what the server counts.
describe("buildPattern + rowHits (parity with find_query)", () => {
  it("is literal and case-insensitive by default", () => {
    expect(spans("an error occurred; errors pile up", "error")).toEqual([
      [3, 5],
      [19, 5],
    ]);
    expect(spans("a.b axb", "a.b")).toEqual([[0, 3]]);
  });

  it("matches case", () => {
    expect(spans("Error error", "Error", { case: true })).toEqual([[0, 5]]);
  });

  it("matches whole words only", () => {
    expect(spans("an error occurred; errors pile up", "error", { word: true })).toEqual([[3, 5]]);
    expect(spans("terror", "error", { word: true })).toEqual([]);
    expect(spans("web-ui.md updated", "web-ui", { word: true })).toEqual([[0, 6]]);
    expect(spans("web-ui.md updated", "ui", { word: true })).toEqual([[4, 2]]);
  });

  it("takes regular expressions", () => {
    expect(spans("timeout after 30s", "\\d+s", { regex: true })).toEqual([[14, 3]]);
    expect(buildPattern("err(", { ...OFF, regex: true })).toBeInstanceOf(Error);
    expect((buildPattern("err(", { ...OFF, regex: true }) as Error).message).toMatch(/invalid regular expression/);
  });

  it("skips empty matches", () => {
    expect(spans("abc", "x*", { regex: true })).toEqual([]);
  });
});

describe("rowHits positions", () => {
  it("paints the cell span of each hit", () => {
    expect(rowHits(row("a needle b needle"), pat("needle"))).toEqual([
      { start: 2, len: 6, x: 2, width: 6 },
      { start: 11, len: 6, x: 11, width: 6 },
    ]);
  });

  it("matches across blank cells as spaces", () => {
    expect(spans("web ui", "web ui")).toEqual([[0, 6]]);
  });

  it("counts a wide character as one code point but paints two cells", () => {
    // "日x needle": 日 takes cells 0-1, x is cell 2, needle starts at cell 4.
    const cells: RowCell[] = [
      { ch: "日", x: 0, w: 2 },
      { ch: "x", x: 2, w: 1 },
      { ch: "", x: 3, w: 1 },
      ...Array.from("needle").map((ch, i) => ({ ch, x: 4 + i, w: 1 })),
    ];
    expect(rowHits(cells, pat("needle"))).toEqual([{ start: 3, len: 6, x: 4, width: 6 }]);
    expect(rowHits(cells, pat("日x"))).toEqual([{ start: 0, len: 2, x: 0, width: 3 }]);
  });

  it("an empty term is an error, not a pattern", () => {
    expect(buildPattern("", OFF)).toBeInstanceOf(Error);
  });
});
