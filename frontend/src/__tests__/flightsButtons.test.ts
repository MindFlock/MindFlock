/**
 * The flights' buttons wear the app's tiers (docs/flights.md "As built:
 * round 11 style"). The owner: "the tidy button and a few other buttons are
 * not in the correct style." Two buttons had no style at all — Tidy (`.fl-btn`
 * only sets white-space) and the line picker's `.fl-chip` (no rule anywhere) —
 * and NewSessionBlock's Cancel named a `.btn-secondary` that never existed.
 * These pin every <button in components/flights to a tier and the toolbar's
 * rules to the secondary tier's geometry.
 */
import { describe, expect, it } from "vitest";
import { SRC, attr, css, cssRule, tagsWith } from "./sourceText";

/** A tier class, or a class whose own rules wear one (where it lives). */
const TIERS: Record<string, string> = {
  // primary — .btn-primary (settings/screens/screens.css)
  "btn-primary": "primary",
  // secondary — .test-btn (dialogs/SetupDialog.css) and the classes built on it
  "test-btn": "secondary",
  "sky-tidy": "secondary (Sky.css)",
  "sky-zoom-btn": "secondary, segmented (Sky.css)",
  "sky-stop-chip": "secondary (StopShelf.css)",
  // chips — .fl-pick (FlywayCanvas.css)
  "fl-pick": "chip",
  "fl-chip": "chip (Sky.css .sky-flypick)",
  "fl-toggle": "chip, a switch",
  // quiet-icon — .fl-icon-btn (StopParts.css), the top bar's .tb-item
  "fl-icon-btn": "quiet",
  "fl-gap-add": "quiet, accent-edged (FlywayCanvas.css)",
  "skl-size": "quiet (SkyLine.css)",
  "skl-card-fold": "quiet (SkyLine.css)",
  "sky-new-close": "quiet (NewSessionBlock.css)",
  "sky-lane-toggle": "quiet (IntakeLane.css)",
  "sky-lane-more": "quiet (IntakeLane.css)",
  "sky-lane-rail-icon": "quiet, a kind badge (IntakeLane.css)",
  "sky-shelf-fold": "quiet, a tab (StopShelf.css)",
  "sky-drawer-tab": "quiet, a tab (Sky.css)",
  // menu rows — .vf-menu-item (dialogs/VerifyDialog.css)
  "fl-stopmenu-item": "menu row (StopMenu.css)",
  // links — button.linklike (dialogs/SetupDialog.css)
  linklike: "link",
  "sky-perch-name": "link, a name (Sky.css)",
  "fl-strip-line": "link, a strip's line (Flights.css)",
  // documented exceptions: whole rows / cards that are one button
  "fl-check-row": "a card row (Flights.css)",
  "fl-palette-item": "a palette card (StopParts.css)",
};

const FLIGHTS = Object.entries(SRC).filter(([p]) => p.startsWith("components/flights/") && p.endsWith(".tsx"));

