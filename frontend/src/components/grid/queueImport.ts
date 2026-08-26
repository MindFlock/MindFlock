/** File → prompts for the Queue tab's drop-to-import: a .csv queues one
 * prompt per record (quoted fields, embedded commas/newlines and "" escapes
 * handled), anything else queues one prompt per line. Pure, so the parsing —
 * the part with the quoting edge cases — is unit-testable without a DOM. */

/** Minimal RFC-4180-style CSV: records of cells, honoring quoted fields with
 * embedded commas, newlines, and doubled-quote escapes. */
export function csvRecords(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          cell += '"';
          i++;
        } else inQuotes = false;
      } else cell += ch;
    } else if (ch === '"') {
      inQuotes = true;
    } else if (ch === ",") {
      row.push(cell);
      cell = "";
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else cell += ch;
  }
  if (cell !== "" || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

/** Column names that mark a first row as a header, not a prompt. */
const HEADER_WORDS = new Set([
  "prompt",
  "prompts",
  "text",
  "entry",
  "entries",
  "task",
  "tasks",
  "item",
  "items",
  "message",
  "messages",
  "queue",
  // Companion columns a prompt sheet tends to carry next to the prompt.
  "title",
  "name",
  "description",
  "priority",
  "notes",
  "order",
  "id",
]);

/** The prompts a dropped file means: CSV → one per record (multi-column rows
 * collapse to their non-empty cells joined by a space; an obvious header row
 * like "prompt" is skipped), everything else → one per non-blank line. */
export function promptsFromFile(name: string, text: string): string[] {
  let lines: string[];
  if (/\.csv$/i.test(name)) {
    const records = csvRecords(text).map((cells) =>
      cells.map((c) => c.trim()).filter(Boolean)
    );
    const first = records[0];
    if (first?.length && first.every((c) => HEADER_WORDS.has(c.toLowerCase()))) {
      records.shift();
    }
    lines = records.map((cells) => cells.join(" "));
  } else {
    lines = text.split(/\r\n|\r|\n/);
  }
  return lines.map((l) => l.trim()).filter(Boolean);
}
