/** Pure SQL text utilities for the dbclient extension — the statement splitter
 * behind "Run" (statement at cursor) and "Run All", plus the small classifier
 * the query pad uses to word its no-WHERE confirmation. No DOM, no imports:
 * vitest imports this file by relative path from frontend/src/__tests__.
 *
 * One left-to-right scan (regions()) partitions the text into code, comment,
 * string and dollar-quoted regions; everything else is a consumer of that
 * partition. The backend (service.py) runs its own scanner over the same
 * grammar to count statements server-side, so this one is a convenience for
 * the editor, never the guard.
 *
 * Grammar: '…' with '' doubling (backslash escapes only for the mysql dialect
 * and for E'…' strings), "…" with "" doubling, `…` with `` doubling, -- line
 * comments, # line comments (mysql only), slash-star block comments (nesting,
 * as PostgreSQL does; flat for mysql), and $tag$ … $tag$ dollar quoting. A
 * ';' in a code region ends a statement; the trailing statement needs no ';'.
 * Segments holding only whitespace and comments are dropped. */

const IDENT_CHAR = /[A-Za-z0-9_]/;
const TAG_START = /[A-Za-z_]/;

/** Partition `text` into contiguous regions {type, start, end} where type is
 * "code" | "comment" | "string" | "dollar". Unterminated constructs swallow
 * the rest of the text — the editor would rather run one odd statement than
 * split a string in half. */
export function regions(text, opts) {
  const src = String(text == null ? "" : text);
  const mysql = !!(opts && opts.dialect === "mysql");
  const out = [];
  const n = src.length;
  let i = 0;
  let codeStart = 0;

  const closeCode = (at) => {
    if (at > codeStart) out.push({ type: "code", start: codeStart, end: at });
  };

  while (i < n) {
    const c = src[i];
    const next = src[i + 1];

    if ((c === "-" && next === "-") || (mysql && c === "#")) {
      closeCode(i);
      const end = skipLine(src, i + (c === "#" ? 1 : 2));
      out.push({ type: "comment", start: i, end });
      i = codeStart = end;
      continue;
    }
    if (c === "/" && next === "*") {
      closeCode(i);
      const end = skipBlockComment(src, i + 2, !mysql);
      out.push({ type: "comment", start: i, end });
      i = codeStart = end;
      continue;
    }
    if (c === "'" || c === '"' || c === "`") {
      // E'…' turns backslash escapes on for that one string (PostgreSQL);
      // mysql has them on for '…' and "…" always. Backticks never escape.
      let escapes = false;
      if (c === "'") {
        const prev = src[i - 1];
        const isE = (prev === "E" || prev === "e") && !IDENT_CHAR.test(src[i - 2] || "");
        escapes = mysql || isE;
      } else if (c === '"') {
        escapes = mysql;
      }
      closeCode(i);
      const end = skipQuoted(src, i + 1, c, escapes);
      out.push({ type: "string", start: i, end });
      i = codeStart = end;
      continue;
    }
    // $tag$ … $tag$ — never right after an identifier char, or a "a$b$"-style
    // identifier would read as an opener; "$1" (a parameter) has no tag.
    if (c === "$" && !IDENT_CHAR.test(src[i - 1] || "")) {
      const tag = dollarTag(src, i);
      if (tag !== null) {
        closeCode(i);
        const closeAt = src.indexOf(tag, i + tag.length);
        const end = closeAt < 0 ? n : closeAt + tag.length;
        out.push({ type: "dollar", start: i, end });
        i = codeStart = end;
        continue;
      }
    }
    i += 1;
  }
  closeCode(n);
  return out;
}

function skipLine(src, i) {
  const nl = src.indexOf("\n", i);
  return nl < 0 ? src.length : nl;
}

function skipBlockComment(src, i, nest) {
  const n = src.length;
  let depth = 1;
  while (i < n && depth > 0) {
    if (nest && src[i] === "/" && src[i + 1] === "*") {
      depth += 1;
      i += 2;
      continue;
    }
    if (src[i] === "*" && src[i + 1] === "/") {
      depth -= 1;
      i += 2;
      continue;
    }
    i += 1;
  }
  return i;
}

function skipQuoted(src, i, q, backslash) {
  const n = src.length;
  while (i < n) {
    const c = src[i];
    if (backslash && c === "\\") {
      i += 2;
      continue;
    }
    if (c === q) {
      // A doubled delimiter is a literal delimiter, not the end.
      if (src[i + 1] === q) {
        i += 2;
        continue;
      }
      return i + 1;
    }
    i += 1;
  }
  return n;
}

