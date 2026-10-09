/** Copying out of a Claude Code pane without its layout.
 *
 * Claude Code draws its own screen: a message starts with "⏺ " ("●" off macOS,
 * "❯" for what you typed), tool output with "⎿ ", every row after the first
 * is indented to line up, and it wraps long text ITSELF — each wrapped row is
 * a separate terminal row with a real line break (tmux `capture-pane -J`
 * can't rejoin them), not a soft wrap xterm could join. So a drag-copy of one
 * sentence came out as
 *
 *     ⏺ The file list came back empty, so pytest ran … that the 3 are the
 *       known sandbox ones:
 *
 * with the bullet, a line break in the middle of the sentence, and the
 * two-space gutter in front of every following row.
 *
 * {@link cleanClaudeSelection} undoes that. Markers become indentation; a row
 * is rejoined with the next one only when the next one's first word could not
 * have fit on it — exactly when Claude Code would have wrapped — and nothing
 * suggests a real line break (a new list item, a marker, a table or rule row,
 * a code-looking line end, a row cut short with "…"); the common indentation
 * is removed. Plain text can't always tell a wrapped sentence from two lines
 * that happen to fill the row, so every rule errs toward KEEPING a break. */

const BULLET = /^(\s*)(?:[-*•]|\d{1,3}[.)])\s+/;
/** The row that starts a message (Claude's reply, or what you typed). */
const MESSAGE = /^\s*[⏺●❯](?:\s|$)/;
/** Any of Claude Code's row markers. */
const MARKER = /^\s*[⏺●❯⎿](?:\s|$)/;
/** Table borders and cells, rules, the input box and its footer (box
 * drawing, block elements, the ⏵ arrows): never part of a wrapped sentence. */
const CHROME = /^\s*[─-▟⏵]/;
/** A numbered diff line in tool output ("  71 -   foo"). */
const DIFF_LINE = /^\s*\d+\s+[-+ ]\s/;
/** Line ends / starts that mark code rather than a sentence wrapped between
 * two words. */
const CODE_END = /(?:[;{}[(\\]|=>)$/;
const CODE_START = /^(?:[)}\]]|\/\/|#|\.\w)/;

/** Terminal column width of one code point: 2 for East Asian wide/fullwidth
 * characters and emoji, else 1 — close enough to xterm's own table for
 * measuring how full a row is. */
function cpWidth(cp: number): number {
  if (
    (cp >= 0x1100 && cp <= 0x115f) ||
    (cp >= 0x2e80 && cp <= 0xa4cf && cp !== 0x303f) ||
    (cp >= 0xac00 && cp <= 0xd7a3) ||
    (cp >= 0xf900 && cp <= 0xfaff) ||
    (cp >= 0xfe30 && cp <= 0xfe4f) ||
    (cp >= 0xff00 && cp <= 0xff60) ||
    (cp >= 0xffe0 && cp <= 0xffe6) ||
    (cp >= 0x1f300 && cp <= 0x1faff) ||
    (cp >= 0x20000 && cp <= 0x3fffd)
  ) {
    return 2;
  }
  return 1;
}

export function displayWidth(s: string): number {
  let w = 0;
  for (const ch of s) w += cpWidth(ch.codePointAt(0) || 0);
  return w;
}

function indentOf(row: string): number {
  return row.length - row.trimStart().length;
}

/** Where a row's TEXT starts: after its indent and, for a list item, after
 * the bullet — the column Claude Code lines that item's wrapped rows up to. */
function textIndent(row: string): number {
  const m = BULLET.exec(row);
  return m ? m[0].length : indentOf(row);
}

/** The row with Claude Code's markers turned into the indentation they
 * occupy ("⏺ " / "⎿ " are two columns each), so every row of one block
 * lines up the same way. */
function unmark(row: string): string {
  return row.replace(/^(\s*)[⏺●❯⎿](?:\s|$)/, (_m, lead: string) => lead + "  ");
}

/**
 * Rejoin Claude Code's wrapped rows and drop its gutter.
 *
 * @param text      the terminal selection (rows separated by "\n")
 * @param cols      the width Claude Code drew at (the terminal's columns)
 * @param startCol  the column the selection starts at on its first row (a
 *                  selection that starts mid-row is that much fuller than
 *                  its text)
 */