/** A className's classes: a plain string, or an expression's string literals. */
const classesOf = (value: string): string[] =>
  (value.includes('"') ? [...value.matchAll(/"([^"]*)"/g)].map((m) => m[1]) : [value]).flatMap((v) => v.split(/\s+/)).filter(Boolean);

describe("every flights button wears a tier", () => {
  for (const [path, src] of FLIGHTS) {
    const opens = (src.match(/<button\b/g) || []).length;
    if (!opens) continue;
    it(path, () => {
      const tags = [...tagsWith(src, "type", "button"), ...tagsWith(src, "type", "submit")].filter((t) => t.name === "button");
      expect(tags.length).toBe(opens);
      for (const tag of tags) {
        const value = attr(tag, "className");
        if (value === undefined) {
          // StopShelf's chips spread a props object whose className is the chip tier.
          expect(tag.text, path + ": a button with no class").toContain("{...props}");
          continue;
        }
        const tiers = classesOf(value).filter((c) => c in TIERS);
        expect(tiers, path + ": " + tag.text.slice(0, 120)).not.toEqual([]);
      }
    });
  }

  it("round 13: the palette's and the catalog's buttons name their tiers", () => {
    const palette = SRC["components/flights/SkyPalette.tsx"];
    // Round 14: the palette IS all the blocks — its All blocks… went; the ×
    // that returns to the shelf is the quiet tier.
    expect(palette).not.toContain("sky-palette-all");
    expect(palette).toContain('className="fl-icon-btn sky-palette-close"');
    const catalog = SRC["components/flights/StopCatalog.tsx"];
    expect(catalog).toContain('className="test-btn sky-catalog-add"');
    expect(catalog).toMatch(/className=\{"fl-icon-btn sky-catalog-shelf"/);
    // ⤢ Float the blocks: the fold handle's quiet tab.
    expect(SRC["components/flights/StopShelf.tsx"]).toContain('className="sky-shelf-fold sky-shelf-float"');
    // The ⋯ are the house OverflowMenu (a <summary>, the quiet .vf-menu tier).
    expect(palette).toContain("<OverflowMenu");
    expect(catalog).toContain("<OverflowMenu");
  });

  it("the spread chips are the chip tier", () => {
    const shelf = SRC["components/flights/StopShelf.tsx"];
    expect(shelf.match(/className: "sky-stop-chip[^"]*"/g)?.length).toBeGreaterThanOrEqual(3);
  });

  it("no class that exists nowhere (.btn-secondary was Cancel's)", () => {
    for (const [path, src] of FLIGHTS) expect(src, path).not.toMatch(/\bbtn-secondary\b/);
  });

  it("the dialogs name their tiers: every action is test-btn or btn-primary", () => {
    for (const f of ["StopDialog.tsx", "TakeoffDialog.tsx", "NewSessionBlock.tsx"]) {
      const src = SRC["components/flights/" + f];
      const tags = [...tagsWith(src, "type", "button"), ...tagsWith(src, "type", "submit")].filter((t) => t.name === "button");
      const actions = tags.filter((t) => !/fl-icon-btn|sky-new-close/.test(t.text));
      for (const t of actions) expect(t.text, f).toMatch(/className="[^"]*\b(test-btn|btn-primary)\b/);
    }
  });
});

describe("the toolbar is the secondary tier", () => {
  const sky = css("components/flights/Sky.css");
  const canvas = css("components/flights/FlywayCanvas.css");
  const testBtn = cssRule(css("components/dialogs/SetupDialog.css"), ".test-btn");
  const primary = cssRule(css("components/settings/screens/screens.css"), ".btn-primary");

  it("the tiers' radius and the toolbar's height are one token each", () => {
    expect(testBtn).toContain("border-radius: 6px");
    expect(primary).toContain("border-radius: 6px");
    expect(canvas).toMatch(/--fl-btn-radius: 6px;/);
    expect(canvas).toMatch(/--fl-ctl-h: 28px;/);
  });

  it("Tidy: .test-btn's fill and border, the shared radius and height", () => {
    const tidy = cssRule(sky, ".sky-toolbar .fl-btn.sky-tidy");
    expect(tidy).toContain("background: var(--panel-2)");
    expect(tidy).toContain("border: 1px solid var(--border)");
    expect(tidy).toContain("border-radius: var(--fl-btn-radius)");
    expect(tidy).toContain("height: var(--fl-ctl-h)");
    expect(cssRule(sky, ".sky-toolbar .fl-btn.sky-tidy:hover")).toContain("border-color: var(--accent)");
  });

  it("zoom: one bordered group in Tidy's fill, radius and height, divided keys", () => {
    const zoom = cssRule(sky, ".sky-zoom");
    expect(zoom).toContain("background: var(--panel-2)");
    expect(zoom).toContain("border: 1px solid var(--border)");
    expect(zoom).toContain("border-radius: var(--fl-btn-radius)");
    expect(zoom).toContain("height: var(--fl-ctl-h)");
    expect(cssRule(sky, ".sky-toolbar .sky-zoom > * + *")).toContain("border-left: 1px solid var(--border)");
    expect(cssRule(sky, ".sky-toolbar .sky-zoom-btn")).toContain("border: 0");
  });

  it("the mode chips: the top bar's Grid · Sky pair, at the same height", () => {
    const modes = cssRule(sky, ".sky-modes.fl-chiprow");
    expect(modes).toContain("height: var(--fl-ctl-h)");
    expect(modes).toContain("border-radius: var(--fl-btn-radius)");
    expect(cssRule(sky, ".sky-toolbar .sky-modes .fl-pick.on")).toContain("background: var(--panel-2)");
  });

  it("the line picker's chips have a rule (they had none)", () => {
    expect(cssRule(sky, ".sky-flypick .fl-chip")).toContain("border-radius: 999px");
  });
});

describe("quiet-icon: the house hover and the accent ring", () => {
  const ring = /outline: 2px solid var\(--accent\)/;
  const cases: Array<[string, string]> = [
    ["components/flights/StopParts.css", ".fl-icon-btn"],
    ["components/flights/FlywayCanvas.css", ".fl-tool"],
    ["components/flights/FlywayCanvas.css", ".fl-gap-add"],
    ["components/flights/SkyLine.css", ".skl-size"],
    ["components/flights/SkyLine.css", ".skl-card-fold"],
    ["components/flights/NewSessionBlock.css", ".sky-new-close"],
    ["components/flights/IntakeLane.css", ".sky-lane-toggle"],
    ["components/flights/StopShelf.css", ".sky-shelf-fold"],
    ["components/flights/StopShelf.css", ".sky-stop-chip"],
    ["components/flights/SkyPalette.css", ".sky-palette-resize"],
    ["components/flights/Sky.css", ".sky-toolbar .fl-btn.sky-tidy"],
    ["components/flights/Sky.css", ".sky-toolbar .sky-zoom-btn"],
  ];
  for (const [file, sel] of cases) {
    it(sel + " rings on focus", () => {
      expect(cssRule(css(file), sel + ":focus-visible")).toMatch(ring);
    });
  }

  it("the glyph buttons share the tiers' radius", () => {
    expect(cssRule(css("components/flights/StopParts.css"), ".fl-icon-btn")).toContain("border-radius: var(--fl-btn-radius, 6px)");
    expect(cssRule(css("components/flights/FlywayCanvas.css"), ".fl-tool")).toContain("border-radius: var(--fl-btn-radius, 6px)");
    expect(cssRule(css("components/flights/SkyLine.css"), ".skl-size")).toContain("border-radius: var(--fl-btn-radius, 6px)");
  });

  it("the stop menu's rows are .vf-menu-item's", () => {
    const row = cssRule(css("components/flights/StopMenu.css"), ".fl-stopmenu-item");
    const vf = cssRule(css("components/dialogs/VerifyDialog.css"), ".vf-menu-list .vf-menu-item");
    for (const d of ["padding: 5px 9px", "border-radius: 5px", "font-size: 12px", "line-height: 1.5"]) {
      expect(vf).toContain(d);
      expect(row).toContain(d);
    }
    expect(cssRule(css("components/flights/StopMenu.css"), ".fl-stopmenu-item:hover:not(:disabled)")).toContain("background: var(--panel-2)");
  });
});

describe("both themes: accent text holds 4.5:1", () => {
  const canvas = css("components/flights/FlywayCanvas.css");
  it("--fl-ink-accent: lighter on dark, the deep twin on light", () => {
    expect(cssRule(canvas, ":root")).toMatch(/--fl-ink-accent: color-mix\(in srgb, var\(--accent\) 62%, #fff\)/);
    expect(cssRule(canvas, ".light")).toContain("--fl-ink-accent: var(--accent-deep)");
  });

  it("the + and the flights' links use it, not the raw accent", () => {
    expect(cssRule(canvas, ".fl-box-add")).toContain("color: var(--fl-ink-accent)");
    expect(cssRule(canvas, ".fl-gap-add")).toContain("color: var(--fl-ink-accent)");
    expect(cssRule(css("components/flights/Flights.css"), ".sky button.linklike")).toContain("var(--fl-ink-accent");
  });

  it("the Session chip is primary-tinted, the rename field the house input", () => {
    const shelf = css("components/flights/StopShelf.css");
    expect(cssRule(shelf, ".sky-stop-chip.sky-session-chip")).toContain("background: rgba(var(--accent-rgb), 0.16)");
    // The field moved to StopCatalog's PlanNameField (the shelf's chips and the catalog's cards share it).
    expect(SRC["components/flights/StopCatalog.tsx"]).toContain('className="sky-saved-rename fl-input"');
  });
});
