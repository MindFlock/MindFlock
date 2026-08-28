import { describe, it, expect } from "vitest";
// The dbclient extension ships as plain ES modules under the Python package's
// static dir (no build step), so the splitter is imported by relative path from
// outside the TS root. vitest resolves it fine; tsc has no declaration for a
// .js file without allowJs, hence the ignore — the assertions below type it.
// @ts-ignore -- untyped ES module outside src/
import * as sqlMod from "../../../backend/web/static/extensions/dbclient/sql.js";

interface Stmt {
  text: string;
  start: number;
  end: number;
}
interface Opts {
  dialect?: "standard" | "mysql";
}
const splitStatements = sqlMod.splitStatements as (sql: string, opts?: Opts) => Stmt[];
const statementAt = sqlMod.statementAt as (sql: string, cursor: number, opts?: Opts) => Stmt | null;
const stripLiterals = sqlMod.stripLiterals as (sql: string, opts?: Opts) => string;
const classifyStatement = sqlMod.classifyStatement as (
  sql: string,
  opts?: Opts
) => { verb: string; hasWhere: boolean; isWrite: boolean };

const texts = (sql: string, opts?: Opts) => splitStatements(sql, opts).map((s) => s.text);

describe("splitStatements", () => {
  it("splits on semicolons and keeps a trailing statement without one", () => {
    expect(texts("SELECT 1; SELECT 2;\nSELECT 3")).toEqual(["SELECT 1", "SELECT 2", "SELECT 3"]);
  });

  it("returns offsets that slice back to the trimmed text", () => {
    const sql = "  SELECT 1 ;\n\n  UPDATE t SET a = 1  ";
    for (const s of splitStatements(sql)) {
      expect(sql.slice(s.start, s.end)).toBe(s.text);
    }
    // "  SELECT 1 ;\n\n  UPDATE…" — the second statement starts after two
    // newlines and two spaces, at 16.
    expect(splitStatements(sql).map((s) => [s.start, s.end])).toEqual([
      [2, 10],
      [16, 34],
    ]);
  });

  it("drops empty and comment-only segments", () => {
    expect(texts(";;  ; -- nothing\n/* still nothing */ ;")).toEqual([]);
    expect(texts("SELECT 1;\n-- trailing note")).toEqual(["SELECT 1"]);
    expect(texts("")).toEqual([]);
  });

  it("ignores semicolons inside single-quoted strings, including '' doubling", () => {
    expect(texts("SELECT 'a;b'; SELECT 'it''s; fine'; SELECT 1")).toEqual([
      "SELECT 'a;b'",
      "SELECT 'it''s; fine'",
      "SELECT 1",
    ]);
  });

  it("ignores semicolons inside double-quoted and backtick identifiers", () => {
    expect(texts('SELECT "a;b" FROM t; SELECT `x;y` FROM u')).toEqual([
      'SELECT "a;b" FROM t',
      "SELECT `x;y` FROM u",
    ]);
  });

  it("does not treat a backslash as an escape in standard strings", () => {
    // PostgreSQL / SQLite: '\' is a complete one-character string.
    expect(texts("SELECT 'C:\\'; SELECT 2")).toEqual(["SELECT 'C:\\'", "SELECT 2"]);
  });

  it("honours backslash escapes in E'…' strings and in the mysql dialect", () => {
    expect(texts("SELECT E'it\\'s; ok'; SELECT 2")).toEqual(["SELECT E'it\\'s; ok'", "SELECT 2"]);
    expect(texts("SELECT 'it\\'s; ok'; SELECT 2", { dialect: "mysql" })).toEqual([
      "SELECT 'it\\'s; ok'",
      "SELECT 2",
    ]);
    expect(texts('SELECT "a\\"; b"; SELECT 2', { dialect: "mysql" })).toEqual([
      'SELECT "a\\"; b"',
      "SELECT 2",
    ]);
  });

  it("skips line comments (--, and # for mysql only)", () => {
    expect(texts("SELECT 1 -- ; not a split\n; SELECT 2")).toEqual([
      "SELECT 1 -- ; not a split",
      "SELECT 2",
    ]);
    expect(texts("SELECT 1 # ; comment\n; SELECT 2", { dialect: "mysql" })).toEqual([
      "SELECT 1 # ; comment",
      "SELECT 2",
    ]);
    // Standard SQL: '#' is just a character, so the ';' after it splits.
    expect(texts("SELECT 1 # ; SELECT 2")).toEqual(["SELECT 1 #", "SELECT 2"]);
  });

  it("skips block comments, nesting them like PostgreSQL", () => {
    expect(texts("SELECT /* a; b */ 1; SELECT 2")).toEqual(["SELECT /* a; b */ 1", "SELECT 2"]);
    expect(texts("SELECT /* outer /* inner; */ still; */ 1; SELECT 2")).toEqual([
      "SELECT /* outer /* inner; */ still; */ 1",
      "SELECT 2",
    ]);
  });

  it("treats $$ and $tag$ bodies as opaque", () => {
    const fn =
      "CREATE FUNCTION f() RETURNS int AS $$\nBEGIN\n  RETURN 1;\nEND;\n$$ LANGUAGE plpgsql;\nSELECT f()";
    expect(texts(fn)).toEqual([fn.slice(0, fn.indexOf(";\nSELECT")), "SELECT f()"]);
    const tagged = "DO $body$ BEGIN PERFORM 1; END $body$; SELECT 2";
    expect(texts(tagged)).toEqual(["DO $body$ BEGIN PERFORM 1; END $body$", "SELECT 2"]);
  });

  it("does not mistake a positional parameter or an identifier's $ for a dollar quote", () => {
    expect(texts("SELECT $1; SELECT 2")).toEqual(["SELECT $1", "SELECT 2"]);
    expect(texts("SELECT a$b$ FROM t; SELECT 2")).toEqual(["SELECT a$b$ FROM t", "SELECT 2"]);
  });

  it("swallows an unterminated string to the end rather than splitting inside it", () => {
    expect(texts("SELECT 'open; still")).toEqual(["SELECT 'open; still"]);
  });
});

