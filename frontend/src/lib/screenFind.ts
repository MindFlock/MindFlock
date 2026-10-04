/** Matching the find query against ONE row of the live terminal screen, for
 * the pane find highlights (FindHighlights).
 *
 * The server decides what the hits ARE (backend core/find_query — match
 * case, whole word, regex, proximity) and where the current one sits; this
 * paints every visible occurrence of the query's terms with the SAME rules,
 * so buildPattern must stay in step with find_query.compile_term: literal
 * text fully escaped, whole word as a lookaround on word characters (not \b,
 * which misbehaves at a term's own punctuation), case-insensitive unless
 * "match case".
 *
 * Pure so it's testable without xterm. Positions are counted in code points
 * over the row's cells — the way the server's text indexes them — while each
 * hit also carries the cell span to paint, which is where a wide character
 * (CJK, emoji: one code point, two cells) differs. */

export interface FindOptions {
  case: boolean;
  word: boolean;
  regex: boolean;
}

export interface RowCell {
  /** The cell's characters ("" for a blank cell). */
  ch: string;
  /** Cell column. */
  x: number;
  /** Cells wide (2 for a wide character). */
  w: number;
}

export interface RowHit {
  /** Code-point index of the hit in the row's text. */
  start: number;
  /** Its length in code points. */
  len: number;
  /** First cell and cell width to paint. */
  x: number;
  width: number;
}

/** The pattern for one term, or an Error with a readable message. */
export function buildPattern(text: string, opts: FindOptions): RegExp | Error {
  if (!text) return new Error("empty");
  let src = opts.regex ? text : text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (opts.word) src = `(?<!\\w)(?:${src})(?!\\w)`;
  try {
    return new RegExp(src, opts.case ? "gu" : "giu");
  } catch (e) {
    // "Invalid regular expression: /upload(/giu: Unterminated group" → the reason.
    const why = (e as Error).message.replace(/^Invalid regular expression: \/.*\/[a-z]*: /, "");
    return new Error("invalid regular expression: " + why.charAt(0).toLowerCase() + why.slice(1));
  }
}

/** Every hit of ``pattern`` in the row. */
export function rowHits(cells: RowCell[], pattern: RegExp): RowHit[] {
  // The row as a string, with the cell and code-point index of every UTF-16
  // unit, so a match's string offsets map back to cells and to the server's
  // code-point columns.
  let text = "";
  const cellAt: number[] = [];
  const cpAt: number[] = [];
  let cp = 0;
  cells.forEach((cell, i) => {
    for (const c of Array.from(cell.ch || " ")) {
      for (let k = 0; k < c.length; k++) {
        cellAt.push(i);
        cpAt.push(cp);
      }
      text += c;
      cp++;
    }
  });
  cellAt.push(cells.length);
  cpAt.push(cp);
  const out: RowHit[] = [];
  const re = new RegExp(pattern.source, pattern.flags.includes("g") ? pattern.flags : pattern.flags + "g");
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (!m[0].length) {
      re.lastIndex++; // an empty match isn't a hit
      continue;
    }
    const s = m.index;
    const e = s + m[0].length;
    const a = cells[cellAt[s]];
    const b = cells[cellAt[e - 1]];
    out.push({ start: cpAt[s], len: cpAt[e] - cpAt[s], x: a.x, width: b.x + b.w - a.x });
  }
  return out;
}
