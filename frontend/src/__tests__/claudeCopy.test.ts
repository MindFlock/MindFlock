import { describe, expect, it } from "vitest";
import { cleanClaudeSelection, displayWidth } from "../lib/claudeCopy";

const COLS = 60;
/** Rebuild what Claude Code draws: a marker row, then rows lined up under the
 * text, each word-wrapped the way its renderer wraps — its text stops one
 * column short of the pane's edge. */
function draw(text: string, marker = "●", cols = COLS, gutter = 2): string {
  const rows: string[] = [];
  let row = marker + " ";
  for (const word of text.split(" ")) {
    const sep = row.trim() === marker ? "" : " ";
    if (displayWidth(row + sep + word) > cols - 1) {
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

describe("cleanClaudeSelection: wrapped text", () => {
  it("rejoins a wrapped message and drops the bullet and gutter", () => {
    const drawn = draw(SENTENCE);
    expect(drawn.split("\n").length).toBeGreaterThan(2); // really wrapped
    expect(cleanClaudeSelection(drawn, COLS)).toBe(SENTENCE);
  });

  it("works with the macOS bullet and with what you typed", () => {
    expect(cleanClaudeSelection(draw(SENTENCE, "⏺"), COLS)).toBe(SENTENCE);
    expect(cleanClaudeSelection(draw(SENTENCE, "❯"), COLS)).toBe(SENTENCE);
  });

  it("rejoins wrapped list items under their bullet, item by item", () => {
    const sel = [
      "● Two things:",
      "  - The first item is long enough that the renderer wraps it",
      "    onto a second row.",
      "  - Short item.",
    ].join("\n");
    expect(cleanClaudeSelection(sel, 64)).toBe(
      [
        "Two things:",
        "- The first item is long enough that the renderer wraps it onto a second row.",
        "- Short item.",
      ].join("\n")
    );
  });

  it("rejoins a path broken mid-token (at the column before the edge) without a space", () => {
    const path = "/tmp/claude-1000/-home-user-project/tasks/abcdef0123456789/output.txt";
    const cut = COLS - 1 - "● Wrote ".length;
    const first = "● Wrote " + path.slice(0, cut);
    expect(displayWidth(first)).toBe(COLS - 1);
    expect(cleanClaudeSelection(first + "\n  " + path.slice(cut), COLS)).toBe("Wrote " + path);
  });

  it("keeps the space when two words wrapped at the edge", () => {
    const first = "● The renderer was moved into a separate module named configuration";
    const cols = displayWidth(first) + 1;
    expect(cleanClaudeSelection(first + "\n  loader so the server can reuse it.", cols)).toBe(
      "The renderer was moved into a separate module named configuration loader so the server can reuse it."
    );
  });

  it("starts mid-row: the first row's offset counts toward fullness", () => {
    const drawn = draw(SENTENCE).split("\n");
    const offset = 12;
    const sel = [drawn[0].slice(offset), ...drawn.slice(1)].join("\n");
    expect(cleanClaudeSelection(sel, COLS, offset)).toBe(SENTENCE.slice(offset - 2));
  });

  it("rejoins a status notice that wraps back to column 0", () => {
    const head = '● Dynamic workflow "Last residuals: ledger hand-over, scoped"';
    const sel = head + "\ncompleted · 24m 50s";
    expect(cleanClaudeSelection(sel, displayWidth(head) + 1)).toBe(
      'Dynamic workflow "Last residuals: ledger hand-over, scoped" completed · 24m 50s'
    );
  });

  it("uses the widest row when the text was drawn wider than the terminal now is", () => {
    const wide = draw(SENTENCE, "●", 100);
    // shown in a 60-column terminal: the 99-wide rows prove the real width
    expect(cleanClaudeSelection(wide, COLS)).toBe(SENTENCE);
  });

  it("measures wide characters as two columns", () => {
    const wide = "漢字".repeat(14); // 56 columns
    // CJK has no spaces: a full row of it was split mid-run, so no space.
    expect(cleanClaudeSelection("● " + wide + "\n  next", COLS)).toBe(wide + "next");
    expect(cleanClaudeSelection("● " + "ab".repeat(14) + "\n  next", COLS)).toBe(
      "ab".repeat(14) + "\nnext"
    );
  });
});

describe("cleanClaudeSelection: real line breaks stay", () => {
  it("short lines, blank lines, separate paragraphs", () => {
    const sel = ["● First line.", "  Second line, short.", "", "  New paragraph."].join("\n");
    expect(cleanClaudeSelection(sel, COLS)).toBe(
      ["First line.", "Second line, short.", "", "New paragraph."].join("\n")
    );
  });

  it("never joins a short row to a column-0 row", () => {
    expect(cleanClaudeSelection("● done\nnext thing", COLS)).toBe("done\nnext thing");
  });

  it("a new list item or nested bullet never joins a full row", () => {
    const full = "  - The selection cleaner now rejoins the wrapped rows of i";
    expect(displayWidth(full)).toBe(COLS - 1);
    expect(cleanClaudeSelection(full + "\n    - Child item one", COLS)).toBe(
      "- The selection cleaner now rejoins the wrapped rows of i\n  - Child item one"
    );
    expect(cleanClaudeSelection(full + "\n  2. Second", COLS)).toBe(full.trim() + "\n2. Second");
  });

  it("consecutive tool-output rows stay separate", () => {
    const sel = [
      "  ⎿  appended 14 lines to release-flow-via-pr.md; now 212 lines",
      "  ⎿  Shell cwd was reset to /home/user/project",
    ].join("\n");
    expect(cleanClaudeSelection(sel, 64)).toBe(
      ["appended 14 lines to release-flow-via-pr.md; now 212 lines", "Shell cwd was reset to /home/user/project"].join("\n")
    );
  });

  it("tool output keeps its indent relative to the message", () => {
    const sel = ["● Ran the tests", "  ⎿  3 passed", "     in 0.2s"].join("\n");
    expect(cleanClaudeSelection(sel, COLS)).toBe(["Ran the tests", "   3 passed", "   in 0.2s"].join("\n"));
    expect(cleanClaudeSelection(["  ⎿  3 passed", "     in 0.2s"].join("\n"), COLS)).toBe("3 passed\nin 0.2s");
  });

  it("diff lines in tool output stay line for line", () => {
    const sel = [
      "     71 -        assert result.status_code == 200, result.text",
      "     72 +        assert result.status_code == 201",
    ].join("\n");
    expect(cleanClaudeSelection(sel, 64).split("\n")).toHaveLength(2);
  });

  it("tables are copied row by row", () => {
    const table = [
      "  ┌──────────┬──────────┬─────────────────────────────────────┐",
      "  │ Option   │ Latency  │ Notes                               │",
      "  ├──────────┼──────────┼─────────────────────────────────────┤",
      "  │ Redis    │ 1 ms     │ in memory                           │",
      "  └──────────┴──────────┴─────────────────────────────────────┘",
    ];
    const sel = ["● Here is the comparison:", "", ...table].join("\n");
    expect(cleanClaudeSelection(sel, 66)).toBe(
      ["Here is the comparison:", "", ...table.map((r) => r.slice(2))].join("\n")
    );
  });

  it("a drag over the input box and footer doesn't collapse into one line", () => {
    const rule = "─".repeat(COLS);
    const sel = [
      "  Pushed the branch and opened the pull request for review",
      rule,
      "❯ ",
      rule,
      "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    ].join("\n");
    const out = cleanClaudeSelection(sel, COLS).split("\n");
    expect(out[0]).toBe("Pushed the branch and opened the pull request for review");
    expect(out).toHaveLength(5);
  });

  it("code lines near the edge are not merged", () => {
    const sel = [
      "  const rows = raw.map(unmark).filter((row) => row.trim().length > 0);",
      "  const lines: string[] = [];",
      "  if (rows.length === 0) {",
      "    return text;",
      "  }",
    ].join("\n");
    expect(cleanClaudeSelection(sel, 74).split("\n")).toHaveLength(5);
  });

  it("a line after a lone URL stays its own line", () => {
    const url = "https://github.com/acme/project/actions/runs/12345678901/job/3456789";
    const sel = ["● CI failed. The log is here:", "  " + url, "  The failing test is test_launch_parity."].join("\n");
    const out = cleanClaudeSelection(sel, 80).split("\n");
    expect(out[out.length - 1]).toBe("The failing test is test_launch_parity.");
  });

  it("never joins a row Claude Code truncated with an ellipsis", () => {
    const first = "❯ " + "x".repeat(COLS - 5) + " …";
    expect(cleanClaudeSelection(first + "\n  second line of the prompt", COLS)).toBe(
      first.slice(2) + "\nsecond line of the prompt"
    );
  });

  it("trims trailing spaces and leaves empty input alone", () => {
    expect(cleanClaudeSelection("● hi   \n  there  ", COLS)).toBe("hi\nthere");
    expect(cleanClaudeSelection("", COLS)).toBe("");
  });
});
