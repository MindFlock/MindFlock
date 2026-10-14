/** Frontend wave 4 (J2 palette, J3 context line, J4 presets, J5 budget toast,
 * round-3 CSS fix): the markup hooks, JS wiring and CSS rules, pinned on the
 * module that owns each one so they can't silently regress.
 *
 * Moved from tests/unit/test_frontend_wave4.py, which grepped the built
 * app.js/style.css. The notify-rule check (GET /api/notify/config) stays there.
 * Source helpers: sourceText.ts. */

import { describe, expect, it } from "vitest";
import { BUILTIN_PRESETS, PRESET_STORE_KEY } from "../lib/presets";
import { blockFrom, code, codeFilesWith, css } from "./sourceText";

const PALETTE = "components/palette/CommandPalette.tsx";

describe("J2 — command palette (Ctrl+P / Ctrl+Shift+P)", () => {
  it("renders its panel, input and list, and the footer button opens it", () => {
    const pal = code(PALETTE);
    expect(pal).toContain('"palette"');
    expect(pal).toContain('"palette-panel"');
    expect(pal).toContain('"palette-input"');
    expect(pal).toContain('"palette-list"');
    // The shortcut is surfaced on the always-visible button.
    const top = code("components/TopBar.tsx");
    expect(top).toContain('"palette-btn"');
    // Ctrl+P / Ctrl+Shift+P (Cmd on Mac); Ctrl+K is the chord prefix for
    // focused-session git actions.
    expect(top).toContain('keys("palette")');
  });

  it("toggles through the capture-phase keymap, so it works over a terminal", () => {
    const km = code("lib/keymap.ts");
    expect(km).toContain("togglePalette");
    expect(km).toContain("e.ctrlKey || e.metaKey");
  });

  it("filters with a fuzzy subsequence score over the action list", () => {
    const pal = code(PALETTE);
    expect(pal).toContain("fuzzyScore");
    expect(pal).toContain("palette-item");
    for (const label of [
      '"New session…"',
      '"Focus: "',
      '"Open Settings"',
      '"Open Doctor"',
      '"Open Setup checklist"',
      '"Toggle sidebar"',
      '"Recently closed…"',
    ]) {
      expect(pal, label).toContain(label);
    }
  });

  it("reuses the shared session-action handlers", () => {
    // The palette's focused-session rows come from lib/sessionActionTable —
    // the same rows the sidebar › menu runs.
    expect(code(PALETTE)).toContain('from "../../lib/sessionActionTable"');
    const table = code("lib/sessionActionTable.ts");
    for (const call of ["commitSession(t)", "pushSession(t)", "makePrSession(t)", "ideSession(t)"]) {
      expect(table, call).toContain(call);
    }
  });

  it("arrows + Enter run, Escape closes", () => {
    const pal = code(PALETTE);
    expect(pal).toContain("ArrowDown");
    expect(pal).toContain("ArrowUp");
    expect(pal).toContain("filtered[sel]");
    expect(pal).toContain("closeDialog");
  });

  it("palette CSS rules", () => {
    const sheet = css("components/palette/CommandPalette.css");
    for (const sel of [
      "#palette-panel",
      "#palette-input",
      ".palette-item",
      ".palette-item.selected",
      ".palette-hint",
      "#palette-btn",
    ]) {
      expect(sheet, sel).toContain(sel);
    }
  });
});

describe("J3 — session context line (diff stat)", () => {
  it("is feature-detected off diff_stat and rendered in the pane header only", () => {
    const pane = code("components/grid/Pane.tsx");
    // Feature detection: the backend may not send diff_stat.
    expect(pane).toContain("diff_stat");
    expect(pane).toContain("ctx-files");
    expect(pane).toContain("ctx-add");
    // Hidden when null / all-zero.
    expect(pane).toContain("hasDiffStat");
    // The sidebar card no longer carries the diff-stat line.
    expect(codeFilesWith("inst-ctx")).toEqual([]);
  });

  it("context-line CSS, tinted from the theme vars so both themes are covered", () => {
    const sheet = css("components/grid/contextPresets.css");
    for (const sel of [".ctxline", ".ctxline .ctx-add", ".ctxline .ctx-del", ".pane-head .ctxline"]) {
      expect(sheet, sel).toContain(sel);
    }
    expect(blockFrom(sheet, ".ctxline .ctx-add")).toContain("--green");
    expect(blockFrom(sheet, ".ctxline .ctx-del")).toContain("--red");
  });
});

describe("J4 — prompt presets in the New-session dialog", () => {
  it("renders the Task box and the saved-prompt picker (saving lives in Prompts)", () => {
    const fold = code("components/dialogs/newSession/PromptFold.tsx");
    // #new-prompt is the box's id in New (scope "new"; "tpl" in a template's editor).
    for (const id of ['scope = "new"', 'id={scope + "-prompt"}', '"new-preset"', '"preset-manage"']) {
      expect(fold, id).toContain(id);
    }
    // One save on the New form: "Save as template…" (the whole setup). Saved
    // prompts are added and removed in the Prompts dialog.
    for (const id of ['"preset-save"', '"preset-del"']) expect(fold, id).not.toContain(id);
    expect(code("components/dialogs/newSession/FooterActions.tsx")).toContain('id="new-save-tpl"');
  });

  it("ships the four built-ins under the documented key", () => {
    expect(PRESET_STORE_KEY).toBe("mindflock.prompt_presets");
    expect(BUILTIN_PRESETS.map((p) => p.name)).toEqual([
      "Fix failing tests",
      "Address PR review comments",
      "Write tests for recent changes",
      "Refactor for clarity — no behavior change",
    ]);
    for (const p of BUILTIN_PRESETS) expect(p.prompt.length).toBeGreaterThan(0);
  });

  it("Manage… hands over to Prompts, and the seed prompt rides the create payload", () => {
    const fold = code("components/dialogs/newSession/PromptFold.tsx");
    expect(fold).toContain('openDialogFor("prompts", "new")');
    expect(code("components/dialogs/PromptsDialog.tsx")).toContain("saveUserPresets");
    expect(code("lib/newSessionSubmit.ts")).toContain("body.prompt = promptVal");
  });
});

describe("J5 — budget guardrail frontend", () => {
  it("toasts session.budget_exceeded with $cost of $budget", () => {
    const toasts = code("components/EventToasts.tsx");
    expect(toasts).toContain('"session.budget_exceeded"');
    expect(toasts).toContain("exceeded its budget");
    expect(toasts).toContain("d.cost");
    expect(toasts).toContain("d.budget");
  });

  it("Settings has the per-session budget field", () => {
    const general = code("components/settings/screens/General.tsx");
    expect(general).toContain('"session_budget_usd"');
    expect(general).toContain("Per-session budget (USD, 0 = off)");
  });
});

describe("round-3 CSS fix: sidebar row title/branch flex weights", () => {
  it("the title wins free width over the branch", () => {
    const sheet = css("components/sidebar/SidebarRow.css");
    const title = blockFrom(sheet, ".inst .title {");
    const branch = blockFrom(sheet, ".inst .branch {");
    // Title grows into free width (was flex: 0 1 auto → ellipsized at ~5
    // chars while the branch span won all free width as an illegible sliver).
    expect(title).toContain("flex: 1 1 auto");
    expect(title).toContain("min-width: 34px"); // legible floor kept (H4)
    // Branch yields: content-sized, capped at 40%, with its own ellipsis.
    expect(branch).toContain("flex: 0 1 auto");
    expect(branch).toContain("max-width: 40%");
    expect(branch).toContain("text-overflow: ellipsis");
  });
});
