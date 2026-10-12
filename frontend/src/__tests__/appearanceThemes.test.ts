/** Settings → Appearance: selectable accent + surface themes. Palettes live in
 * theme.css (served as-is, checked in Python); this pins the React half — the
 * screen renders one swatch per preset plus the built-in default, a choice
 * persists to the server and the local pre-paint cache, the top-bar glyphs are
 * monochrome so they survive every region hue, and the base stylesheet
 * declares the region tokens every multi-color set steers.
 *
 * Moved from tests/unit/test_appearance_accent.py, which grepped the built
 * app.js / style.css. The swatch screen is rendered (react-dom/server; the
 * vitest environment is node); the stylesheet checks read the owning source
 * sheet. mobile.js / mobile.css / theme.css stay in Python: they are
 * hand-written and served as-is. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { ALL_CODE, ALL_CSS, code, src } from "./sourceText";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})) }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { Appearance } = await import("../components/settings/screens/Appearance");

// One bird per color — every color exists in BOTH dimensions under one key;
// Swallow (the default) and the multi-hue sets are surface-only.
const COLORS = [
  "cardinal",
  "oriole",
  "goldfinch",
  "quetzal",
  "bluejay",
  "bunting",
  "violetear",
  "flamingo",
  "pheasant",
  "heron",
];
const SURFACE_ONLY = [
  "raven",
  "macaw",
  "lorikeet",
  "roller",
  "toucan",
  "greenjay",
  "gouldian",
  "mallard",
  "kingfisher",
  "peacock",
  "mandarin",
];
const ACCENTS = COLORS;
const SURFACES = [...COLORS, ...SURFACE_ONLY];

const REGION_VARS = ["topbar", "sidebar"].flatMap((r) =>
  ["bg", "hi", "border", "text", "muted"].map((part) => `--${r}-${part}`)
);
const APP_ALIAS_VARS = ["--app-panel", "--app-panel-2", "--app-border", "--app-text", "--app-muted"];

function renderScreen(): string {
  const g = globalThis as Record<string, unknown>;
  const saved = g.document;
  g.document = { documentElement: { getAttribute: () => null } };
  try {
    return renderToStaticMarkup(createElement(Appearance, {} as never));
  } finally {
    g.document = saved;
  }
}

describe("the Appearance screen", () => {
  const html = renderScreen();

  it("is a settings screen with both swatch rows", () => {
    expect(src("components/settings/SettingsDialog.tsx")).toMatch(
      /\{ key: "appearance", label: "Appearance",/
    );
    expect(html).toContain('id="accent-swatches"');
    expect(html).toContain('id="surface-swatches"');
  });

  it("renders a swatch per preset, plus a default that clears the override", () => {
    // Empty choice = the built-in look.
    expect(html).toContain('data-accent-choice=""');
    expect(html).toContain('data-surface-choice=""');
    // Violetear IS the built-in accent, so its swatch is the default one.
    expect(html).toMatch(/data-accent-choice=""[^>]*>.*?Violetear \(default\)/);
    for (const name of ACCENTS.filter((n) => n !== "violetear")) {
      expect(html, name).toContain(`data-accent-choice="${name}"`);
    }
    for (const name of SURFACES) expect(html, name).toContain(`data-surface-choice="${name}"`);
  });

  it("persists a choice to the server (all devices) and the pre-paint cache", () => {
    const screen = src("components/settings/screens/Appearance.tsx");
    expect(screen).toContain('api("/api/settings"');
    expect(screen).toContain('"cs_surface"');
  });

  it("terminal canvas/cursor follow the live CSS vars", () => {
    const terms = src("lib/terminals.ts");
    expect(terms).toContain("function cssVar(");
    expect(terms).toContain('"--term-bg"');
    expect(terms).toContain('"--term-fg"');
  });

  it("the retired-name migration map is gone", () => {
    expect(ALL_CODE).not.toContain("LEGACY_PRESETS");
  });
});

describe("top-bar glyphs are monochrome, never emoji", () => {
  /** A button's own markup plus the body of any *Glyph component it renders —
   * a glyph used in more than one place gets hoisted into its own component,
   * so resolve that one hop before checking what paints it. */
  function buttonGlyphMarkup(file: string, id: string): string {
    const text = code(file);
    const at = text.indexOf(`id="${id}"`);
    expect(at, id).toBeGreaterThanOrEqual(0);
    const button = text.slice(at, text.indexOf("</button>", at));
    const parts = [button];
    for (const name of new Set([...button.matchAll(/<(\w+Glyph)\b/g)].map((m) => m[1]))) {
      const decl = `function ${name}(`;
      expect(text, `${id} renders <${name}/> but ${file} does not declare it`).toContain(decl);
      const body = text.slice(text.indexOf(decl));
      parts.push(body.slice(0, body.indexOf("\n}\n")));
    }
    return parts.join("\n");
  }

  it("the theme toggle and the bell paint with currentColor", () => {
    // An emoji paints its own colors, so 🌙/🔔 vanish on Goldfinch's lemon bar
    // or Toucan's amber one; currentColor follows the bar's rebound --text.
    const theme = buttonGlyphMarkup("components/TopBar.tsx", "theme-btn");
    expect(theme).not.toContain("🌙");
    expect(theme).not.toContain("☀");
    expect(theme).toContain('"currentColor"');
    const bell = buttonGlyphMarkup("components/NotificationsBell.tsx", "notif-btn");
    expect(bell).toContain("BellGlyph");
    expect(bell).not.toContain("🔔");
    expect(bell).toContain('"currentColor"');
  });
});

