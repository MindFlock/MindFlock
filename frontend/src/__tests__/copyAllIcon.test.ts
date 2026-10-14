/** The pane header's Copy all is always its clipboard glyph, never the words
 * "Copy all" (owner, 2026-10-14: "I don't like that it says copy all, go back
 * to the icon"). History still switches word ↔ glyph with the header's fit;
 * the History overlay's own "Copy all" button is a different control. */

import { describe, expect, it } from "vitest";
import { code, CSS } from "./sourceText";

describe("Copy all in the pane header is an icon", () => {
  it("the header button and the Customize preview carry no word label", () => {
    const pane = code("components/grid/Pane.tsx");
    const start = pane.indexOf('className="act copy-history"');
    expect(start).toBeGreaterThan(-1);
    const button = pane.slice(start, pane.indexOf("</button>", start));
    expect(button).toContain("<svg");
    expect(button).not.toContain("btn-label");
    expect(button).toContain("aria-label=\"Copy this pane's whole history\"");

    const preview = code("components/customize/PaneHeaderPicker.tsx");
    const p = preview.indexOf('className="act copy-history"');
    expect(preview.slice(p, preview.indexOf("</span>", p))).not.toContain("btn-label");
  });

  it("its glyph is never hidden in a roomy header", () => {
    const css = CSS["components/grid/Pane.css"];
    expect(css).toContain(".pane-head:not(.head-icons) .act:not(.copy-history) .btn-icon");
    expect(css).not.toMatch(/\.pane-head:not\(\.head-icons\) \.act \.btn-icon\s*\{/);
  });
});
