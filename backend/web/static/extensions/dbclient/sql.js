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

// ---------------------------------------------------------------------------
// The table page's visible SQL (buildTableSql) — the query the unified table
// view SHOWS for its structured state (sort, filters, page). The page itself
// still loads through /table-data with bound parameters; this text is the
// faithful, runnable mirror of it, so a sort click visibly rewrites the query
// and the user can edit it and press Run. Literals are inlined (quoted) here
// precisely because the text must stand alone as one statement.
// ---------------------------------------------------------------------------

// --- Identifiers ------------------------------------------------------------
// This module owns them (ui.js re-exports these) because they are part of the
// SQL text, and the rule is "write what a person would have typed":
// `SELECT * FROM abuse_reports LIMIT 100`, not
// `SELECT * FROM "public"."abuse_reports" LIMIT 100`. The quotes and the schema
// come back the moment they carry meaning — a capital, a space, a keyword, a
// schema that is not the one the engine already resolves to.

/** A name that needs no quotes: lowercase, no punctuation, not keyword-shaped.
 * Anything else is quoted, because there the quotes are what makes it work. */
const PLAIN_IDENT_RE = /^[a-z_][a-z0-9_]*$/;

/** Words that must stay quoted when they name a table or a column. Not the
 * full reserved list of any one engine — the union of what sqlite, postgres and
 * mysql refuse, trimmed to words people plausibly name things after. A miss
 * costs a manual quote in an editable text box (the data itself loads through
 * /table-data with bound parameters, never through this text), so the list errs
 * long rather than clever. */
const RESERVED = new Set(
  ("add all alter analyze and any array as asc authorization between binary both by " +
    "call cascade case cast change check collate column condition constraint continue " +
    "convert create cross cube current current_date current_role current_time " +
    "current_timestamp current_user cursor database databases default deferrable " +
    "delayed delete desc describe deterministic distinct distinctrow div do double " +
    "drop dual each else elseif enclosed end escape escaped except exists exit explain " +
    "false fetch float for force foreign from fulltext function grant group grouping " +
    "having high_priority if ignore ilike in index infile initially inner inout " +
    "insensitive insert int integer intersect interval into is isnull iterate join key " +
    "keys kill lateral leading leave left like limit lines load localtime " +
    "localtimestamp lock long loop low_priority match maxvalue modifies natural not " +
    "notnull null numeric offset on only optimize option optionally or order out outer " +
    "outfile over partition placing precision primary procedure purge range read reads " +
    "real recursive references regexp release rename repeat replace require resignal " +
    "restrict return returning revoke right rlike rows schema schemas select sensitive " +
    "separator session_user set show signal similar smallint some spatial specific sql " +
    "sqlexception sqlstate sqlwarning ssl starting stored straight_join symmetric table " +
    "terminated then to trailing trigger true undo union unique unlock unsigned update " +
    "usage use user using values varchar variables varying verbose virtual when where " +
    "while window with write xor zerofill").split(" ")
);

/** The schema an engine resolves to with no qualifier. MySQL is absent on
 * purpose: its "schema" is a database, and dropping that is a different table,
 * not a tidier name. */
const DEFAULT_SCHEMA = { postgres: "public", postgresql: "public", sqlite: "main" };

