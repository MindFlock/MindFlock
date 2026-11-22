/** Every theme, resolved to the colours a person actually sees, plus the
 * colour maths the contrast test (themeContrast.test.ts) judges them with.
 *
 * A look is three choices: light or dark, a surface set (html[data-surface])
 * and an accent (html[data-accent]). This module replays the cascade the
 * browser applies for one combination — tokens.css `:root` (+ `.light`), the
 * Sky's text inks, then theme.css's accent and surface blocks (unlayered, so
 * they beat every layered token) — and resolves the var() / color-mix() chains
 * to plain hex, so a test can measure a pair the way it renders.
 *
 * Read from source text: tokens.css through sourceText, theme.css (served
 * as-is from backend/web/static, shared with the phone page) through a glob. */

import { src } from "./sourceText";

const THEME_RAW = import.meta.glob<string>("../../../backend/web/static/theme.css", {
  query: "?raw",
  import: "default",
  eager: true,
});

/** theme.css as served. */
export const THEME_CSS: string = Object.values(THEME_RAW)[0] ?? "";

type Vars = Record<string, string>;
interface Block {
  selectors: string[];
  vars: Vars;
}

const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");

/** Flat rule blocks (no nesting — these sheets have none) with their custom
 * properties. Non-custom declarations are ignored. */
export function blocks(css: string): Block[] {
  const out: Block[] = [];
  for (const m of stripComments(css).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const vars: Vars = {};
    for (const d of m[2].matchAll(/(--[\w-]+)\s*:\s*([^;]+);?/g)) vars[d[1]] = d[2].trim();
    out.push({ selectors: m[1].split(",").map((s) => s.trim()), vars });
  }
  return out;
}

function varsFor(list: Block[], selector: string): Vars {
  const out: Vars = {};
  for (const b of list) if (b.selectors.includes(selector)) Object.assign(out, b.vars);
  return out;
}

const THEME_BLOCKS = blocks(THEME_CSS);
const BASE_SHEETS = ["styles/tokens.css", "components/flights/FlywayCanvas.css"].map((p) =>
  blocks(src(p))
);

/** Every surface key theme.css defines, in file order ("" = the built-in
 * default, Swallow). */
export const SURFACE_KEYS: string[] = [
  "",
  ...new Set(
    THEME_BLOCKS.flatMap((b) =>
      b.selectors.flatMap((s) => [...s.matchAll(/^html\[data-surface="([\w-]+)"\]$/g)].map((m) => m[1]))
    )
  ),
];

/** Every accent key theme.css defines ("" = the built-in default). */
export const ACCENT_KEYS: string[] = [
  "",
  ...new Set(
    THEME_BLOCKS.flatMap((b) =>
      b.selectors.flatMap((s) => [...s.matchAll(/^html\[data-accent="([\w-]+)"\]$/g)].map((m) => m[1]))
    )
  ),
];

export type Mode = "dark" | "light";

/** The custom properties on <html> for one look, fully resolved. Values that
 * are not colours (shadows, lengths) come back as written. */
export function resolveLook(mode: Mode, surface: string, accent = ""): Vars {
  const raw: Vars = {};
  for (const sheet of BASE_SHEETS) {
    Object.assign(raw, varsFor(sheet, ":root"));
    if (mode === "light") Object.assign(raw, varsFor(sheet, ".light"));
  }
  // theme.css is unlayered, so it beats the layered tokens whatever the
  // specificity; within it, html.light[…] (0,2,1) beats html[…] (0,1,1).
  if (accent) {
    Object.assign(raw, varsFor(THEME_BLOCKS, `html[data-accent="${accent}"]`));
    if (mode === "light") Object.assign(raw, varsFor(THEME_BLOCKS, `html.light[data-accent="${accent}"]`));
  }
  if (surface) {
    Object.assign(raw, varsFor(THEME_BLOCKS, `html[data-surface="${surface}"]`));
    if (mode === "light") Object.assign(raw, varsFor(THEME_BLOCKS, `html.light[data-surface="${surface}"]`));
  }
  const out: Vars = {};
  const resolve = (name: string, depth = 0): string => {
    if (name in out) return out[name];
    if (depth > 20) throw new Error("var() cycle at " + name);
    const v = raw[name];
    if (v === undefined) return "";
    const r = substitute(v, (n) => resolve(n, depth + 1));
    out[name] = r;
    return r;
  };
  for (const name of Object.keys(raw)) resolve(name);
  return out;
}

