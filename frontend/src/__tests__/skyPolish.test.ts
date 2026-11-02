/** Round 17 polish (docs/flights.md "As built: round 17 polish-css") — the
 * Sky's manners in CSS only: ONE selection ring (2px accent, 2px out, screen
 * px at any zoom) on every selected line, box, object and the intake lane; a
 * fainter hover ring (1px, half the accent) and the keyboard's ring (1px, the
 * accent) as the same outline; one shadow scale (`--sky-shadow-rest`,
 * `--sky-shadow-raised` while dragged), defined once; radii unified (lines
 * 10, boxes 8, chips 999); 120ms transitions on transform / box-shadow /
 * outline-color / opacity only, none under reduced motion; 40px touch
 * targets that never resize anything; the drop gap's ring readable far out.
 * Read as text: the node environment can't lay CSS out. */

import { describe, expect, it } from "vitest";
import { css, cssFilesWith, cssRule, squash } from "./sourceText";

const F = "components/flights/";
const FENCE = [
  "SkyLine.css",
  "FlywayCanvas.css",
  "GroundedLine.css",
  "IntakeLane.css",
  "LandingStrip.css",
  "SkyObjects.css",
  "StickyNote.css",
  "SkySelection.css",
  "SkyPalette.css",
  "StopShelf.css",
  "StopEditing.css",
  "StopParts.css",
].map((f) => F + f);

const canvas = css(F + "FlywayCanvas.css");
const line = css(F + "SkyLine.css");
const select = css(F + "SkySelection.css");
const objects = css(F + "SkyObjects.css");
const note = css(F + "StickyNote.css");
const lane = css(F + "IntakeLane.css");
const palette = css(F + "SkyPalette.css");
const grounded = css(F + "GroundedLine.css");
const shelf = css(F + "StopShelf.css");

/** The body of the rule whose selector is exactly `sel` — for selectors with
 * commas inside `:is(…)`, which `cssRule` splits. */
const ruleOf = (sheet: string, sel: string): string => {
  const flat = squash(sheet);
  const at = flat.indexOf(squash(sel) + " {");
  if (at < 0) return "";
  return flat.slice(flat.indexOf("{", at) + 1, flat.indexOf("}", at)).trim();
};

const RING = "outline: calc(2px / var(--sky-z, 1)) solid var(--accent)";
const OFFSET = "outline-offset: calc(2px / var(--sky-z, 1))";
const REST_RING = "outline: calc(1px / var(--sky-z, 1)) solid transparent";
const HOVER = "outline-color: color-mix(in srgb, var(--accent) 50%, transparent)";

describe("one shadow scale", () => {
  it("--sky-shadow-rest and --sky-shadow-raised are defined once, on the sky and any canvas, softer in light", () => {
    expect(cssFilesWith("--sky-shadow-rest:")).toEqual([F + "FlywayCanvas.css"]);
    expect(cssFilesWith("--sky-shadow-raised:")).toEqual([F + "FlywayCanvas.css"]);
    const tokens = cssRule(canvas, ".sky");
    expect(tokens).toMatch(/--sky-shadow-rest: [^;]+;/);
    expect(tokens).toMatch(/--sky-shadow-raised: [^;]+;/);
    expect(cssRule(canvas, ".fl-canvas")).toContain("--sky-shadow-rest:");
    expect(cssRule(canvas, ".light .sky")).toMatch(/--sky-shadow-rest: [^;]+;[^}]*--sky-shadow-raised: /);
  });

  it("every line, box and object draws the resting shadow", () => {
    expect(cssRule(line, ".skl")).toContain("box-shadow: var(--sky-shadow-rest)");
    expect(cssRule(line, ".light .skl")).toContain("box-shadow: var(--sky-shadow-rest)");
    expect(cssRule(canvas, ".fl-box")).toContain("box-shadow: var(--sky-shadow-rest)");
    expect(cssRule(objects, ".sky-block")).toContain("box-shadow: var(--sky-shadow-rest)");
    expect(cssRule(note, ".sky-note")).toContain("box-shadow: var(--sky-shadow-rest)");
    expect(cssRule(note, ".dark .sky-note")).toContain("box-shadow: var(--sky-shadow-rest)");
    expect(cssRule(palette, ".sky-palette")).toContain("box-shadow: var(--sky-shadow-rest)");
    for (const [sheet, sel] of [
      [line, ".skl"],
      [canvas, ".fl-box"],
      [objects, ".sky-block"],
      [note, ".sky-note"],
      [palette, ".sky-palette"],
    ] as const)
      expect(cssRule(sheet, sel), sel).not.toMatch(/box-shadow:[^;]*rgba\(/);
  });

  it("raised while dragged: a line's slot, an object, the lane, the floating palette, a lifted box", () => {
    expect(cssRule(line, ".sky-line-slot.is-dragging > .skl")).toBe("box-shadow: var(--sky-shadow-raised);");
    expect(ruleOf(objects, ".sky-object.is-dragging > :is(.sky-block, .sky-note, .sky-lane)")).toBe("box-shadow: var(--sky-shadow-raised);");
    expect(cssRule(lane, ".sky .sky-intake.is-dragging > .sky-lane")).toBe("box-shadow: var(--sky-shadow-raised);");
    expect(cssRule(palette, ".sky-palette-slot.is-dragging .sky-palette")).toBe("box-shadow: var(--sky-shadow-raised);");
    expect(cssRule(canvas, ".fl-lifted")).toContain("box-shadow: var(--sky-shadow-raised)");
    expect(cssRule(canvas, ".fl-box.fl-lifted")).toContain("outline-color: var(--accent)");
  });
});

