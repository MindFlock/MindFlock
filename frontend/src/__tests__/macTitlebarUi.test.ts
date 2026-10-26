/** The macOS title bar, frontend half: TopBar's two attributes and mirrored
 * cluster, and the stylesheet rules that make room for the traffic lights.
 *
 * Moved from tests/unit/test_mac_titlebar.py's bundle checks. The electron
 * side (main.js / preload.js) and the source-structure checks of TopBar.tsx /
 * shell.ts stay there, as does the served-vs-source gap comparison (a
 * freshness contract); lib/shell's strict `=== true` gate is driven in
 * shell.test.ts. */

import { describe, expect, it } from "vitest";
import { ALL_CODE, cssRule, src } from "./sourceText";

const TOPBAR = src("components/TopBar.tsx");
const CSS = src("components/TopBar.css");

describe("TopBar.css: room for the lights, and the mirrored cluster", () => {
  it("keys the gap on data-mac-lights, never on data-mac", () => {
    // Keyed on data-mac it would leave a hole in fullscreen, where macOS hides
    // the lights and no button occupies it.
    expect(cssRule(CSS, "#topbar[data-mac-lights]")).toContain("padding-left");
    expect(cssRule(CSS, "#topbar[data-mac]")).not.toContain("padding-left");
    expect(cssRule(CSS, "#topbar[data-mac] .tb-end")).not.toBe("");
  });
});

describe("TopBar.tsx and lib/shell.ts carry the mac title bar", () => {
  it("marks the layout with both attributes and renders the .tb-end cluster", () => {
    expect(TOPBAR).toContain("data-mac={");
    expect(TOPBAR).toContain("data-mac-lights={");
    expect(TOPBAR).toContain('<div className="tb-end">');
  });

  it("reads the bridge's capability strictly, and subscribes to fullscreen", () => {
    const shell = src("lib/shell.ts");
    expect(shell).toMatch(/nativeTitleBar\)? === true/);
    expect(shell).toContain("onFullScreenChanged");
    expect(TOPBAR).toContain("onFullScreenChanged(setFullScreen)");
  });

  it("builds the swapping cluster once, after the drag handle", () => {
    // A future edit that duplicates the cluster instead of reusing the
    // hoisted element fails here — anywhere in the app, not just TopBar.
    expect(ALL_CODE.split('id="theme-btn"').length - 1).toBe(1);
    expect(ALL_CODE.split('id="brand-logo"').length - 1).toBe(1);
    expect(TOPBAR.indexOf('className="tb-drag"')).toBeGreaterThan(-1);
    expect(TOPBAR.indexOf('className="tb-drag"')).toBeLessThan(TOPBAR.indexOf('className="tb-end"'));
  });

  it("mirrors the mic with the bell: after it on the left, before it in .tb-end", () => {
    // Left (Windows / Linux / a browser): bell, then mic. The Mac mirror reads
    // from the far corner inward — logo, theme, bell — so the mic comes first.
    const left = TOPBAR.slice(TOPBAR.indexOf('<div className="tb-left">'), TOPBAR.indexOf('<nav className="tb-menu"'));
    expect(left.indexOf("{!mac && <TopBarMic />}")).toBeGreaterThan(left.indexOf("{!mac && <NotificationsBell />}"));
    const end = TOPBAR.slice(TOPBAR.indexOf('<div className="tb-end">'));
    expect(end.indexOf("<TopBarMic />")).toBeGreaterThan(-1);
    expect(end.indexOf("<TopBarMic />")).toBeLessThan(end.indexOf("<NotificationsBell />"));
    // Rendered in exactly one of the two places at a time, one button id app-wide.
    expect(TOPBAR.split("<TopBarMic />").length - 1).toBe(2);
    expect(ALL_CODE.split('id="mic-btn"').length - 1).toBe(1);
  });
});
