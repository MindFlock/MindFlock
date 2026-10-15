/** The session menu fits the window (UX round 5, finding 9: with More ▸
 * unfolded, the pane ⋯ menu ran off the bottom of a 900 px screen and put
 * Delete… at y≈889). Both hosts — the pane header's ⋯ (PaneMoreMenu) and the
 * sidebar row's ⋮ (RowMenu) — are placed by lib/menuPlace after a hidden
 * measuring pass, re-fit as the menu grows, and on a window too short for it
 * scroll their items with Close / Delete… pinned at the foot. The numbers are
 * the real ones measured in the lab sandbox. */

import { describe, expect, it } from "vitest";
import { placeMenu, refitMenu, MENU_MARGIN } from "../lib/menuPlace";
import { code, css } from "./sourceText";

const m = MENU_MARGIN;
// The pane header's ⋯ at 1440 px wide: right edge 1417, 61–77 px from the top.
const paneBtn = { x: 1417, y: 77 + 4, flipY: 61 - 4, align: "end" as const };

describe("the pane ⋯ menu, More ▸ unfolded", () => {
  it("1440×900: hangs under the ⋯ folded, then nudges up so Delete… stays on screen", () => {
    const folded = placeMenu(paneBtn, 280, 470, 1440, 900);
    expect(folded).toEqual({ left: 1417 - 280, top: 81, maxHeight: 900 - 2 * m });
    const unfolded = refitMenu(folded, 280, 813, 1440, 900);
    expect(unfolded.top).toBe(900 - m - 813);
    expect(unfolded.top + 813).toBeLessThanOrEqual(900 - m);
  });

  it("1280×720: taller than the window, so it is capped at it and scrolls", () => {
    const folded = placeMenu({ ...paneBtn, x: 1257 }, 280, 470, 1280, 720);
    const unfolded = refitMenu(folded, 280, 813, 1280, 720);
    expect(unfolded).toEqual({ left: 1257 - 280, top: m, maxHeight: 720 - 2 * m });
  });

  it("opened with More ▸ already tall (a pane low in a 2×2): flips above the ⋯", () => {
    const low = { x: 1417, y: 500 + 4, flipY: 484 - 4, align: "end" as const };
    expect(placeMenu(low, 280, 420, 1440, 900).top).toBe(480 - 420);
  });
});

describe("both hosts measure first and keep the whole menu on screen", () => {
  it("the pane ⋯ menu is placed by lib/menuPlace, hidden until measured, re-fit as it grows", () => {
    const pane = code("components/grid/PaneMoreMenu.tsx");
    expect(pane).toMatch(/import \{ placeMenu, refitMenu, type MenuPlacement \} from "\.\.\/\.\.\/lib\/menuPlace"/);
    expect(pane).toContain('visibility: "hidden"');
    expect(pane).toContain("maxHeight: place.maxHeight");
    expect(pane).toContain("new ResizeObserver(fit)");
    // Its old placement, pinned under the button with no measurement, is gone.
    expect(pane).not.toContain("window.innerWidth - r.right");
    expect(pane).not.toMatch(/right: pos\.right/);
  });

  it("the row ⋮ menu counts its borders, so its foot doesn't overhang the margin", () => {
    expect(code("components/sidebar/RowMenu.tsx")).toContain("el.offsetHeight - el.clientHeight + el.scrollHeight");
  });

  it("the pane menu's items scroll inside its placed height, not a guessed 100vh - 120px", () => {
    const sheet = css("components/sidebar/SessionMenu.css");
    const rule = /\.pane-more-menu \.session-menu \{([^}]*)\}/.exec(sheet)?.[1] ?? "";
    expect(rule).toContain("min-height: 0");
    expect(rule).toContain("overflow-y: auto");
    expect(rule).not.toContain("100vh");
  });

  it("Close and Delete… are pinned at the foot of a menu that scrolls", () => {
    const rule = /\.session-menu \.sm-danger \{([^}]*)\}/.exec(css("components/sidebar/SessionMenu.css"))?.[1] ?? "";
    expect(rule).toContain("position: sticky");
    expect(rule).toContain("bottom: 0");
    expect(rule).toContain("background: var(--panel)");
  });

  it("More ▸ unfolding scrolls its items into sight above that foot", () => {
    const menu = code("components/sidebar/SessionMenu.tsx");
    expect(menu).toContain('className="sm-group sm-more-group" ref={moreRef}');
    expect(menu).toMatch(/querySelector\("\.sm-danger"\)/);
    expect(menu).toContain("box.scrollTop += by");
  });
});
