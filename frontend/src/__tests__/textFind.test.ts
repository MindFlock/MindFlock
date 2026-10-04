import { describe, expect, it } from "vitest";
import { findAll, pickStart, stepMatch } from "../lib/textFind";

describe("findAll", () => {
  it("finds every case-insensitive occurrence", () => {
    expect(findAll("Error: x\nerror again\nERROR", "error")).toEqual([0, 9, 21]);
  });

  it("treats the query literally, not as a regex", () => {
    expect(findAll("a.b axb (a.b)", "a.b")).toEqual([0, 9]);
    expect(findAll("x[1] x1", "[1]")).toEqual([1]);
  });

  it("does not overlap hits", () => {
    expect(findAll("aaaa", "aa")).toEqual([0, 2]);
  });

  it("keeps offsets exact past characters whose lower case is longer", () => {
    // "İ".toLowerCase() is two code units; lower-case + indexOf would skew.
    const text = "İstanbul foo";
    expect(findAll(text, "foo")).toEqual([text.indexOf("foo")]);
  });

  it("caps the number of hits", () => {
    expect(findAll("a".repeat(100), "a", 10)).toHaveLength(10);
  });

  it("returns nothing for an empty query or text", () => {
    expect(findAll("abc", "")).toEqual([]);
    expect(findAll("", "a")).toEqual([]);
  });
});

describe("pickStart", () => {
  it("lands on the last hit at or above the anchor", () => {
    expect(pickStart([5, 20, 40], 30)).toBe(1);
    expect(pickStart([5, 20, 40], 20)).toBe(1);
    expect(pickStart([5, 20, 40], 1000)).toBe(2);
  });

  it("falls back to the first hit below when none is above", () => {
    expect(pickStart([5, 20, 40], 2)).toBe(0);
  });

  it("is -1 with no hits", () => {
    expect(pickStart([], 10)).toBe(-1);
  });
});

describe("stepMatch", () => {
  it("wraps at both ends", () => {
    expect(stepMatch(2, 1, 3)).toBe(0);
    expect(stepMatch(0, -1, 3)).toBe(2);
    expect(stepMatch(1, 1, 3)).toBe(2);
  });

  it("starts from an end when nothing is current", () => {
    expect(stepMatch(-1, 1, 3)).toBe(0);
    expect(stepMatch(-1, -1, 3)).toBe(2);
    expect(stepMatch(0, 1, 0)).toBe(-1);
  });
});
