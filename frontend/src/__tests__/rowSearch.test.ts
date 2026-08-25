/** The Ctrl+F row filter behind the Recently-closed and Workspaces dialogs. */

import { describe, it, expect } from "vitest";
import { matchesTokens, searchTokens } from "../lib/rowSearch";

describe("searchTokens", () => {
  it("is empty for a blank or whitespace-only query", () => {
    expect(searchTokens("")).toEqual([]);
    expect(searchTokens("   ")).toEqual([]);
  });

  it("lowercases and splits on runs of whitespace", () => {
    expect(searchTokens("  Shortcut   21018 ")).toEqual(["shortcut", "21018"]);
  });
});

describe("matchesTokens", () => {
  const row = ["feature/shortcut-21018-path-expansion", "shortcut-21018", "/home/e/ws/sc-21018"];

  it("matches everything when the query is empty", () => {
    expect(matchesTokens(row, searchTokens(""))).toBe(true);
  });

  it("matches a plain substring, case-insensitively", () => {
    expect(matchesTokens(row, searchTokens("PATH-EXPANSION"))).toBe(true);
    expect(matchesTokens(row, searchTokens("cloudflare"))).toBe(false);
  });

  it("requires every token, so extra words narrow instead of widen", () => {
    expect(matchesTokens(row, searchTokens("shortcut expansion"))).toBe(true);
    expect(matchesTokens(row, searchTokens("shortcut cloudflare"))).toBe(false);
  });

  it("ignores separators between tokens — dashes need not be typed", () => {
    expect(matchesTokens(row, searchTokens("shortcut 210"))).toBe(true);
  });

  it("searches every field, including ones the row only shows on hover", () => {
    expect(matchesTokens(row, searchTokens("/home/e/ws"))).toBe(true);
  });

  it("drops nullish fields instead of matching on 'undefined'", () => {
    expect(matchesTokens(["base", null, undefined], searchTokens("undefined"))).toBe(false);
    expect(matchesTokens(["base", null, undefined], searchTokens("base"))).toBe(true);
  });

  it("does not let one field's tail run into the next (fields are space-joined)", () => {
    expect(matchesTokens(["abc", "def"], searchTokens("abcdef"))).toBe(false);
  });
});
