/** User-rebindable keyboard shortcuts: the store, the ids, the cheat-sheet
 * editor and its CSS.
 *
 * The "?" cheat-sheet doubles as the editor — click a rebindable row, press
 * the new combo — with per-row reset (↺) and a Restore-defaults button.
 * Overrides live per-device ("mf_keymap"). The resolver behaviour itself is
 * covered by keymap.test.ts; this pins the wiring around it.
 *
 * Moved from tests/unit/test_keymap_rebind.py, which grepped the built
 * app.js/style.css (the docs check stays there). Source helpers:
 * sourceText.ts. */

import { describe, expect, it } from "vitest";
import { KEYMAP, chordKeyFor, comboProblem, defaultCombosFor, effBindings } from "../lib/keymap";
import { code, css } from "./sourceText";

const SHEET = "components/palette/ShortcutsSheet.tsx";

describe("the overrides store", () => {
  it("persists per device under mf_keymap and resolves through one helper", () => {
    const km = code("lib/keymap.ts");
    expect(km).toContain('"mf_keymap"');
    expect(km).toContain("_saveKeyOv");
    // One resolver turns an entry + overrides into the effective triggers
    // (plural — an action can hold several combos); the dispatcher goes
    // through it.
    expect(km).toContain("function effBindings(");
    expect(km.split("effBindings(").length - 1).toBeGreaterThan(1);
    expect(typeof effBindings).toBe("function");
    // "+" appends a combo, seeded from the defaults so they keep working.
    expect(km).toContain("function defaultCombosFor(");
    expect(typeof defaultCombosFor).toBe("function");
    expect(code(SHEET)).toContain("shortcut-add");
  });
});

describe("keymap entries carry rebind ids", () => {
  it("primary bindings are addressable for overrides", () => {
    const ids = new Set(KEYMAP.map((e) => e.id));
    for (const id of ["palette", "sidebar", "new", "cycle", "close", "reopen", "reload", "talk", "workers", "flock"]) {
      expect(ids.has(id), id).toBe(true);
    }
  });

  it("browser-safe duplicates retire with their primary; Shift-inverse partners follow it", () => {
    expect(KEYMAP.some((e) => e.aliasOf === "new")).toBe(true);
    expect(KEYMAP.some((e) => e.aliasOf === "close")).toBe(true);
    expect(KEYMAP.some((e) => e.pairOf === "cycle")).toBe(true);
  });
});

describe("chords rebind their second key", () => {
  it("one helper used by dispatch and the sheet alike", () => {
    // Chord actions stay keyed by their default letter.
    const km = code("lib/keymap.ts");
    expect(km).toContain("function chordKeyFor(");
    expect(km).toContain("_keyOv.chords");
    expect(typeof chordKeyFor).toBe("function");
    expect(code(SHEET)).toContain("chordKeyFor(");
  });
});

describe("the sheet is the editor", () => {
  it("click-to-rebind capture with a cancel path; the dispatcher stands down meanwhile", () => {
    const sheet = code(SHEET);
    expect(sheet).toContain("Press the new keys…");
    expect(sheet).toContain("Press the combo to add…");
    expect(sheet).toContain("setRebindCapturing");
    expect(code("lib/keymap.ts")).toContain("setRebindCapturing");
  });

  it("rejects bare printable keys, Shift on paired actions, collisions", () => {
    expect(code("lib/keymap.ts")).toContain("function comboProblem(");
    expect(typeof comboProblem).toBe("function");
    expect(code(SHEET)).toContain("comboProblem(");
  });

  it("per-row reset and restore-all", () => {
    const sheet = code(SHEET);
    expect(sheet).toContain("shortcut-reset");
    expect(sheet).toContain("shortcuts-restore");
    expect(sheet).toContain("Restore defaults");
  });
});

describe("custom bare keys are guarded while typing", () => {
  it("a custom bare-key combo never fires in inputs or terminals", () => {
    // Overrides can't inherit the default keys' editing guards.
    expect(code("lib/keymap.ts")).toContain(
      "t !== b && !t.mod && !t.alt && isEditingTarget(document.activeElement)"
    );
  });
});

describe("rebind CSS", () => {
  it("rows are real boxes on shared tracks so a whole row hover-highlights", () => {
    const sheet = css("components/palette/ShortcutsSheet.css");
    for (const sel of [
      ".shortcut-row.rebindable",
      ".shortcut-row.capturing",
      ".shortcut-reset",
      ".shortcut-add",
      ".shortcut-controls",
      ".shortcuts-restore",
      ".shortcuts-hint",
    ]) {
      expect(sheet, sel).toContain(sel);
    }
    // subgrid, not display:contents
    expect(sheet).toContain("grid-template-columns: subgrid");
    expect(sheet).toContain(".shortcut-row.rebindable:hover");
  });
});