describe("statementAt", () => {
  const sql = "SELECT 1;\nSELECT 2;  \n\nSELECT 3";
  // Offsets: "SELECT 1" = 0..8, ';' at 8, "SELECT 2" = 10..18, ';' at 18,
  // "SELECT 3" = 23..31.

  it("returns null for empty input", () => {
    expect(statementAt("", 0)).toBeNull();
    expect(statementAt("  -- only a comment", 5)).toBeNull();
  });

  it("picks the statement whose span contains the caret, ends inclusive", () => {
    expect(statementAt(sql, 0)?.text).toBe("SELECT 1");
    expect(statementAt(sql, 4)?.text).toBe("SELECT 1");
    // Right before the ';' (caret at the end of the text) still counts.
    expect(statementAt(sql, 8)?.text).toBe("SELECT 1");
    expect(statementAt(sql, 10)?.text).toBe("SELECT 2");
    expect(statementAt(sql, 31)?.text).toBe("SELECT 3");
  });

  it("after a ';' on the same line keeps the previous statement", () => {
    // Caret just past the ';' of SELECT 1 (before the newline).
    expect(statementAt(sql, 9)?.text).toBe("SELECT 1");
    // Trailing spaces after "SELECT 2;" — same line, still SELECT 2.
    expect(statementAt(sql, 20)?.text).toBe("SELECT 2");
  });

  it("on a later line before the next statement moves to the next one", () => {
    // The blank line between SELECT 2 and SELECT 3.
    expect(statementAt(sql, 22)?.text).toBe("SELECT 3");
  });

  it("clamps a caret past the end to the last statement, and before the first to the first", () => {
    expect(statementAt(sql, 999)?.text).toBe("SELECT 3");
    expect(statementAt("\n\n  SELECT 1", 0)?.text).toBe("SELECT 1");
  });
});

describe("stripLiterals / classifyStatement", () => {
  it("blanks comments and literal bodies while keeping length and delimiters", () => {
    const sql = "SELECT 'where' /* where */ -- where\nFROM t";
    const bare = stripLiterals(sql);
    expect(bare.length).toBe(sql.length);
    expect(/\bwhere\b/i.test(bare)).toBe(false);
    expect(bare.startsWith("SELECT '")).toBe(true);
  });

  it("names the verb and whether a real WHERE is present", () => {
    expect(classifyStatement("DELETE FROM t")).toEqual({ verb: "DELETE", hasWhere: false, isWrite: true });
    expect(classifyStatement("UPDATE t SET a = 'where'")).toEqual({
      verb: "UPDATE",
      hasWhere: false,
      isWrite: true,
    });
    expect(classifyStatement("update t set a = 1 where id = 2")).toEqual({
      verb: "UPDATE",
      hasWhere: true,
      isWrite: true,
    });
    expect(classifyStatement("-- note\nSELECT 1")).toEqual({ verb: "SELECT", hasWhere: false, isWrite: false });
  });
});