describe("the base palette", () => {
  const tokens = src("styles/tokens.css");
  const block = (css: string, scope: string) => css.split(scope, 2)[1].split("}", 1)[0];

  it("region tokens alias the globals on :root, never repeated in .light", () => {
    const root = block(tokens, ":root {");
    for (const v of [...REGION_VARS, ...APP_ALIAS_VARS]) expect(root, v).toContain(v + ":");
    // A custom property resolves its var()s against the value that won on the
    // same element, so one declaration on :root already picks up the light
    // palette. A copy in .light would rot.
    const light = block(tokens, ".light {");
    for (const v of REGION_VARS) expect(light, v).not.toContain(v + ":");
  });

  it("each region rebinds the generic surface tokens on itself", () => {
    // That's what makes every control inside a region follow its hue with no
    // per-component CSS.
    const regions = src("styles/regions.css");
    for (const region of ["topbar", "sidebar"]) {
      const m = regions.match(
        new RegExp(`#${region}\\s*\\{([^}]*--panel:\\s*var\\(--${region}-bg\\)[^}]*)\\}`)
      );
      expect(m, `regions.css does not rebind --panel on #${region}`).toBeTruthy();
      for (const [generic, token] of [
        ["--panel-2", "hi"],
        ["--border", "border"],
        ["--text", "text"],
        ["--muted", "muted"],
      ]) {
        expect(m![1]).toContain(`${generic}: var(--${region}-${token})`);
      }
    }
    // Popovers floating out of a recolored region opt back into the app palette.
    expect(regions).toContain("--panel: var(--app-panel)");
  });

  it(":root and .light carry the accent companions and terminal vars", () => {
    // So rgba() glows, light-mode deep text and xterm all resolve per theme.
    for (const scope of [":root {", ".light {"]) {
      const b = block(tokens, scope);
      for (const v of ["--accent-rgb:", "--accent-deep:", "--term-bg:", "--term-fg:"]) {
        expect(b, `${scope} ${v}`).toContain(v);
      }
    }
    // No accent-purple glow may remain hard-coded (it would ignore the theme).
    expect(ALL_CSS).not.toContain("rgba(125, 86, 244");
  });
});
