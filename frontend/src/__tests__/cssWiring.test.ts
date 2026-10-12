/** Rules that must reach the shipped style.css (moved from the built-CSS checks
 * in tests/unit/test_frontend_css_wiring.py).
 *
 * The stylesheet manifest (styles/index.css) is a hand-kept @import list, so a
 * rule reaches the bundle exactly when its sheet is imported there. These pin
 * both halves at the source: the sheet carries the rule, and the manifest
 * imports the sheet.
 *
 * - The full-history overlay shipped unstyled once (`position: absolute;
 *   inset: 0; z-index: 4` never reached the page), so opening it produced an
 *   invisible zero-size block and every route into it looked broken.
 * - The account chip and its swap menu shipped as dead hooks once: both sheets
 *   were imported, so the orphaned-sheet guard could not see a class referenced
 *   by a component and defined by nobody. */

import { describe, expect, it } from "vitest";
import { normCode } from "./sourceText";

const MANIFEST = normCode("styles/index.css");

/** `selector`'s declarations in an already-normalised sheet. */
function rule(css: string, selector: string): string {
  const start = css.indexOf(selector + " {");
  if (start < 0) throw new Error("no rule for " + selector);
  return css.slice(start, css.indexOf("}", start));
}

describe("the history overlay is a visible layer", () => {
  it("is imported by the stylesheet manifest", () => {
    expect(MANIFEST).toContain('@import "../components/grid/HistoryOverlay.css" layer(components);');
  });

  it("is positioned and painted over the pane", () => {
    const css = normCode("components/grid/HistoryOverlay.css");
    const block = rule(css, ".hist-overlay");
    expect(block).toContain("position: absolute");
    expect(block).toContain("inset: 0");
    expect(block).toContain("z-index");
    // The scroller and its header ride along with it.
    expect(css).toContain(".hist-scroll");
    expect(css).toContain(".hist-bar");
  });
});

describe("the account chip and its swap menu are styled", () => {
  it("lives in sheets the manifest imports", () => {
    expect(MANIFEST).toContain('@import "../components/grid/Pane.css" layer(components);');
    expect(MANIFEST).toContain('@import "../components/usage/usage.css" layer(components);');
  });

  it("keeps the chip in the header's right-hand group", () => {
    const pane = normCode("components/grid/Pane.css");
    // Only the FIRST .tok in the header may take the auto margin; a second one
    // splits the free space and lands the chip mid-header.
    expect(pane).toContain(".pane-head .acct-chip + .tok {");
    // A user-typed label cannot be allowed to grow the no-wrap header.
    expect(pane).toContain(".acct-chip .usage-head {");
  });

  it("makes the swap rows answer the pointer", () => {
    const usage = normCode("components/usage/usage.css");
    // The swap rows are buttons wearing a <tr>.
    expect(usage).toContain(".acct-pop-row {");
    expect(usage).toContain(".acct-pop-model select {");
  });
});
