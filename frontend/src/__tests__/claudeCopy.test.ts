import { describe, expect, it } from "vitest";
import { cleanClaudeSelection, displayWidth } from "../lib/claudeCopy";

const COLS = 60;
/** Rebuild what Claude Code draws: a marker row, then rows lined up under the
 * text, each word-wrapped at ``cols`` the way its renderer wraps. */
function draw(text: string, marker = "●", cols = COLS, gutter = 2): string {
  const rows: string[] = [];
  let row = marker + " ";
  for (const word of text.split(" ")) {
    const sep = row.trim() === marker ? "" : " ";
    if (displayWidth(row + sep + word) > cols) {
      rows.push(row);
      row = " ".repeat(gutter) + word;
    } else {
      row += sep + word;
    }
  }
  rows.push(row);
  return rows.join("\n");
}

const SENTENCE =
  "The file list came back empty, so pytest ran the whole suite instead: 10,757 passed, 3 failed. Checking that the 3 are the known ones.";

describe("cleanClaudeSelection", () => {
  it("rejoins a wrapped message and drops the bullet and gutter", () => {
    const drawn = draw(SENTENCE);
    expect(drawn.split("\n").length).toBeGreaterThan(2); // really wrapped
    expect(cleanClaudeSelection(drawn, COLS)).toBe(SENTENCE);
  });

  it("works with the macOS bullet and with what you typed", () => {
    expect(cleanClaudeSelection(draw(SENTENCE, "⏺"), COLS)).toBe(SENTENCE);
    expect(cleanClaudeSelection(draw(SENTENCE, "❯"), COLS)).toBe(SENTENCE);
  });

  it("keeps real line breaks: short lines, blank lines, separate paragraphs", () => {
    const sel = ["● First line.", "  Second line, short.", "", "  New paragraph."].join("\n");
    expect(cleanClaudeSelection(sel, COLS)).toBe(
      ["First line.", "Second line, short.", "", "New paragraph."].join("\n")
    );
  });

  it("rejoins wrapped list items under their bullet, item by item", () => {
    const sel = [
      "● Two things:",
      "  - The first item is long enough that the renderer wraps it",
      "    onto a second row.",
      "  - Short item.",
    ].join("\n");
    expect(cleanClaudeSelection(sel, COLS)).toBe(
      [
        "Two things:",
        "- The first item is long enough that the renderer wraps it onto a second row.",
        "- Short item.",
      ].join("\n")
    );
  });

  it("rejoins a path broken mid-token without inserting a space", () => {
    const path = "/tmp/claude-1000/-home-user-project/tasks/abcdef0123456789/output.txt";
    const first = "● Wrote " + path.slice(0, COLS - "● Wrote ".length);
    expect(displayWidth(first)).toBe(COLS);
    const sel = first + "\n  " + path.slice(COLS - "● Wrote ".length);
    expect(cleanClaudeSelection(sel, COLS)).toBe("Wrote " + path);
  });

  it("starts mid-row: the first row's offset counts toward fullness", () => {
    const drawn = draw(SENTENCE).split("\n");
    const offset = 12;
    const sel = [drawn[0].slice(offset), ...drawn.slice(1)].join("\n");
    expect(cleanClaudeSelection(sel, COLS, offset)).toBe(SENTENCE.slice(offset - 2));
  });

  it("rejoins a status notice that wraps back to column 0", () => {
    const head = '● Dynamic workflow "Last residuals: ledger hand-over, scoped"';
    expect(displayWidth(head)).toBeGreaterThan(COLS - 10);
    const sel = head + "\ncompleted · 24m 50s";
    expect(cleanClaudeSelection(sel, COLS)).toBe(
      'Dynamic workflow "Last residuals: ledger hand-over, scoped" completed · 24m 50s'
    );
  });

  it("never joins a short row to a column-0 row", () => {
    expect(cleanClaudeSelection("● done\nnext thing", COLS)).toBe("done\nnext thing");
  });

  it("keeps tool output separate and only as indented as it is relative to the message", () => {
    const sel = ["● Ran the tests", "  ⎿  3 passed", "     in 0.2s"].join("\n");
    expect(cleanClaudeSelection(sel, COLS)).toBe(
      ["Ran the tests", "   3 passed", "   in 0.2s"].join("\n")
    );
    // Selecting only the output drops all of its indent.
    expect(cleanClaudeSelection(["  ⎿  3 passed", "     in 0.2s"].join("\n"), COLS)).toBe(
      ["3 passed", "in 0.2s"].join("\n")
    );
  });

  it("measures wide characters as two columns", () => {
    const wide = "漢字".repeat(14); // 56 columns
    const sel = "● " + wide + "\n  next";
    // 2 + 56 = 58; 58 + 1 + 4 > 60 → it was a wrap
    expect(cleanClaudeSelection(sel, COLS)).toBe(wide + " next");
    // the same text measured as narrow would not have wrapped
    expect(cleanClaudeSelection("● " + "ab".repeat(14) + "\n  next", COLS)).toBe(
      "ab".repeat(14) + "\nnext"
    );
  });

  it("never joins a row Claude Code truncated with an ellipsis", () => {
    const first = "❯ " + "x".repeat(COLS - 4) + " …";
    expect(displayWidth(first)).toBe(COLS);
    expect(cleanClaudeSelection(first + "\n  second line of the prompt", COLS)).toBe(
      first.slice(2) + "\nsecond line of the prompt"
    );
  });

  it("trims trailing spaces and leaves empty input alone", () => {
    expect(cleanClaudeSelection("● hi   \n  there  ", COLS)).toBe("hi\nthere");
    expect(cleanClaudeSelection("", COLS)).toBe("");
  });
});
