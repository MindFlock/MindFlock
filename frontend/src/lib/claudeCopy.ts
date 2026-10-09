/** Copying out of a Claude Code pane without its layout.
 *
 * Claude Code draws its own screen: a message starts with "⏺ " ("●" off macOS,
 * "❯" for what you typed), tool output
 * with "⎿ ", every row after the first is indented to line up, and it wraps
 * long text ITSELF — each wrapped row is a separate terminal row with a real
 * line break (tmux `capture-pane -J` can't rejoin them), not a soft wrap
 * xterm could join. So a drag-copy of one sentence came out as
 *
 *     ⏺ The file list came back empty, so pytest ran … that the 3 are the
 *       known sandbox ones:
 *
 * with the bullet, a line break in the middle of the sentence, and the
 * two-space gutter in front of every following row.
 *
 * {@link cleanClaudeSelection} undoes that: markers become indentation, a row
 * is rejoined with the next one only when the next one's first word could not
 * have fit on it (exactly when Claude Code would have wrapped), and the common
 * indentation is removed. Real line breaks — short lines, list items, blank
 * lines, code — stay. */

const BULLET = /^(\s*)(?:[-*•]|\d{1,3}[.)])\s+/;
/** The row that starts a message (Claude's reply, or what you typed). */
const MESSAGE = /^\s*[⏺●❯]\s/;

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
  return row.replace(/^(\s*)[⏺●❯⎿]\s/, (_m, lead: string) => lead + "  ");
}

/** Whether the selection looks like Claude Code's layout at all (the
 * markers); the caller also knows the pane's provider. */
export function hasClaudeMarkers(text: string): boolean {
  return /^\s*[⏺●❯⎿]\s/m.test(text);
}

/**
 * Rejoin Claude Code's wrapped rows and drop its gutter.
 *
 * @param text      the terminal selection (rows separated by "\n")
 * @param cols      the terminal's width in columns
 * @param startCol  the column the selection starts at on its first row (a
 *                  selection that starts mid-row is that much fuller than
 *                  its text)
 */
export function cleanClaudeSelection(text: string, cols: number, startCol = 0): string {
  if (!text || cols <= 0) return text;
  const raw = text.replace(/\r/g, "").split("\n").map((r) => r.replace(/\s+$/, ""));
  const rows = raw.map(unmark);
  const lines: string[] = [];
  const flush = new Set<number>(); // lines that start a message: always flush left
  let cur = "";
  let curRowWidth = 0; // the full on-screen width of the LAST row joined into cur
  let contIndent = 0; // where wrapped rows of the current line start
  rows.forEach((row, i) => {
    const width = displayWidth(row) + (i === 0 ? startCol : 0);
    if (i > 0 && cur.trim() && row.trim()) {
      const next = row.trim();
      const firstWord = next.split(/\s+/, 1)[0];
      // Wrapped rows line up under the text — except Claude Code's status
      // notices, which wrap back to column 0. Either way, only a row whose
      // first word could not have fit on the row above is a wrap.
      const lined = indentOf(row) === contIndent || indentOf(row) === 0;
      // A row Claude Code cut short ("…") was truncated, not wrapped: what
      // follows is the next line, however full the row looks.
      const truncated = cur.endsWith("…");
      const wrapped = lined && !truncated && curRowWidth + 1 + displayWidth(firstWord) > cols;
      if (wrapped) {
        // A row filled to the last column that ends inside a long token (a
        // path, a URL) was broken mid-token: no space between the halves.
        const lastToken = cur.slice(cur.lastIndexOf(" ") + 1);
        const midToken = curRowWidth >= cols && lastToken.length >= 12;
        cur += (midToken ? "" : " ") + next;
        curRowWidth = width;
        return;
      }
    }
    if (i > 0) lines.push(cur);
    if (MESSAGE.test(raw[i])) flush.add(lines.length);
    cur = row;
    curRowWidth = width;
    // A selection that starts mid-row has no gutter on its first row: its
    // wrapped rows line up wherever the second row does.
    contIndent =
      i === 0 && startCol > 0 && rows.length > 1 ? indentOf(rows[1]) : textIndent(row);
  });
  lines.push(cur);
  // Drop the indentation every line shares (the gutter), keep the rest. A
  // first line that starts mid-row has no gutter to share.
  const counted = startCol > 0 ? lines.slice(1) : lines;
  const indents = counted.filter((l) => l.trim()).map(indentOf);
  const common = indents.length ? Math.min(...indents) : 0;
  return lines
    .map((l, i) => {
      if (!l.trim()) return "";
      if (flush.has(i) || (i === 0 && startCol > 0)) return l.trimStart();
      return l.slice(Math.min(common, indentOf(l)));
    })
    .join("\n");
}