describe("one ring", () => {
  it("the selection ring is ONE rule over every selected thing: 2px of the accent, 2px out, solid, screen-sized", () => {
    const sels = [".skl.is-selected", ".skl.is-focused", ".fl-box.selected", ".fl-box.fl-box-selected", ".sky-object.is-selected", ".sky .sky-intake.is-selected > .sky-lane"];
    const m = /([^{}]+)\{([^{}]*)\}/g;
    const owner = [...select.matchAll(m)].find(([, s]) => s.includes(".sky-object.is-selected"));
    expect(owner).toBeTruthy();
    const list = owner![1].split(",").map((s) => s.trim());
    expect(list).toEqual(sels);
    expect(owner![2]).toContain(RING);
    expect(owner![2]).toContain(OFFSET);
    // No dashed selection left in the round's sheets.
    for (const f of FENCE) expect(css(f), f).not.toMatch(/is-selected[^{]*\{[^}]*dashed/);
    expect(cssRule(select, ".fl-box.fl-box-selected")).not.toContain("dashed");
  });

  it("every ringed thing wears the ring transparent at rest, so only its colour and width change", () => {
    for (const [sheet, sel] of [
      [canvas, ".fl-box"],
      [line, ".skl"],
      [objects, ".sky-object"],
      [lane, ".sky .sky-intake > .sky-lane"],
    ] as const) {
      const rule = cssRule(sheet, sel);
      expect(rule, sel).toContain(REST_RING);
      expect(rule, sel).toContain("outline-offset: calc(2px / var(--sky-z, 1))");
    }
    // The old outline: none (which hid the selection under a pointer focus) is gone.
    expect(cssRule(canvas, ".fl-box")).not.toContain("outline: none");
    expect(line).not.toMatch(/\.skl:focus:not\(:focus-visible\) \{[^}]*outline: none/);
    expect(cssRule(line, ".skl:where(:focus:not(:focus-visible))")).toBe("outline-color: transparent;");
  });

  it("hover: 1px at half the accent, at zero weight so the selection always wins — the box, the line's bar, an object's head, the lane's head", () => {
    expect(cssRule(canvas, ".fl-box:where(:not(.fl-box-open):hover)")).toBe(HOVER + ";");
    expect(cssRule(line, ".skl:where(:has(> .skl-bar:hover))")).toBe(HOVER + ";");
    expect(ruleOf(objects, ".sky-object:where(:has(:is(.sky-block-head, .sky-note-head, .sky-lane-head):hover))")).toBe(HOVER + ";");
    expect(cssRule(lane, ".sky .sky-intake > .sky-lane:where(:has(> .sky-lane-head:hover))")).toBe(HOVER + ";");
  });

  it(":focus-visible is the hover ring in the selection's colour (no second box-shadow ring)", () => {
    expect(cssRule(canvas, ".fl-box:focus-visible")).toContain("outline-color: var(--accent)");
    expect(cssRule(canvas, ".fl-box:focus-visible")).not.toContain("box-shadow");
    expect(cssRule(line, ".skl:focus-visible")).toBe("outline-color: var(--accent);");
    expect(cssRule(grounded, ".skl.skl-grounded:focus-visible")).toContain("outline-color: var(--accent)");
    expect(cssRule(grounded, ".skl.skl-grounded:focus-visible")).not.toMatch(/outline: none|box-shadow/);
    expect(cssRule(objects, ".sky-object:where(:has(:focus-visible))")).toBe("outline-color: var(--accent);");
    expect(cssRule(note, ".sky-object:has(> .sky-note:focus)")).toBe("outline-color: var(--accent);");
    expect(cssRule(line, ".skl.is-focused")).not.toContain("box-shadow");
  });
});

describe("radii: lines 10, boxes 8, chips 999", () => {
  it("as the contract says", () => {
    expect(cssRule(line, ".skl")).toContain("border-radius: 10px");
    expect(cssRule(canvas, ".fl-box")).toContain("border-radius: 8px");
    expect(cssRule(line, ".skl .fl-box:not(.fl-box-open)")).toContain("border-radius: 8px");
    expect(cssRule(line, ".skl .fl-box.fl-box-branch:not(.fl-box-open)")).toContain("border-radius: 8px");
    expect(cssRule(grounded, ".skl-grounded-head")).toContain("border-radius: 8px");
    expect(cssRule(objects, ".sky-block")).toContain("border-radius: 8px");
    expect(cssRule(objects, ".sky-object")).toContain("border-radius: 8px");
    expect(cssRule(note, ".sky-note")).toContain("border-radius: 8px");
    expect(cssRule(lane, ".sky-lane-block")).toContain("border-radius: 8px");
    expect(cssRule(palette, ".sky-palette")).toContain("border-radius: 10px");
    expect(cssRule(shelf, ".sky-stop-chip")).toContain("border-radius: 999px");
    expect(cssRule(canvas, ".fl-box-chip")).toContain("border-radius: 999px");
  });
});

describe("motion", () => {
  const TRANSITIONS = /transition:\s*([^;]+);/g;

  it("the round's transitions are 120ms on transform / box-shadow / outline-color / opacity only", () => {
    expect(cssRule(canvas, ".fl-box")).toContain("transition: transform 120ms ease, box-shadow 120ms ease, outline-color 120ms ease, opacity 120ms ease");
    expect(cssRule(line, ".skl")).toContain("transition: box-shadow 120ms ease, outline-color 120ms ease, opacity 120ms ease");
    expect(cssRule(objects, ".sky-object")).toContain("transition: outline-color 120ms ease");
    for (const f of FENCE) {
      for (const [, v] of css(f).matchAll(TRANSITIONS)) {
        if (!v.includes("120ms")) continue;
        for (const part of v.split(",")) expect(part.trim().split(/\s+/)[0], f + ": " + v).toMatch(/^(transform|box-shadow|outline-color|opacity)$/);
      }
    }
  });

  it("nothing in the round's sheets transitions a size (width / height / margin / padding)", () => {
    for (const f of FENCE) for (const [, v] of css(f).matchAll(TRANSITIONS)) expect(v, f).not.toMatch(/\b(width|height|margin|padding|all)\b/);
  });

  it("every sheet with a 120ms transition turns it off under prefers-reduced-motion", () => {
    for (const f of FENCE) {
      const text = css(f);
      if (!text.includes("120ms")) continue;
      expect(text, f).toMatch(/@media \(prefers-reduced-motion: reduce\) \{[\s\S]*?transition: none;/);
    }
    expect(line).toMatch(/@media \(prefers-reduced-motion: reduce\) \{\s*\.skl,\s*\.skl-box-resize \{\s*transition: none;/);
    expect(objects).toMatch(/@media \(prefers-reduced-motion: reduce\) \{\s*\.sky-object,\s*\.sky-block \{\s*transition: none;/);
    expect(canvas).toMatch(/@media \(prefers-reduced-motion: reduce\) \{[^@]*\.fl-box,\s*\.fl-wire \{\s*transition: none;/);
  });
});

describe("touch and the drop", () => {
  it("on a touch screen every control has a 40px hit area that resizes nothing (a centred ::after)", () => {
    const m = /@media \(hover: none\) \{\s*:is\(([^)]*(?:\([^)]*\)[^)]*)*)\) \{\s*position: relative;\s*\}\s*:is\(([^{]*)\)::after \{([^}]*)\}/.exec(canvas);
    expect(m).toBeTruthy();
    const [, flow, all, body] = m!;
    for (const c of [".skl-size", ".skl-card-fold", ".fl-tool", ".fl-icon-btn", ".sky-stop-chip", ".sky-lane-toggle", ".sky-shelf-fold", ".sky-selbar button", ".sky .fl-btn"]) {
      expect(flow, c).toContain(c);
      expect(all, c).toContain(c);
    }
    // The absolutely placed handles are already a containing block: never made relative.
    for (const h of [".skl-box-resize", ".skl-resize", ".sky-palette-resize", ".fl-gap-add", ".fl-bird-marker"]) {
      expect(all, h).toContain(h);
      expect(flow, h).not.toContain(h);
    }
    expect(body).toContain('content: ""');
    expect(body).toContain("position: absolute");
    expect(body).toContain("min-width: 40px");
    expect(body).toContain("min-height: 40px");
    expect(body).toContain("transform: translate(-50%, -50%)");
  });

  it("the drop gap reads at every band, far too: a 2px accent ring over a 2px panel gap; the end + keeps 2 screen px far out", () => {
    expect(cssRule(canvas, ".fl-gap-add.fl-gap-lit")).toContain("box-shadow: 0 0 0 2px var(--panel), 0 0 0 4px var(--accent)");
    expect(cssRule(canvas, ".fl-gap-add.fl-gap-lit")).not.toContain("0.25)");
    const far = cssRule(line, '.skl[data-lod="far"] .fl-box-add.fl-drop');
    expect(far).toContain("box-shadow: 0 0 0 calc(2px / var(--sky-z, 1)) var(--accent)");
    expect(far).toContain("background: rgba(var(--accent-rgb), 0.3)");
  });
});
