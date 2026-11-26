import { describe, expect, it } from "vitest";

/* The shape and control scale (styles/tokens.css "Shape and control scale").
 * A ux-sweep of the app counted 11 corner radii, 44 paddings and 29 heights
 * across its buttons; the radii became six tokens, the buttons four sizes.
 * These keep a literal from creeping back in. */

const sheets = Object.entries(
  import.meta.glob<string>("../**/*.css", { query: "?raw", import: "default", eager: true })
).map(([path, text]) => ({ path: path.replace(/^\.\.\//, ""), text: text.replace(/\/\*[\s\S]*?\*\//g, "") }));

const MOBILE_CSS = Object.values(
  import.meta.glob<string>("../../../backend/web/static/mobile.css", { query: "?raw", import: "default", eager: true })
)[0];

const tokens = sheets.find((s) => s.path === "styles/tokens.css")!.text;

const SCALE: Record<string, string> = {
  "--radius-sm": "4px",
  "--radius": "6px",
  "--radius-md": "8px",
  "--radius-lg": "12px",
  "--radius-xl": "16px",
  "--radius-pill": "999px",
  "--ctl-pad-xs": "1px 6px",
  "--ctl-pad-sm": "3px 10px",
  "--ctl-pad": "5px 12px",
  "--ctl-pad-lg": "7px 16px",
  "--ctl-h-xs": "20px",
  "--ctl-h-sm": "24px",
  "--ctl-h": "28px",
  "--ctl-h-lg": "32px",
};

describe("the shape and control scale", () => {
  it("is declared once in tokens.css, and mobile.css copies it by value", () => {
    for (const [name, value] of Object.entries(SCALE)) {
      expect(tokens).toContain(`${name}: ${value};`);
      expect(MOBILE_CSS).toContain(`${name}: ${value};`);
    }
  });

  it("no stylesheet writes a corner radius as a literal between 2px and 20px", () => {
    const bad: string[] = [];
    for (const { path, text } of [...sheets, { path: "mobile.css", text: MOBILE_CSS }]) {
      for (const m of text.matchAll(/border(?:-[a-z]+)?-radius:\s*([^;}]+)/g)) {
        if (/(?<![\w.-])([2-9]|1\d|20)px/.test(m[1])) bad.push(`${path}: ${m[0]}`);
      }
    }
    expect(bad).toEqual([]);
  });

  it("a button with no size of its own takes 13px at one line height, not the browser's", () => {
    const base = sheets.find((s) => s.path === "styles/base.css")!.text;
    expect(base).toMatch(/\nbutton \{\s*font-size: 13px;\s*line-height: 1\.2;\s*\}/);
  });

  it("each control size pairs its padding with its height", () => {
    const pairs = { "--ctl-pad-xs": "--ctl-h-xs", "--ctl-pad-sm": "--ctl-h-sm", "--ctl-pad-lg": "--ctl-h-lg" };
    let seen = 0;
    for (const { text } of sheets) {
      for (const rule of text.matchAll(/\{[^{}]*padding: var\((--ctl-pad(?:-xs|-sm|-lg)?)\)[^{}]*\}/g)) {
        const pad = rule[1] as keyof typeof pairs | "--ctl-pad";
        const h = pad === "--ctl-pad" ? "--ctl-h" : pairs[pad];
        // a rule that sets its own height (or sits in a flex row that does)
        // may pad without the min-height; most take both
        if (rule[0].includes(`min-height: var(${h})`)) seen++;
        else expect(rule[0]).toMatch(/min-height: var\(--ctl-h|height:|display: (inline-)?(flex|grid)/);
      }
    }
    expect(seen).toBeGreaterThan(100);
  });

  it("the sidebar's View row is one height: the number chips and Grid · Sky", () => {
    const row = sheets.filter((s) => s.path.startsWith("components/sidebar/")).map((s) => s.text).join("\n");
    expect(row).toMatch(/#view-modes \.vm \{[^}]*min-height: var\(--ctl-h-sm\)/);
    expect(row).toMatch(/#view-modes \.vt \{[^}]*min-height: var\(--ctl-h-sm\)/);
  });
});
