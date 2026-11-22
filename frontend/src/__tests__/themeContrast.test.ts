/** Every theme has to be comfortable to read: the guard rail for theme.css.
 *
 * The owner's verdict on the first aviary was that only Bluejay and the
 * standard look were usable — the rest were "far too bright" and hard to read.
 * This walks EVERY surface set × light/dark × accent, resolved the way the
 * browser cascades it (themePalette.ts), and holds the numbers:
 *
 *   · text 7:1 and secondary text 4.5:1 on every surface it sits on — the
 *     window's three, the sidebar's two, the top bar's two (the bar's text
 *     4.5:1: it is labels and icons, not reading);
 *   · state colours 3:1 on every surface they sit on (a dot, a ring, a chip
 *     edge; WCAG 1.4.11), their chip ink 4.5:1, and the five apart from each
 *     other by eye;
 *   · the status inks read as text (pushed / failed / caution) 4.5:1;
 *   · each accent 3:1 as a ring, its button text 4.5:1, its text twin 4.5:1;
 *   · the terminal: foreground 7:1, every ANSI colour and selected text 4.5:1;
 *   · calm: no large surface bright or saturated (the actual complaint), and
 *     no dark-mode top bar or hairline bright enough to glare.
 *
 * A failure names the look and the pair, with both colours, so the fix is a
 * hex away. Colours are judged as they render: a var() chain or a color-mix()
 * resolves first. */

import { describe, expect, it } from "vitest";
import { ANSI_DARK, ANSI_LIGHT, SELECTION_DARK, SELECTION_LIGHT } from "../lib/termPalette";
import {
  ACCENT_KEYS,
  contrast,
  deltaE,
  isHex,
  mix,
  oklch,
  resolveLook,
  SURFACE_KEYS,
  THEME_CSS,
  type Mode,
} from "./themePalette";

const MODES: Mode[] = ["dark", "light"];

/** The state colours, by role. Labels always ride with them; this keeps the
 * colour itself honest too. */
const TONES = ["working", "attention", "waiting", "broken", "neutral", "muted"];

const look = (mode: Mode, surface: string) => `${surface || "swallow"}/${mode}`;

type Fail = string;

function pairCheck(
  fails: Fail[],
  tag: string,
  v: Record<string, string>,
  fg: string,
  bgs: string[],
  floor: number
) {
  for (const bg of bgs) {
    const a = v[fg];
    const b = v[bg];
    if (!isHex(a) || !isHex(b)) {
      fails.push(`${tag}: ${fg} on ${bg} does not resolve to a colour (${a} / ${b})`);
      continue;
    }
    const got = contrast(a, b);
    if (got < floor) fails.push(`${tag}: ${fg} on ${bg} ${got.toFixed(2)} < ${floor} (${a} on ${b})`);
  }
}

const WINDOW = ["--bg", "--panel", "--panel-2"];
const SIDEBAR = ["--sidebar-bg", "--sidebar-hi"];
const TOPBAR = ["--topbar-bg", "--topbar-hi"];

describe("theme.css is what the test walks", () => {
  it("reads the served sheet and finds the sets", () => {
    expect(THEME_CSS).toContain('html[data-surface="bluejay"]');
    expect(SURFACE_KEYS.length).toBeGreaterThanOrEqual(20);
    expect(ACCENT_KEYS.length).toBeGreaterThanOrEqual(10);
    // The default is a real look too, resolved from tokens.css alone.
    expect(resolveLook("dark", "")["--bg"]).toBe("#0f1117");
  });
});

