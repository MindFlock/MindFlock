/// <reference types="vite/client" />
/** Round 12's Sky guide (components/flights/SkyGuide.tsx): its sections and
 * sketches, the keyboard table, the little store that opens it, and the
 * three ways in — the sky's ? menu (onboarding/TourButton), the palette's
 * "Learn the Sky", and the guide's own "Replay the tour". */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import GUIDE_SRC from "../components/flights/SkyGuide.tsx?raw";
import GUIDE_CSS from "../components/flights/SkyGuide.css?raw";
import PALETTE_SRC from "../components/palette/CommandPalette.tsx?raw";
import TOUR_BUTTON_SRC from "../components/onboarding/TourButton.tsx?raw";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})) }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const storage = new Map<string, string>();
(globalThis as Record<string, unknown>).localStorage = {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
};

const { SkyGuide, SKY_GUIDE_SECTIONS, SKY_GUIDE_KEYS, useSkyGuide, openSkyGuide, closeSkyGuide } = await import("../components/flights/SkyGuide");
const { TourButton } = await import("../components/onboarding/TourButton");

const render = (section?: string) => renderToStaticMarkup(createElement(SkyGuide, { open: true, onClose: () => {}, section, portal: false }));

describe("the guide's sections", () => {
  it("are the ten the contract names, in its order", () => {
    expect(SKY_GUIDE_SECTIONS.map((s) => s.title)).toEqual([
      "What the sky is",
      "Lines and boxes",
      "Stops and saved stops",
      "The bird",
      "Halt points and Remove",
      "Grounded stops",
      "Selection",
      "Tools and the keyboard",
      "The intake lane",
      "Where things are saved",
    ]);
    expect(new Set(SKY_GUIDE_SECTIONS.map((s) => s.id)).size).toBe(SKY_GUIDE_SECTIONS.length);
  });

  it("each renders in the wide modal with its sketch and words, the list on the left", () => {
    for (const s of SKY_GUIDE_SECTIONS) {
      const html = render(s.id);
      expect(html, s.id).toContain('id="sky-guide"');
      expect(html, s.id).toContain('class="modal sky-guide-modal"');
      expect(html, s.id).toContain(`<h3 id="sky-guide-page-title">${s.title.replace("&", "&amp;")}</h3>`);
      expect(html, s.id).toContain(`data-section="${s.id}"`);
      expect(html, s.id).toMatch(/<svg class="sg-sketch"[^>]*role="img"[^>]*aria-label="[^"]+"/);
      // every section is listed, the shown one marked
      for (const t of SKY_GUIDE_SECTIONS) expect(html, s.id + " lists " + t.id).toContain(`data-section="${t.id}"`);
      expect(html.match(/aria-current="true"/g), s.id).toHaveLength(1);
    }
  });

  it("an unknown or missing section opens the first", () => {
    expect(render("nope")).toContain('<h3 id="sky-guide-page-title">What the sky is</h3>');
    expect(render()).toContain('<h3 id="sky-guide-page-title">What the sky is</h3>');
  });

  it("closed renders nothing", () => {
    expect(renderToStaticMarkup(createElement(SkyGuide, { open: false, onClose: () => {} }))).toBe("");
  });

  it("says the things a newcomer needs, in the house words", () => {
    const words = SKY_GUIDE_SECTIONS.map((s) => render(s.id)).join(" ");
    for (const w of ["same sessions as the grid", "saved stop", "Halt now", "Halt here", "pauses the flight right before the next stop", "No session yet", "Group into a stop", "Save to the shelf", "Fly", "synced", "this browser"])
      expect(words, w).toContain(w);
    // the guide's own copy never says flyway (round 11 retired the word)
    expect(words.toLowerCase()).not.toContain("flyway");
  });

  it("sketches stay small and themeable: currentColor and tokens, no hard-coded ink", () => {
    expect(GUIDE_CSS).toContain("currentColor");
    expect(GUIDE_CSS).toContain("var(--accent)");
    // no fill="#…" / stroke="#…" in the sketches: CSS owns the colours
    expect(GUIDE_SRC).not.toMatch(/(fill|stroke)="#/);
  });
});

describe("the keyboard table", () => {
  it("lists every Sky key, once", () => {
    const keys = SKY_GUIDE_KEYS.map(([k]) => k);
    expect(keys).toEqual(["V", "H", "Space (held)", "0", "1", "+ / −", "T", "Arrows", "Delete", "Esc", "Ctrl+A", "Enter", "Alt+Y", "Ctrl/Alt+N"]);
    for (const [, what] of SKY_GUIDE_KEYS) expect(what.trim()).not.toBe("");
  });

  it("renders as a table on Tools and the keyboard", () => {
    const html = render("keys");
    expect(html).toContain('<table class="sky-guide-keys">');
    for (const [k] of SKY_GUIDE_KEYS) expect(html).toContain(`<kbd>${k}</kbd>`);
  });
});

describe("the guide's store", () => {
  it("opens on a section, follows a new one, and closes", () => {
    expect(useSkyGuide.getState().section).toBeNull();
    openSkyGuide();
    expect(useSkyGuide.getState().section).toBe("what");
    openSkyGuide("bird");
    expect(useSkyGuide.getState().section).toBe("bird");
    openSkyGuide("not-a-section");
    expect(useSkyGuide.getState().section).toBe("what");
    closeSkyGuide();
    expect(useSkyGuide.getState().section).toBeNull();
  });

  it("with no props, the guide renders itself from the store (the host mounts <SkyGuide />)", () => {
    // (A server render reads a zustand store's INITIAL state, so this pins
    // the wiring by source: the store's section, unless `open` is given.)
    expect(renderToStaticMarkup(createElement(SkyGuide, { portal: false }))).toBe("");
    expect(GUIDE_SRC).toContain("const stored = useSkyGuide((s) => s.section);");
    expect(GUIDE_SRC).toContain("const controlled = p.open !== undefined;");
    expect(GUIDE_SRC).toContain("const open = controlled ? !!p.open : stored !== null;");
    expect(GUIDE_SRC).toContain("controlled ? (p.onClose ?? closeSkyGuide) : closeSkyGuide");
  });

  it("is its own zustand store, not the app store", () => {
    expect(GUIDE_SRC).toContain('import { create } from "zustand"');
    expect(GUIDE_SRC).toContain("export const useSkyGuide = create<SkyGuideState>(");
  });

  it("its Replay the tour goes through replayTour (an allowed tour opener)", () => {
    expect(GUIDE_SRC).toContain('replayTour("sky")');
    expect(GUIDE_SRC).not.toMatch(/\bopenCoachTour\(/);
  });

  it("never reaches for window.prompt / alert / confirm", () => {
    for (const src of [GUIDE_SRC, TOUR_BUTTON_SRC]) expect(src).not.toMatch(/\b(prompt|alert|confirm)\(/);
  });
});

describe("the ways in", () => {
  it("the palette has Learn the Sky: shows the sky, then opens the guide", () => {
    expect(PALETTE_SRC).toContain('label: "Learn the Sky"');
    expect(PALETTE_SRC).toMatch(/label: "Learn the Sky",[\s\S]{0,200}setView\("sky"\);\s*openSkyGuide\(\);/);
  });

  it("the sky's ? is a menu: Replay the tour · Open the guide", () => {
    const html = renderToStaticMarkup(createElement(TourButton, { tour: "sky" }));
    expect(html).toContain('class="tour-btn"');
    expect(html).toContain('aria-haspopup="menu"');
    expect(html).toContain('aria-expanded="false"');
    expect(html).toContain('data-tour="sky-guide"');
    expect(TOUR_BUTTON_SRC).toContain("Replay the tour");
    expect(TOUR_BUTTON_SRC).toContain("Open the guide");
    expect(TOUR_BUTTON_SRC).toContain("openSkyGuide()");
  });

  it("every other tour's ? stays the one button", () => {
    const html = renderToStaticMarkup(createElement(TourButton, { tour: "verify" }));
    expect(html).not.toContain("aria-haspopup");
    expect(html).toContain('title="Replay the Verify tour"');
  });
});
