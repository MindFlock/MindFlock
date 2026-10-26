/** A narrow top bar folds instead of clipping (round 5: at 420 px the palette
 * icon was under the window's edge and "Settings" read "Setting"). The rule is
 * fitTopBar's — pure, so it is pinned here — and the wiring around it is pinned
 * against TopBar's source. */

import { describe, expect, it, vi } from "vitest";
import TOPBAR from "../components/TopBar.tsx?raw";
import CSS from "../components/TopBar.css?raw";

// TopBar's imports read `document` (EventToasts, the store's theme class) and
// `localStorage` at load; no DOM here, so a stand-in for each. fitTopBar
// itself touches neither.
const noop = () => {};
// The title badge draws a favicon with `Image` (as in attentionWiring.test.ts).
vi.mock("../lib/titleBadge", () => ({ clearAttention: vi.fn(), updateTitleBadge: vi.fn(), useTitleBadge: vi.fn() }));
vi.stubGlobal("localStorage", { getItem: () => null, setItem: noop, removeItem: noop });
const g = globalThis as unknown as { document?: unknown };
if (typeof g.document === "undefined")
  g.document = {
    hidden: false,
    title: "",
    documentElement: { classList: { contains: () => false, add: noop, remove: noop, toggle: noop } },
    addEventListener: noop,
    removeEventListener: noop,
  };
const { fitTopBar } = await import("../components/TopBar");

// Roughly the real bar: left cluster + New + Intake + palette + gaps ≈ 270,
// the wordmark ≈ 140, Verify / Schedule / Settings ≈ 60 / 75 / 70, ⋯ ≈ 30.
const BAR = { fixed: 270, brand: 140, items: [60, 75, 70], more: 30 };

describe("fitTopBar: what a narrow bar gives up, and in which order", () => {
  it("keeps everything when everything fits", () => {
    expect(fitTopBar({ ...BAR, room: 1440 })).toEqual({ brand: true, shown: 3 });
    // Exactly full still fits (and so does the half-px rounding slack).
    expect(fitTopBar({ ...BAR, room: 615 })).toEqual({ brand: true, shown: 3 });
    expect(fitTopBar({ ...BAR, room: 614.6 })).toEqual({ brand: true, shown: 3 });
  });

  it("drops the wordmark before any control", () => {
    expect(fitTopBar({ ...BAR, room: 614 })).toEqual({ brand: false, shown: 3 });
    expect(fitTopBar({ ...BAR, room: 475 })).toEqual({ brand: false, shown: 3 });
  });

  it("then folds from the right — Settings, Schedule, Verify — making room for ⋯", () => {
    // 270 + 60 + 75 + 30 = 435: Settings folds.
    expect(fitTopBar({ ...BAR, room: 474 })).toEqual({ brand: false, shown: 2 });
    expect(fitTopBar({ ...BAR, room: 435 })).toEqual({ brand: false, shown: 2 });
    // 270 + 60 + 30 = 360: Schedule follows.
    expect(fitTopBar({ ...BAR, room: 434 })).toEqual({ brand: false, shown: 1 });
    expect(fitTopBar({ ...BAR, room: 360 })).toEqual({ brand: false, shown: 1 });
    // Then Verify, and that is the floor: New, Intake and the palette stay.
    expect(fitTopBar({ ...BAR, room: 359 })).toEqual({ brand: false, shown: 0 });
    expect(fitTopBar({ ...BAR, room: 100 })).toEqual({ brand: false, shown: 0 });
  });

  it("reserves the ⋯ only when something folds", () => {
    // All three need 475 with no ⋯ — a huge ⋯ is no reason to fold any.
    expect(fitTopBar({ ...BAR, room: 475, more: 1000 }).shown).toBe(3);
  });

  it("folds only what is on the bar (calm hides Verify and Schedule first)", () => {
    // Settings alone: kept, or folded straight into ⋯.
    const calm = { ...BAR, items: [70] };
    expect(fitTopBar({ ...calm, room: 340 })).toEqual({ brand: false, shown: 1 });
    expect(fitTopBar({ ...calm, room: 339 })).toEqual({ brand: false, shown: 0 });
    expect(fitTopBar({ ...calm, room: 900, items: [] })).toEqual({ brand: true, shown: 0 });
  });
});

describe("TopBar wiring around the fold", () => {
  it("keeps every id on its own bar button, and folds by class, not by unmounting", () => {
    for (const id of ["new-btn", "intake-btn", "verify-btn", "schedule-btn", "settings-btn", "palette-btn"])
      expect(TOPBAR, id).toContain(`id="${id}"`);
    for (const key of ["verify", "schedule", "settings"]) {
      expect(TOPBAR).toContain(`className={"tb-item" + (isFolded("${key}") ? " tb-folded" : "")}`);
      expect(TOPBAR).toContain(`data-fold="${key}"`);
    }
    // New, Intake and the palette never fold.
    for (const id of ["new-btn", "intake-btn", "palette-btn"]) {
      const at = TOPBAR.indexOf(`id="${id}"`);
      expect(TOPBAR.slice(at, at + 200), id).not.toContain("tb-folded");
    }
    // ⋯ sits after Settings and before the palette.
    const se = TOPBAR.indexOf('id="settings-btn"');
    const mo = TOPBAR.indexOf('id="more-btn"');
    const pa = TOPBAR.indexOf('id="palette-btn"');
    expect(se < mo && mo < pa).toBe(true);
  });

  it("the mic never folds: it rides in the left cluster, measured as fixed width", () => {
    // The fold measures `start.firstElementChild` (the .tb-left cluster) as
    // one fixed width, so the mic beside the bell is counted, never folded.
    const left = TOPBAR.slice(TOPBAR.indexOf('<div className="tb-left">'), TOPBAR.indexOf('<nav className="tb-menu"'));
    expect(left).toContain("<TopBarMic />");
    expect(TOPBAR).toContain("w(start.firstElementChild)");
    const mic = TOPBAR.indexOf("<TopBarMic />");
    expect(TOPBAR.slice(mic - 80, mic + 80)).not.toContain("tb-folded");
  });

  it("the calm rule still decides what is on the bar; the fold only lays it out", () => {
    expect(TOPBAR).toMatch(/\{shows\.verify && \(\s*<button\s+id="verify-btn"/);
    expect(TOPBAR).toMatch(/\{shows\.schedule && \(\s*<button\s+id="schedule-btn"/);
    expect(TOPBAR).toMatch(/shows\.verify && \{\s*key: "verify"/);
    expect(TOPBAR).toMatch(/shows\.schedule && \{\s*key: "schedule"/);
  });

  it("⋯ carries the folded items' counts and opens a real menu", () => {
    expect(TOPBAR).toContain("{foldedCount > 0 && (");
    expect(TOPBAR).toContain('aria-haspopup="menu"');
    expect(TOPBAR).toContain('role="menuitem"');
    // Each row wears its bar button's tooltip (shortcut and count line).
    expect(TOPBAR).toContain("title: verifyTitle,");
    expect(TOPBAR).toContain("title: scheduleTitle,");
  });

  it("measures before paint and on resize — no breakpoint list", () => {
    expect(TOPBAR).toContain("useLayoutEffect(() => measure.current());");
    expect(TOPBAR).toContain("new ResizeObserver(");
    expect(CSS).not.toMatch(/@media \(max-width: 5[26]0px\)/);
    expect(CSS).toMatch(/#topbar \.tb-menu \.tb-folded,\s*#topbar \.tb-brand\.tb-folded \{\s*display: none;/);
  });
});
