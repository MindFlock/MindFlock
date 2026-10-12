/** The app's SOURCE as text, for the structural tests: JSX wiring, CSS rules
 * and "X is gone" checks that the node test environment can't exercise.
 *
 * These replaced pytest checks that grepped the BUILT bundle
 * (backend/web/static/app.js / style.css). Every bundler bump re-laid that text
 * out and broke them; source text is ours, and pinning the module that owns a
 * thing is stronger than "somewhere in app.js". Sources come through Vite's
 * glob (this project installs no node types); stylesheets need vitest.config's
 * `css.include` for `?raw`, or they read as "" (sourceText.test.ts guards it).
 *
 * Two rules keep a move from the bundle faithful:
 *  - the bundle carried no comments, so the `code` / `normCode` / `css`
 *    readers drop or blank them — a positive pin can't pass on prose, and a
 *    negative ("X is gone") can't fail on a comment that merely mentions X;
 *  - a negative runs over EVERY app file (`codeFilesWith`, `allCode`,
 *    `ALL_CODE`), never one module, so it stays as broad as "not in the
 *    bundle".
 *
 * Paths are keyed under src/ ("lib/keymap.ts"); a typo or a moved module
 * throws instead of matching nothing. */

const RAW = import.meta.glob<string>(["../**/*.{ts,tsx,css}", "!../__tests__/**"], {
  query: "?raw",
  import: "default",
  eager: true,
});

/** Every app source file (never the tests), keyed by its path under src/. */
export const SOURCES: Record<string, string> = Object.fromEntries(
  Object.entries(RAW).map(([path, text]) => [path.replace(/^\.\.\//, ""), text])
);

const pick = (re: RegExp): Record<string, string> =>
  Object.fromEntries(Object.entries(SOURCES).filter(([p]) => re.test(p)));

/** The .ts/.tsx subset of {@link SOURCES}. */
export const SRC = pick(/\.tsx?$/);
/** The .css subset of {@link SOURCES}. */
export const CSS = pick(/\.css$/);

const TS_FILES = Object.keys(SRC);
const CSS_FILES = Object.keys(CSS);

/** One source file's raw text (comments kept). */
export function src(path: string): string {
  const text = SOURCES[path];
  if (text === undefined) throw new Error("no such source: src/" + path);
  return text;
}

/** Collapse every whitespace run to one space (the old tests/_bundle.squash). */
export const squash = (s: string): string => s.replace(/\s+/g, " ");

/** Layout-blind form of a code snippet: whitespace runs collapse, and the
 * trailing comma / space a line break leaves just inside a bracket goes.
 * Token order, quoting and spelling still have to match. */
export function norm(s: string): string {
  return s
    .replace(/\s*,\s*(?=[)\]}])/g, " ")
    .replace(/\s+/g, " ")
    .replace(/([([]) /g, "$1")
    .replace(/ ([)\]])/g, "$1");
}

// --- comments ----------------------------------------------------------------

const REGEX_PREV = new Set([..."(,=:[!&|?{};+-*%<>~^"]);

/** Scan TS/TSX text, handing each comment to `onComment` (which returns its
 * replacement) and keeping strings, template literals (with `${…}` nesting)
 * and regex literals verbatim. A `//` right after a `:` is a URL in JSX text,
 * not a comment; a '…' or "…" literal never spans a line, so an apostrophe in
 * JSX text ("it's") can't swallow the file. */
function scan(s: string, onComment: (text: string, out: string, rest: string) => [string, number]): string {
  let out = "";
  let i = 0;
  const n = s.length;
  // Template nesting: the brace depth at which each open `${…}` closes.
  const tmpl: number[] = [];
  let depth = 0;
  let prev = "";
  const scanTemplate = () => {
    let j = i;
    while (j < n) {
      if (s[j] === "\\") {
        j += 2;
        continue;
      }
      if (s[j] === "`") {
        out += s.slice(i, j + 1);
        i = j + 1;
        prev = "`";
        return;
      }
      if (s[j] === "$" && s[j + 1] === "{") {
        out += s.slice(i, j + 2);
        i = j + 2;
        tmpl.push(depth);
        depth++;
        prev = "{";
        return;
      }
      j++;
    }
    out += s.slice(i);
    i = n;
  };
  while (i < n) {
    const c = s[i];
    const d = s[i + 1];
    if ((c === "/" && d === "/" && s[i - 1] !== ":") || (c === "/" && d === "*")) {
      const end =
        d === "/"
          ? (s.indexOf("\n", i) + 1 || n + 1) - 1
          : s.indexOf("*/", i + 2) < 0
            ? n
            : s.indexOf("*/", i + 2) + 2;
      const [replaced, skip] = onComment(s.slice(i, end), out, s.slice(end));
      out = replaced;
      i = end + skip;
      continue;
    }
    if (c === '"' || c === "'") {
      let j = i + 1;
      while (j < n && s[j] !== c && s[j] !== "\n") j += s[j] === "\\" ? 2 : 1;
      out += s.slice(i, j + 1);
      i = j + 1;
      prev = c;
      continue;
    }
    if (c === "`") {
      out += c;
      i++;
      scanTemplate();
      continue;
    }
    if (c === "/" && (prev === "" || REGEX_PREV.has(prev))) {
      let j = i + 1;
      let cls = false;
      while (j < n && s[j] !== "\n") {
        if (s[j] === "\\") {
          j += 2;
          continue;
        }
        if (s[j] === "[") cls = true;
        else if (s[j] === "]") cls = false;
        else if (s[j] === "/" && !cls) break;
        j++;
      }
      out += s.slice(i, j + 1);
      i = j + 1;
      prev = "/";
      continue;
    }
    if (c === "{") depth++;
    if (c === "}") {
      depth--;
      if (tmpl.length && tmpl[tmpl.length - 1] === depth) {
        tmpl.pop();
        out += c;
        i++;
        scanTemplate();
        continue;
      }
    }
    if (!/\s/.test(c)) prev = c;
    out += c;
    i++;
  }
  return out;
}

