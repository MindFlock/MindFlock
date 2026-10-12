/** The source reader the structural tests stand on (sourceText.ts). If it
 * misreads a file, a positive pin passes on prose or a negative one passes on
 * an empty string — so its scanner and its inputs are pinned here. */

import { describe, expect, it } from "vitest";
import {
  ALL_CSS,
  CSS,
  SRC,
  code,
  css,
  cssRule,
  cssRules,
  dropComments,
  fn,
  has,
  normCode,
  stripComments,
  tagsWithId,
  attr,
} from "./sourceText";

const SAMPLE = [
  'const a = "x // y"; // gone-1',
  "/* gone-2 */ const b = `t ${\"//\"} u ${`in ${1 /* gone-3 */}`}`; const r = /[\"']\\//;",
  'const re2 = /"/; const after = "kept // too";',
  "<a>https://x.dev</a>",
  "<div>{/* gone-4 */}<span/></div>",
  "const s = '/* not a comment */';",
].join("\n");

describe("stripComments (blank)", () => {
  const s = stripComments(SAMPLE);

  it("blanks every comment, keeping offsets and lines", () => {
    expect(s).not.toMatch(/gone-\d/);
    expect(s.length).toBe(SAMPLE.length);
    expect(s.split("\n").length).toBe(SAMPLE.split("\n").length);
  });

  it("keeps strings, URLs in JSX text, templates with nested ${}, and regex literals", () => {
    expect(s).toContain('"x // y"');
    expect(s).toContain('`t ${"//"} u ${`in ${1 ');
    expect(s).toContain("/[\"']\\//");
    expect(s).toContain('const re2 = /"/; const after = "kept // too";');
    expect(s).toContain("https://x.dev");
    expect(s).toContain("'/* not a comment */'");
  });
});

describe("dropComments (remove)", () => {
  const s = dropComments(SAMPLE);

  it("removes comments, and a JSX comment's own braces", () => {
    expect(s).not.toMatch(/gone-\d/);
    expect(s).toContain("<div><span/></div>");
    expect(s).toContain('const a = "x // y"; \n');
  });

  it("leaves literals alone", () => {
    expect(s).toContain('"kept // too"');
    expect(s).toContain("'/* not a comment */'");
    expect(s).toContain("https://x.dev");
  });
});

describe("the inputs are real", () => {
  it("reads the app's modules and stylesheets (never the tests)", () => {
    expect(Object.keys(SRC).length).toBeGreaterThan(100);
    expect(Object.keys(CSS).length).toBeGreaterThan(20);
    expect(Object.keys(SRC).some((p) => p.includes("__tests__"))).toBe(false);
  });

  it("stylesheets are not empty (vitest.config's css.include for ?raw)", () => {
    for (const [p, t] of Object.entries(CSS)) expect(t.length, p).toBeGreaterThan(0);
    expect(ALL_CSS).toContain("{");
    expect(css("styles/index.css")).toContain("@import");
  });

  it("a missing module throws instead of matching nothing", () => {
    expect(() => code("no/such/module.ts")).toThrow();
    expect(() => fn("lib/keymap.ts", "noSuchFunction")).toThrow();
  });
});

describe("the readers", () => {
  it("normCode and has are layout-blind, not spelling-blind", () => {
    expect(has("f(\n  a,\n  b,\n)", "f(a, b)")).toBe(true);
    expect(has("f(a, b)", "f(b, a)")).toBe(false);
    expect(normCode("lib/keymap.ts")).not.toMatch(/\n/);
  });

  it("cssRule / cssRules find a rule by its exact selector", () => {
    const sheet = "/* .a { color: red } */\n.a,\n.b {\n  color: blue;\n}\n.ab { x: y; }\n";
    expect(cssRule(sheet, ".a")).toBe("color: blue;");
    expect(cssRule(sheet, ".c")).toBe("");
    expect(cssRules(".sidebar-resizer", Object.keys(CSS)).length).toBeGreaterThan(0);
  });

  it("tagsWithId / attr read a JSX opening tag", () => {
    const [tag] = tagsWithId('<button\n  id="go"\n  onClick={() => run({ a: 1 })}\n  title="Go">', "go");
    expect(tag.name).toBe("button");
    expect(attr(tag, "title")).toBe("Go");
    expect(attr(tag, "onClick")).toBe("() => run({ a: 1 })");
  });
});