export function quoteIdent(engine, name) {
  const s = String(name);
  if (PLAIN_IDENT_RE.test(s) && !RESERVED.has(s)) return s;
  if (engine === "mysql") return "`" + s.replace(/`/g, "``") + "`";
  return '"' + s.replace(/"/g, '""') + '"';
}

/** True when naming this schema would tell the reader nothing the engine does
 * not already assume. With no `engine`, every engine's default counts — right
 * for a LABEL, which is why tableLabel() takes that path and SQL does not. */
export function isDefaultSchema(schema, engine) {
  if (!schema) return true;
  const want = String(schema).toLowerCase();
  if (engine) return DEFAULT_SCHEMA[String(engine).toLowerCase()] === want;
  return Object.values(DEFAULT_SCHEMA).includes(want);
}

/** The table as SQL: `abuse_reports`, or `analytics."order"` when the schema
 * and the quotes are doing work. */
export function qualifiedName(engine, schema, table) {
  const t = quoteIdent(engine, table);
  return isDefaultSchema(schema, engine) ? t : quoteIdent(engine, schema) + "." + t;
}

/** The same name for a title or a label — unquoted, and without the schema
 * nobody needed to hear. */
export function tableLabel(schema, table, engine) {
  return isDefaultSchema(schema, engine) ? String(table) : schema + "." + table;
}

const quoteIdentFor = quoteIdent;

/** One inlined string literal. MySQL string literals treat a backslash as an
 * escape character, so it is doubled there and only there. */
function sqlLiteral(engine, value) {
  let s = String(value);
  if (engine === "mysql") s = s.replace(/\\/g, "\\\\");
  return "'" + s.replace(/'/g, "''") + "'";
}

const NUMERIC_RE = /^-?\d+(\.\d+)?$/;

/** Coarse family of a declared column type — the twin of service.py's
 * type_family, for deciding whether a comparison value renders bare. */
function typeFamily(colType) {
  const t = String(colType || "").toUpperCase();
  if (t.includes("BOOL")) return "bool";
  if ((t.includes("INT") || t.includes("SERIAL")) && !t.includes("INTERVAL") && !t.includes("POINT")) return "int";
  if (["REAL", "FLOA", "DOUB"].some((k) => t.includes(k))) return "float";
  return "text";
}

/** A comparison value: bare for a numeric/bool COLUMN (matching how the
 * server binds by declared type), quoted for text — so "007" against a TEXT
 * column stays '007'. With no declared type, numeric-looking values render
 * bare. */
function sqlValue(engine, value, colType) {
  const s = String(value);
  const fam = colType === undefined ? null : typeFamily(colType);
  const bare = fam === null ? NUMERIC_RE.test(s.trim()) : (fam === "int" || fam === "float") && NUMERIC_RE.test(s.trim());
  return bare ? s.trim() : sqlLiteral(engine, s);
}

/** One filter's WHERE fragment — the same shapes /table-data generates
 * server-side (see service.py _filter_sql; keep the two in step). */
function filterSql(engine, f) {
  const q = quoteIdentFor(engine, f.column);
  const op = String(f.op || "");
  if (op === "null") return q + " IS NULL";
  if (op === "notnull") return q + " IS NOT NULL";
  if (op === "contains") {
    const pattern =
      "%" +
      String(f.value == null ? "" : f.value)
        .replace(/\\/g, "\\\\")
        .replace(/%/g, "\\%")
        .replace(/_/g, "\\_") +
      "%";
    const cast = engine === "mysql" ? "CAST(" + q + " AS CHAR)" : "CAST(" + q + " AS TEXT)";
    const like = engine === "postgres" ? "ILIKE" : "LIKE";
    const escape = engine === "mysql" ? "ESCAPE '\\\\'" : "ESCAPE '\\'";
    return cast + " " + like + " " + sqlLiteral(engine, pattern) + " " + escape;
  }
  const value = f.value == null ? "" : f.value;
  if (String(value) === "" && (op === "eq" || op === "ne")) {
    // The grid drops empty-value filters before they get here; an explicit
    // empty string still reads best as the literal.
    return q + " " + (op === "eq" ? "=" : "<>") + " ''";
  }
  const sym = { eq: "=", ne: "<>", gt: ">", lt: "<" }[op];
  if (!sym) return "";
  return q + " " + sym + " " + sqlValue(engine, value, f.colType);
}

/** The SELECT the table page is showing: `SELECT * FROM t [WHERE …]
 * [ORDER BY …] LIMIT n [OFFSET m]`, quoted for `engine`. opts: {engine,
 * schema, table, filters: [{column, op, value}], sort: {column, dir} | null,
 * limit, offset, columnTypes?: {name: declared type} — with types, values
 * quote by the column's family the way the server binds them}. */
export function buildTableSql(opts) {
  const engine = opts.engine || "";
  const types = opts.columnTypes || null;
  const target = qualifiedName(engine, opts.schema, opts.table);
  let sql = "SELECT * FROM " + target;
  const where = (opts.filters || [])
    .map((f) => filterSql(engine, types ? { ...f, colType: types[f.column] } : f))
    .filter(Boolean);
  if (where.length) sql += " WHERE " + where.join(" AND ");
  if (opts.sort && opts.sort.column) {
    sql +=
      " ORDER BY " +
      quoteIdentFor(engine, opts.sort.column) +
      (opts.sort.dir === "desc" ? " DESC" : " ASC");
  }
  const limit = Number(opts.limit) > 0 ? Math.floor(Number(opts.limit)) : 100;
  sql += " LIMIT " + limit;
  const offset = Number(opts.offset) > 0 ? Math.floor(Number(opts.offset)) : 0;
  if (offset) sql += " OFFSET " + offset;
  return sql;
}