export function cleanClaudeSelection(text: string, cols: number, startCol = 0): string {
  if (!text || cols <= 0) return text;
  const raw = text.replace(/\r/g, "").split("\n").map((r) => r.replace(/\s+$/, ""));
  const rows = raw.map(unmark);
  const widths = rows.map((r, i) => displayWidth(r) + (i === 0 ? startCol : 0));
  // A row wider than the terminal proves it was drawn at a wider size: wrap
  // at the wider of the two. Claude Code's text stops one column short of
  // the edge, so that last column is never usable.
  const usable = Math.max(cols, ...widths) - 1;

  const lines: string[] = [];
  const flush = new Set<number>(); // lines that start a message: flush left
  let cur = "";
  let curRow = 0; // index of the last row joined into cur
  let contIndent = 0; // where wrapped rows of the current line start

  /** Whether row ``i`` continues the line being built (Claude Code wrapped
   * it) rather than starting a new one. */
  const joins = (i: number): boolean => {
    const prev = rows[curRow];
    const row = rows[i];
    if (!cur.trim() || !row.trim()) return false;
    // Rows that always start their own line, or never belong to a sentence.
    if (MARKER.test(raw[i]) || BULLET.test(row) || DIFF_LINE.test(row)) return false;
    if (CHROME.test(row) || CHROME.test(prev)) return false;
    if (cur.endsWith("…")) return false; // cut short, not wrapped
    // Wrapped rows line up under the text — except Claude Code's status
    // notices, which wrap back to column 0.
    const ind = indentOf(row);
    if (ind !== contIndent && ind !== 0) return false;
    const next = row.trim();
    const fw = displayWidth(next.split(/\s+/, 1)[0]);
    // A token wider than a whole row is no evidence of a wrap.
    if (fw >= usable - contIndent) return false;
    // Code lines end and start in ways a sentence wrapped between words
    // doesn't.
    if (CODE_END.test(prev) || CODE_START.test(next)) return false;
    // A lone token (a URL, a path) that doesn't reach the edge was placed on
    // its own row: it says nothing about whether the next word would fit. (A
    // row ending in a wide character is full one column early: the next one
    // didn't fit.)
    const last = [...prev].pop() || "";
    const full =
      widths[curRow] >= usable ||
      (widths[curRow] === usable - 1 && displayWidth(last) === 2);
    if (!prev.trim().includes(" ") && !full) return false;
    return widths[curRow] + 1 + fw > usable;
  };

  rows.forEach((row, i) => {
    if (i > 0 && joins(i)) {
      const next = row.trim();
      const firstWord = next.split(/\s+/, 1)[0];
      const lastToken = cur.slice(cur.lastIndexOf(" ") + 1).trimStart();
      // Ink/wrap-ansi splits a word only when it is longer than a whole row:
      // two halves that together couldn't fit on one row were one token.
      const midToken = displayWidth(lastToken) + displayWidth(firstWord) > usable - contIndent;
      cur += (midToken ? "" : " ") + next;
      curRow = i;
      return;
    }
    if (i > 0) lines.push(cur);
    if (MESSAGE.test(raw[i])) flush.add(lines.length);
    cur = row;
    curRow = i;
    // A selection that starts mid-row has no gutter on its first row: its
    // wrapped rows line up wherever the second row does.
    contIndent =
      i === 0 && startCol > 0 && rows.length > 1 ? indentOf(rows[1]) : textIndent(row);
  });
  lines.push(cur);

  // Drop the indentation every line shares (the gutter), keep the rest. A
  // first line that starts mid-row has no gutter to share.
  // Table borders, rules and the input box sit at column 0 and don't share it.
  const counted = startCol > 0 ? lines.slice(1) : lines;
  const indents = counted.filter((l) => l.trim() && !CHROME.test(l)).map(indentOf);
  const common = indents.length ? Math.min(...indents) : 0;
  return lines
    .map((l, i) => {
      if (!l.trim()) return "";
      if (flush.has(i) || (i === 0 && startCol > 0)) return l.trimStart();
      return l.slice(Math.min(common, indentOf(l)));
    })
    .join("\n");
}