/** var() and color-mix(in srgb, …) to hex; anything else is left alone. */
function substitute(value: string, lookup: (name: string) => string): string {
  let v = value;
  for (let i = 0; i < 20 && v.includes("var("); i++) {
    v = v.replace(/var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*(?:\([^()]*\))?[^()]*))?\)/, (_, n, fb) => {
      const got = lookup(n);
      return got !== "" ? got : (fb ?? "").trim();
    });
  }
  v = v.replace(
    /color-mix\(\s*in srgb\s*,\s*(#[0-9a-f]{3,8}|transparent)\s*(\d+(?:\.\d+)?%)?\s*,\s*(#[0-9a-f]{3,8}|transparent)\s*(\d+(?:\.\d+)?%)?\s*\)/i,
    (_, a, pa, b, pb) => {
      const p = pa ? parseFloat(pa) / 100 : pb ? 1 - parseFloat(pb) / 100 : 0.5;
      if (a === "transparent" || b === "transparent") return v; // alpha mixes: left unresolved
      return mix(a, b, p);
    }
  );
  return v.trim();
}

// ---------------------------------------------------------------------------
// Colour maths
// ---------------------------------------------------------------------------

export type RGB = [number, number, number];

export function hexToRgb(hex: string): RGB {
  let h = hex.trim().replace(/^#/, "");
  if (h.length === 3 || h.length === 4) h = [...h.slice(0, 3)].map((c) => c + c).join("");
  if (!/^[0-9a-f]{6}/i.test(h)) throw new Error("not a hex colour: " + hex);
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)) as RGB;
}

export const isHex = (v: string | undefined): v is string => !!v && /^#[0-9a-f]{3,8}$/i.test(v.trim());

const toHex = (rgb: RGB) => "#" + rgb.map((c) => Math.round(c).toString(16).padStart(2, "0")).join("");

/** color-mix(in srgb, a p, b) — p of a. */
export function mix(a: string, b: string, p: number): string {
  const x = hexToRgb(a);
  const y = hexToRgb(b);
  return toHex([0, 1, 2].map((i) => x[i] * p + y[i] * (1 - p)) as RGB);
}

const lin = (c: number) => {
  const s = c / 255;
  return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
};

/** WCAG 2 relative luminance. */
export function luminance(hex: string): number {
  const [r, g, b] = hexToRgb(hex).map(lin);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG 2 contrast ratio, 1–21. */
export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** OKLCH: L 0–1 (perceived lightness), C chroma (0 = grey; ~0.37 = the most
 * saturated sRGB can get), h degrees. */
export function oklch(hex: string): { L: number; C: number; h: number } {
  const [r, g, b] = hexToRgb(hex).map(lin);
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  const L = 0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s;
  const A = 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s;
  const B = 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s;
  return { L, C: Math.hypot(A, B), h: ((Math.atan2(B, A) * 180) / Math.PI + 360) % 360 };
}

/** Perceptual distance (OKLab ΔE, ×100 so ~2 is a just-noticeable step and
 * 10+ is plainly a different colour). */
export function deltaE(a: string, b: string): number {
  const p = oklch(a);
  const q = oklch(b);
  const pa = [p.L, p.C * Math.cos((p.h * Math.PI) / 180), p.C * Math.sin((p.h * Math.PI) / 180)];
  const qa = [q.L, q.C * Math.cos((q.h * Math.PI) / 180), q.C * Math.sin((q.h * Math.PI) / 180)];
  return 100 * Math.hypot(pa[0] - qa[0], pa[1] - qa[1], pa[2] - qa[2]);
}
