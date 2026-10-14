/** InfoTip (components/InfoTip.tsx): the circled-i that replaced the "Learn
 * more" folds, and the call sites converted to it.
 *
 * The test environment has no DOM, so the behaviour is pinned where it
 * lives: the pure state machine (tipReduce — hover, focus, Escape, touch
 * toggle, tap outside) and the pure placement (placeTip — flip above/below,
 * shift inside the window, the arrow on the icon). The markup and its aria
 * come from a server render. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import {
  InfoTip,
  TIP_CLOSED,
  TIP_CLOSE_MS,
  placeTip,
  tipOpen,
  tipReduce,
  type TipEvent,
  type TipState,
} from "../components/InfoTip";
import { ALL_CODE, cssRule, src } from "./sourceText";

const run = (...events: TipEvent[]): TipState => events.reduce(tipReduce, TIP_CLOSED);

describe("tipReduce: when the tip is open", () => {
  it("opens on hover and closes once the pointer has left", () => {
    expect(tipOpen(run("enter"))).toBe(true);
    expect(tipOpen(run("enter", "leave"))).toBe(false);
    // The grace that lets the pointer cross onto the bubble.
    expect(TIP_CLOSE_MS).toBe(100);
  });

  it("opens on keyboard focus and closes on blur", () => {
    expect(tipOpen(run("focus"))).toBe(true);
    expect(tipOpen(run("focus", "blur"))).toBe(false);
  });

  it("stays open while focused even after the mouse leaves", () => {
    expect(tipOpen(run("focus", "enter", "leave"))).toBe(true);
  });

  it("Escape closes it whatever opened it", () => {
    for (const how of ["enter", "focus", "tap", "activate"] as TipEvent[]) {
      expect(run(how, "escape")).toEqual(TIP_CLOSED);
    }
  });

  it("a tap toggles it, and a tap outside closes it", () => {
    expect(tipOpen(run("tap"))).toBe(true);
    expect(tipOpen(run("tap", "tap"))).toBe(false);
    expect(tipOpen(run("tap", "outside"))).toBe(false);
    // A tapped-open tip is not closed by a stray pointerleave.
    expect(tipOpen(run("tap", "leave"))).toBe(true);
  });

  it("a toggle that closes clears every reason at once", () => {
    // Hovered open, then Enter: closed, not "still hovered".
    expect(run("enter", "activate")).toEqual(TIP_CLOSED);
    expect(tipOpen(run("activate"))).toBe(true);
  });
});

describe("placeTip: where the bubble goes", () => {
  const view = { w: 1000, h: 800 };
  const size = { w: 300, h: 120 };
  const icon = (x: number, y: number) => ({ left: x, right: x + 18, top: y, bottom: y + 18 });

  it("sits below the icon, centred on it, when there is room", () => {
    const p = placeTip(icon(491, 100), size, view);
    expect(p.side).toBe("below");
    expect(p.top).toBe(126);
    expect(p.left).toBe(350);
    expect(p.arrowX).toBe(150);
  });

  it("flips above near the bottom of the window", () => {
    const p = placeTip(icon(491, 740), size, view);
    expect(p.side).toBe("above");
    expect(p.top).toBe(740 - 8 - 120);
  });

  it("shifts inside the window near an edge, the arrow still on the icon", () => {
    const right = placeTip(icon(970, 100), size, view);
    expect(right.left).toBe(1000 - 8 - 300);
    expect(right.arrowX).toBe(979 - right.left);
    const left = placeTip(icon(2, 100), size, view);
    expect(left.left).toBe(8);
    // Never closer than 12px to the rounded corner.
    expect(left.arrowX).toBe(12);
  });

  it("stays in the window when neither side has room", () => {
    const p = placeTip(icon(491, 300), { w: 300, h: 700 }, view);
    expect(p.top).toBeGreaterThanOrEqual(8);
    expect(p.top + 700).toBeLessThanOrEqual(800);
  });
});

describe("the InfoTip markup", () => {
  const html = renderToStaticMarkup(createElement(InfoTip, { id: "x", children: "The long explanation." }));

  it("is a button named More info, described by the text", () => {
    expect(html).toContain('<button type="button" id="x" class="infotip-btn" aria-label="More info"');
    expect(html).toContain('aria-describedby="x-tip"');
    expect(html).toContain('<span id="x-tip" role="tooltip" class="infotip-sr">The long explanation.</span>');
  });

  it("keeps the text in place (visually hidden) and draws the bubble only when open", () => {
    expect(html).not.toContain('class="infotip"');
    expect(cssRule(src("components/InfoTip.css"), ".infotip-sr")).toContain("clip-path: inset(50%);");
  });

  it("has a focus ring and a bubble in the app's own tokens", () => {
    const css = src("components/InfoTip.css");
    expect(cssRule(css, "button.infotip-btn:focus-visible")).toContain("outline: 2px solid var(--accent);");
    const bubble = cssRule(css, ".infotip");
    expect(bubble).toContain("position: fixed;");
    expect(bubble).toContain("max-width: min(320px, calc(100vw - 16px));");
    expect(bubble).toContain("font-size: 13px;");
    expect(bubble).toContain("background: var(--app-panel-2, var(--panel-2));");
    expect(src("styles/theme-light.css")).toContain(".light .infotip {");
    expect(src("styles/index.css")).toContain('@import "../components/InfoTip.css" layer(components);');
  });

  it("renders into document.body, and never through a native dialog", () => {
    const tip = src("components/InfoTip.tsx");
    expect(tip).toContain("document.body");
    expect(tip).not.toMatch(/window\.(prompt|alert|confirm)\(/);
  });
});

describe("the Learn more folds are InfoTips now", () => {
  it("no LearnMore and no .set-learn-more anywhere", () => {
    expect(ALL_CODE).not.toContain("LearnMore");
    expect(ALL_CODE).not.toContain("set-learn-more");
    expect(src("components/settings/settingsMode.css")).not.toContain("set-learn-more");
  });

  it.each([
    ["components/settings/screens/General.tsx", 2],
    ["components/settings/screens/Workspace.tsx", 2],
    ["components/settings/screens/Appearance.tsx", 1],
  ])("%s carries its tips inside the hint line", (path, n) => {
    const text = src(path);
    expect(text.split("<InfoTip>").length - 1).toBe(n);
    // Inside the hint, after the control: in a <label>, a button BEFORE the
    // select would become the label's control and steal its clicks.
    for (const chunk of text.split("<InfoTip>").slice(1)) {
      expect(chunk.slice(chunk.indexOf("</InfoTip>"), chunk.indexOf("</InfoTip>") + 40)).toMatch(
        /<\/InfoTip>\s*<\/span>/
      );
    }
  });

  it("Verify's 'How Verify works' fold is the title's tip", () => {
    const verify = src("components/dialogs/VerifyDialog.tsx");
    expect(verify).not.toContain("<summary>How Verify works</summary>");
    expect(verify).toContain('<InfoTip id="verify-how" label="How Verify works">');
  });
});