describe("every surface set, both modes", () => {
  for (const mode of MODES) {
    for (const surface of SURFACE_KEYS) {
      const tag = look(mode, surface);
      const v = resolveLook(mode, surface);

      it(`${tag}: text and secondary text`, () => {
        const fails: Fail[] = [];
        pairCheck(fails, tag, v, "--text", WINDOW, 7);
        pairCheck(fails, tag, v, "--muted", WINDOW, 4.5);
        pairCheck(fails, tag, v, "--sidebar-text", SIDEBAR, 7);
        pairCheck(fails, tag, v, "--sidebar-muted", SIDEBAR, 4.5);
        pairCheck(fails, tag, v, "--topbar-text", TOPBAR, 4.5);
        pairCheck(fails, tag, v, "--topbar-muted", ["--topbar-bg"], 4.5);
        pairCheck(fails, tag, v, "--topbar-muted", ["--topbar-hi"], 3);
        // A hairline carries no text, but it must still be an edge.
        pairCheck(fails, tag, v, "--border", ["--bg"], 1.25);
        expect(fails).toEqual([]);
      });

      it(`${tag}: state colours stay visible and distinct`, () => {
        const fails: Fail[] = [];
        for (const t of TONES) {
          pairCheck(fails, tag, v, `--tone-${t}`, [...WINDOW, ...SIDEBAR], 3);
          pairCheck(fails, tag, v, `--tone-${t}-ink`, [`--tone-${t}`], 4.5);
        }
        // working / needs-you / waiting / failed / idle must not be confusable
        // with each other (ΔE ≥ 12 is plainly different, side by side).
        const five = TONES.slice(0, 5);
        five.forEach((a, i) =>
          five.slice(i + 1).forEach((b) => {
            const d = deltaE(v[`--tone-${a}`], v[`--tone-${b}`]);
            if (d < 12) fails.push(`${tag}: --tone-${a} vs --tone-${b} ΔE ${d.toFixed(1)} < 12`);
          })
        );
        // Status colours read as TEXT: pushed, failed, caution, the Sky's inks.
        for (const ink of ["--green", "--red", "--gold", "--fl-ink-working", "--fl-ink-red", "--fl-ink-muted"]) {
          pairCheck(fails, tag, v, ink, WINDOW, 4.5);
        }
        pairCheck(fails, tag, v, "--gold-ink", ["--gold-fill"], 4.5);
        expect(fails).toEqual([]);
      });

      it(`${tag}: the terminal`, () => {
        const fails: Fail[] = [];
        pairCheck(fails, tag, v, "--term-fg", ["--term-bg"], 7);
        const ansi = mode === "light" ? ANSI_LIGHT : ANSI_DARK;
        for (const [name, hex] of Object.entries(ansi)) {
          if (mode === "dark" && name === "black") continue; // meant to sit near the background
          const got = contrast(hex, v["--term-bg"]);
          if (got < 4.5) fails.push(`${tag}: ANSI ${name} ${got.toFixed(2)} < 4.5 (${hex} on ${v["--term-bg"]})`);
        }
        // Selected text: the wash is #rrggbbaa blended over the canvas.
        const sel = mode === "light" ? SELECTION_LIGHT : SELECTION_DARK;
        const alpha = parseInt(sel.slice(7, 9), 16) / 255;
        const washed = mix(sel.slice(0, 7), v["--term-bg"], alpha);
        const got = contrast(v["--term-fg"], washed);
        if (got < 4.5) fails.push(`${tag}: selected text ${got.toFixed(2)} < 4.5 (${v["--term-fg"]} on ${washed})`);
        expect(fails).toEqual([]);
      });

      it(`${tag}: calm — no bright or saturated large surface`, () => {
        const fails: Fail[] = [];
        const large = ["--bg", "--panel", "--sidebar-bg", "--term-bg"];
        for (const name of large) {
          const { L, C } = oklch(v[name]);
          if (mode === "dark" && (L > 0.3 || C > 0.09)) {
            fails.push(`${tag}: ${name} ${v[name]} is L ${L.toFixed(2)} C ${C.toFixed(3)} (dark surfaces: L ≤ 0.30, C ≤ 0.09)`);
          }
          if (mode === "light" && (L < 0.9 || C > 0.03)) {
            fails.push(`${tag}: ${name} ${v[name]} is L ${L.toFixed(2)} C ${C.toFixed(3)} (light surfaces: L ≥ 0.90, C ≤ 0.03)`);
          }
        }
        // The top bar may carry the bird's colour, but as a mid-dark band (white
        // text) or a pale strip — never a bright neon bar.
        const bar = oklch(v["--topbar-bg"]);
        const pale = bar.L >= 0.9 && bar.C <= 0.05;
        const band = bar.L <= 0.6;
        if (!(band || (mode === "light" && pale) || (mode === "dark" && bar.L <= 0.3))) {
          fails.push(`${tag}: --topbar-bg ${v["--topbar-bg"]} is L ${bar.L.toFixed(2)} C ${bar.C.toFixed(3)} — too bright for a bar`);
        }
        if (mode === "dark") {
          // Hairlines round every pane: a lemon or amber line at full
          // brightness vibrates against a dark page.
          for (const name of ["--border", "--sidebar-border"]) {
            const { L } = oklch(v[name]);
            if (L > 0.72) fails.push(`${tag}: ${name} ${v[name]} is L ${L.toFixed(2)} > 0.72 — a glaring hairline`);
          }
        }
        expect(fails).toEqual([]);
      });
    }
  }
});

describe("every accent, on every surface, both modes", () => {
  it("rings, button text and accent text hold", () => {
    const fails: Fail[] = [];
    for (const mode of MODES) {
      for (const accent of ACCENT_KEYS) {
        for (const surface of SURFACE_KEYS) {
          const tag = `${look(mode, surface)} + ${accent || "violetear"}`;
          const v = resolveLook(mode, surface, accent);
          // A focus ring or an active border: 3:1 against what it sits on.
          pairCheck(fails, tag, v, "--accent", ["--bg", "--panel"], 3);
          // A primary button's label.
          pairCheck(fails, tag, v, "--accent-ink", ["--accent"], 4.5);
          // The accent as text: the Sky's ink everywhere, --accent-deep by day.
          pairCheck(fails, tag, v, "--fl-ink-accent", WINDOW, 4.5);
          if (mode === "light") pairCheck(fails, tag, v, "--accent-deep", WINDOW, 4.5);
        }
      }
    }
    expect(fails).toEqual([]);
  });
});

describe("the picker shows every set", () => {
  it("no set in theme.css is missing a light-mode counterpart", () => {
    for (const s of SURFACE_KEYS.filter(Boolean)) {
      expect(THEME_CSS, s).toContain(`html.light[data-surface="${s}"]`);
    }
  });
});
