import { describe, it, expect } from "vitest";
import {
  parseUnifiedDiff,
  buildSplitDiff,
  splitDiffByFile,
  type SplitRow,
} from "../lib/diff";

describe("parseUnifiedDiff", () => {
  it("classifies hunk / add / del / meta / ctx", () => {
    const kinds = parseUnifiedDiff(
      ["@@ -1,2 +1,2 @@", "+added", "-removed", "+++ b/f", "--- a/f", " ctx", "plain"].join("\n")
    ).map((l) => l.kind);
    expect(kinds).toEqual(["hunk", "add", "del", "meta", "meta", "ctx", "ctx"]);
  });
});

describe("buildSplitDiff", () => {
  it("pairs deletions with additions line-for-line and aligns context", () => {
    const rows = buildSplitDiff(
      [
        "diff --git a/f.txt b/f.txt",
        "@@ -1,2 +1,2 @@",
        "-old1",
        "-old2",
        "+new1",
        "+new2",
        " ctx",
      ].join("\n")
    );
    expect(rows[0]).toEqual({ type: "file", file: "f.txt" });
    expect(rows[1].type).toBe("hunk");
    expect(rows[2]).toEqual({
      type: "line",
      lnum: 1,
      ltext: "old1",
      ltype: "del",
      rnum: 1,
      rtext: "new1",
      rtype: "add",
    });
    expect(rows[3]).toEqual({
      type: "line",
      lnum: 2,
      ltext: "old2",
      ltype: "del",
      rnum: 2,
      rtext: "new2",
      rtype: "add",
    });
    // Context line carries the post-hunk line numbers on both sides.
    expect(rows[4]).toEqual({
      type: "line",
      lnum: 3,
      ltext: "ctx",
      ltype: "ctx",
      rnum: 3,
      rtext: "ctx",
      rtype: "ctx",
    });
  });

  it("spills unpaired additions into a blank left cell", () => {
    const rows = buildSplitDiff(["@@ -1,1 +1,2 @@", "-only", "+n1", "+n2"].join("\n"));
    const lines = rows.filter((r): r is Extract<SplitRow, { type: "line" }> => r.type === "line");
    expect(lines[0]).toMatchObject({ ltext: "only", ltype: "del", rtext: "n1", rtype: "add" });
    expect(lines[1]).toMatchObject({ lnum: "", ltype: "blank", rnum: 2, rtext: "n2", rtype: "add" });
  });

  it("uses the b/ path for a rename and records binary notes", () => {
    const rename = buildSplitDiff(
      [
        "diff --git a/old.txt b/new.txt",
        "similarity index 100%",
        "rename from old.txt",
        "rename to new.txt",
      ].join("\n")
    );
    expect(rename).toEqual([{ type: "file", file: "new.txt" }]);

    const bin = buildSplitDiff(
      ["diff --git a/img.png b/img.png", "Binary files a/img.png and b/img.png differ"].join("\n")
    );
    expect(bin[0]).toEqual({ type: "file", file: "img.png" });
    expect(bin[1]).toEqual({ type: "note", note: "Binary files a/img.png and b/img.png differ" });
  });
});

describe("splitDiffByFile", () => {
  it("splits a multi-file diff and drops the diff --git separators", () => {
    const segs = splitDiffByFile(
      [
        "diff --git a/one.txt b/one.txt",
        "@@ -1 +1 @@",
        "-a",
        "+b",
        "diff --git a/two.txt b/two.txt",
        "@@ -1 +1 @@",
        "-c",
        "+d",
      ].join("\n")
    );
    expect(segs.map((s) => s.file)).toEqual(["one.txt", "two.txt"]);
    expect(segs[0].lines).toEqual(["@@ -1 +1 @@", "-a", "+b"]);
    expect(segs[1].lines).toEqual(["@@ -1 +1 @@", "-c", "+d"]);
  });

  it("buckets leading lines with no diff header under an empty filename", () => {
    const segs = splitDiffByFile(["warning: noise", "more noise"].join("\n"));
    expect(segs).toEqual([{ file: "", lines: ["warning: noise", "more noise"] }]);
  });
});