/** `s` with every // and block comment BLANKED (spaces; newlines kept), so
 * offsets and line structure survive. */
export function stripComments(s: string): string {
  return scan(s, (text, out) => [out + text.replace(/[^\n]/g, " "), 0]);
}

/** `s` with every comment REMOVED (a line comment's newline stays), and a JSX
 * comment's own braces with it (`{/* … *\/}` leaves nothing) — for checks that
 * measure distances in characters, which prose must not stretch. */
export function dropComments(s: string): string {
  return scan(s, (text, out, rest) =>
    text.startsWith("/*") && out.endsWith("{") && rest.startsWith("}") ? [out.slice(0, -1), 1] : [out, 0]
  );
}

const CODE_CACHE = new Map<string, string>();

/** One TS/TSX file with its comments blanked (layout otherwise untouched). */
export function code(path: string): string {
  let t = CODE_CACHE.get(path);
  if (t === undefined) {
    t = stripComments(src(path));
    CODE_CACHE.set(path, t);
  }
  return t;
}

/** One file (TS/TSX or CSS) with comments removed, layout-blind (`norm`). */
export function normCode(path: string): string {
  return norm(path.endsWith(".css") ? dropCssComments(src(path)) : dropComments(src(path)));
}

/** One stylesheet with its comments blanked. */
export function css(path: string): string {
  return src(path).replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "));
}

const dropCssComments = (s: string): string => s.replace(/\/\*[\s\S]*?\*\//g, "");

/** Does `hay` contain `needle`, ignoring layout (both sides `norm`ed)? */
export function has(hay: string, needle: string): boolean {
  return norm(hay).includes(norm(needle));
}

/** All app code (.ts/.tsx), comments removed and `norm`ed — "anywhere in
 * app.js", for the pins that really mean anywhere. */
export function allCode(): string {
  return TS_FILES.slice()
    .sort()
    .map((f) => normCode(f))
    .join("\n");
}

/** All app code (.ts/.tsx) as written, comments KEPT — the strictest
 * "never anywhere" haystack. */
export const ALL_CODE = TS_FILES.map((p) => SOURCES[p]).join("\n");

/** All app stylesheets as written. */
export const ALL_CSS = CSS_FILES.map((p) => SOURCES[p]).join("\n");

/** The source files whose raw text contains `needle`, filtered by extension. */
export function filesWith(needle: string, ext: RegExp = /\.(tsx?|css)$/): string[] {
  return Object.keys(SOURCES).filter((p) => ext.test(p) && SOURCES[p].includes(needle));
}

/** Every app TS/TSX file whose CODE (comments excluded) contains `needle`. */
export function codeFilesWith(needle: string): string[] {
  return TS_FILES.filter((p) => code(p).includes(needle));
}

/** Every app stylesheet whose rules (comments excluded) contain `needle`. */
export function cssFilesWith(needle: string): string[] {
  return CSS_FILES.filter((p) => css(p).includes(needle));
}

/** The top-level declaration `name` in `file` — `function name(`,
 * `export function name<…>(`, `const name = …` — up to its closing line (a
 * column-0 `}` / `});` / `};`), comments removed and `norm`ed. */
export function fn(file: string, name: string): string {
  const text = src(file);
  const re = new RegExp(`^(?:export )?(?:default )?(?:async )?(?:function ${name}[<(]|const ${name} = )`, "m");
  const m = re.exec(text);
  if (!m) throw new Error(`no top-level ${name} in ${file}`);
  const rest = text.slice(m.index);
  const end = rest.search(/\n\}\)?;?(?=\n|$)/);
  return norm(dropComments(end < 0 ? rest : rest.slice(0, end + 2)));
}

