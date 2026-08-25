import { describe, it, expect } from "vitest";
import { csvRecords, promptsFromFile } from "../components/grid/queueImport";

describe("csvRecords", () => {
  it("splits rows and cells", () => {
    expect(csvRecords("a,b\nc,d\n")).toEqual([
      ["a", "b"],
      ["c", "d"],
    ]);
  });

  it("honors quoted fields with commas, newlines, and doubled quotes", () => {
    const text = '"fix the bug, then test",high\n"say ""done""\nwhen finished",low';
    expect(csvRecords(text)).toEqual([
      ["fix the bug, then test", "high"],
      ['say "done"\nwhen finished', "low"],
    ]);
  });

  it("handles CRLF and a missing trailing newline", () => {
    expect(csvRecords("a\r\nb")).toEqual([["a"], ["b"]]);
  });
});

describe("promptsFromFile", () => {
  it("queues one prompt per CSV row, skipping blank rows", () => {
    expect(promptsFromFile("q.csv", "first\n\nsecond\n")).toEqual(["first", "second"]);
  });

  it("collapses multi-column rows to their non-empty cells", () => {
    expect(promptsFromFile("q.csv", "fix login,,urgent\nwrite docs,\n")).toEqual([
      "fix login urgent",
      "write docs",
    ]);
  });

  it("skips an obvious header row, but only an obvious one", () => {
    expect(promptsFromFile("q.csv", "prompt\nfix login\n")).toEqual(["fix login"]);
    expect(promptsFromFile("q.csv", "Prompt,Priority\nfix login,high\n")).toEqual([
      "fix login high",
    ]);
    // A first row that reads like a real prompt is kept.
    expect(promptsFromFile("q.csv", "fix login\nwrite docs\n")).toEqual([
      "fix login",
      "write docs",
    ]);
  });

  it("treats a non-CSV file as one prompt per line, without CSV splitting", () => {
    expect(promptsFromFile("notes.txt", "fix a, then b\n\nship it\r\n")).toEqual([
      "fix a, then b",
      "ship it",
    ]);
  });
});