/** The "$tag$" opener starting at src[i] (a "$"), or null when this "$" does
 * not open a dollar quote. */
function dollarTag(src, i) {
  let j = i + 1;
  if (src[j] === "$") return "$$";
  if (!TAG_START.test(src[j] || "")) return null;
  while (j < src.length && IDENT_CHAR.test(src[j])) j += 1;
  return src[j] === "$" ? src.slice(i, j + 1) : null;
}

/** Split `sql` into statements: [{text, start, end}] with
 * sql.slice(start, end) === text (trimmed of surrounding whitespace). */
export function splitStatements(sql, opts) {
  const src = String(sql == null ? "" : sql);
  const parts = regions(src, opts);
  const out = [];
  let segStart = 0;
  let hasCode = false;

  const flush = (end) => {
    if (hasCode) {
      const stmt = trimRange(src, segStart, end);
      if (stmt) out.push(stmt);
    }
    hasCode = false;
  };

  for (const r of parts) {
    if (r.type === "comment") continue;
    if (r.type !== "code") {
      hasCode = true;
      continue;
    }
    for (let i = r.start; i < r.end; i += 1) {
      const c = src[i];
      if (c === ";") {
        flush(i);
        segStart = i + 1;
      } else if (!hasCode && !/\s/.test(c)) {
        hasCode = true;
      }
    }
  }
  flush(src.length);
  return out;
}

function trimRange(src, a, b) {
  while (a < b && /\s/.test(src[a])) a += 1;
  while (b > a && /\s/.test(src[b - 1])) b -= 1;
  if (a >= b) return null;
  return { text: src.slice(a, b), start: a, end: b };
}

/** The statement the caret is on. Inside a statement's span (its end is
 * inclusive, so a caret right before the ';' counts) that statement wins; in
 * the gap after a ';' the PREVIOUS statement wins while the caret is still on
 * the same line (the natural "type ';', hit Ctrl+Enter" gesture), otherwise
 * the next one; past the last statement, the last. null when there is none. */
export function statementAt(sql, cursor, opts) {
  const src = String(sql == null ? "" : sql);
  const stmts = splitStatements(src, opts);
  if (!stmts.length) return null;
  const at = Math.max(0, Math.min(Number(cursor) || 0, src.length));
  for (const s of stmts) {
    if (at >= s.start && at <= s.end) return s;
  }
  for (let i = 0; i < stmts.length; i += 1) {
    const s = stmts[i];
    if (at < s.start) {
      const prev = stmts[i - 1];
      if (!prev) return s;
      return src.slice(prev.end, at).includes("\n") ? s : prev;
    }
  }
  return stmts[stmts.length - 1];
}

/** `sql` with every comment blanked to spaces and every string / dollar body
 * emptied (delimiters kept), same length as the input — what a keyword search
 * should run over so a 'where' inside a literal cannot fool it. */
export function stripLiterals(sql, opts) {
  const src = String(sql == null ? "" : sql);
  let out = "";
  for (const r of regions(src, opts)) {
    const chunk = src.slice(r.start, r.end);
    if (r.type === "code") out += chunk;
    else if (r.type === "comment") out += " ".repeat(chunk.length);
    else if (r.type === "string") {
      const q = chunk[0];
      const closed = chunk.length > 1 && chunk[chunk.length - 1] === q;
      out += q + " ".repeat(Math.max(0, chunk.length - (closed ? 2 : 1))) + (closed ? q : "");
    } else {
      // dollar: keep both tags, blank the body
      out += " ".repeat(chunk.length);
    }
  }
  return out;
}

/** {verb, hasWhere, isWrite} for one statement — the query pad's wording for
 * the server's needs_confirm reply ("DELETE without a WHERE clause"). The
 * server decides whether confirmation is needed; this only names the risk. */
export function classifyStatement(sql, opts) {
  const bare = stripLiterals(sql, opts);
  const m = /^\s*\(*\s*([A-Za-z]+)/.exec(bare);
  const verb = m ? m[1].toUpperCase() : "";
  const hasWhere = /\bWHERE\b/i.test(bare);
  return { verb, hasWhere, isWrite: verb === "UPDATE" || verb === "DELETE" };
}