/** From the first `start` in `text` up to (not including) the next `stop`. */
export function blockFrom(text: string, start: string, stop = "}"): string {
  const i = text.indexOf(start);
  if (i < 0) throw new Error(`not found: ${start}`);
  const j = text.indexOf(stop, i);
  return text.slice(i, j < 0 ? undefined : j);
}

// --- CSS rules -----------------------------------------------------------------

/** Every top-level rule (selector line through its closing brace) whose
 * selector is exactly `selector`, across the given stylesheets (default: all
 * of them). Comments are blanked first. */
export function cssRules(selector: string, files: string[] = CSS_FILES): string[] {
  const out: string[] = [];
  for (const f of files) {
    const text = "\n" + css(f);
    const needle = "\n" + selector + " {";
    let at = text.indexOf(needle);
    while (at !== -1) {
      const end = text.indexOf("\n}", at);
      out.push(text.slice(at, end + 2));
      at = text.indexOf(needle, end);
    }
  }
  return out;
}

/** The declarations of the CSS rule(s) in `cssText` whose selector list
 * contains `selector` exactly (whitespace-normalised), joined; "" if none. */
export function cssRule(cssText: string, selector: string): string {
  const flat = squash(dropCssComments(cssText));
  const want = squash(selector).trim();
  const bodies: string[] = [];
  for (const m of flat.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (m[1].split(",").map((s) => s.trim()).includes(want)) bodies.push(m[2].trim());
  }
  return bodies.join(" ");
}

// --- JSX tags ------------------------------------------------------------------

export interface JsxTag {
  /** The element name: "button", "textarea", "Modal". */
  name: string;
  /** The opening tag's full text, `<` to `>`. */
  text: string;
}

/** The opening tag that starts at `start` (a `<`), braces and strings respected. */
function openingTagAt(source: string, start: number): string {
  let depth = 0;
  let quote = "";
  for (let i = start + 1; i < source.length; i++) {
    const c = source[i];
    if (quote) {
      if (c === quote && source[i - 1] !== "\\") quote = "";
      continue;
    }
    // Comments between attributes (and inside expressions) are prose.
    if (c === "/" && source[i + 1] === "/") {
      const nl = source.indexOf("\n", i);
      i = nl < 0 ? source.length : nl;
      continue;
    }
    if (c === "/" && source[i + 1] === "*") {
      const end = source.indexOf("*/", i + 2);
      i = end < 0 ? source.length : end + 1;
      continue;
    }
    if (c === '"' || (depth > 0 && (c === "'" || c === "`"))) quote = c;
    else if (c === "{") depth++;
    else if (c === "}") depth--;
    else if (c === ">" && depth === 0) return source.slice(start, i + 1);
  }
  throw new Error("unterminated tag at " + start);
}

/** Every JSX opening tag in `source` carrying the attribute `attrName="value"`. */
export function tagsWith(source: string, attrName: string, value: string): JsxTag[] {
  const out: JsxTag[] = [];
  const needle = new RegExp(`(?<=\\s)${attrName}="${value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"`, "g");
  for (const m of source.matchAll(needle)) {
    // Walk back to the `<` that opens this element, skipping {…} expressions.
    let depth = 0;
    let i = m.index! - 1;
    for (; i >= 0; i--) {
      const c = source[i];
      if (c === "}") depth++;
      else if (c === "{") depth--;
      else if (c === "<" && depth === 0 && /[A-Za-z]/.test(source[i + 1] || "")) break;
    }
    if (i < 0) continue;
    const text = openingTagAt(source, i);
    out.push({ name: /^<([A-Za-z][\w.]*)/.exec(text)![1], text });
  }
  return out;
}

/** Every JSX opening tag with `id="…"`. */
export const tagsWithId = (source: string, id: string): JsxTag[] => tagsWith(source, "id", id);

/** An attribute's value on a tag: the string of `a="x"`, or the raw
 * expression of `a={…}`; undefined when absent. */
export function attr(tag: JsxTag, name: string): string | undefined {
  const m = new RegExp(`\\s${name}=(?:"([^"]*)"|\\{)`).exec(tag.text);
  if (!m) return undefined;
  if (m[1] !== undefined) return m[1];
  let depth = 1;
  const from = m.index + m[0].length;
  for (let i = from; i < tag.text.length; i++) {
    if (tag.text[i] === "{") depth++;
    else if (tag.text[i] === "}" && --depth === 0) return tag.text.slice(from, i);
  }
  return undefined;
}
