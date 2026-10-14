/** Frontend wave 5 (round-4 findings L5–L9) plus the "gh is optional" and
 * commit-dialog contracts, pinned on the module that owns each one.
 *
 * - L5: the palette on Ctrl+P / Ctrl+Shift+P; Ctrl+K is the chord prefix for
 *       the outward-facing git verbs (Ctrl+Alt single strokes are gone).
 * - L6: toast subscribers skip replayed backlog envelopes.
 * - L7: missing-workspace row/pane treatment with a single Delete action.
 * - L8: no-origin push guidance (hint button + copyable command + error cards).
 * - L9: light-theme stage-chip contrast on focused/active rows.
 * - ssh-friendly-github: a contributor with an SSH remote hit a blocking modal
 *   about a CLI she had no reason to install; every PR surface now degrades
 *   to a toast / browser URL instead.
 *
 * Moved from tests/unit/test_frontend_wave5.py, which grepped the built
 * app.js/style.css. core/events.js, mobile.js, the docs and the commit-message
 * routes the dialog calls stay there. Source helpers: sourceText.ts. */

import { describe, expect, it, vi } from "vitest";
import { KEYMAP, shortcutLabels } from "../lib/keymap";
import { PR_REMEDY } from "../lib/sessionActions";
import { backdropPress } from "../components/Modal";
import { blockFrom, code, codeFilesWith, css, squash } from "./sourceText";

/** A top-level function's body in a source file, through its closing brace. */
function fnBody(path: string, head: string): string {
  return blockFrom(code(path), head, "\n}\n");
}

describe("L5 → keymap era: palette on Ctrl+P, Ctrl+K chords", () => {
  it("one table-driven keymap carries the palette on p with shift 'any'", () => {
    // Ctrl == Cmd via the mod alias; shift "any" so both Ctrl+P and VSCode's
    // Ctrl+Shift+P work.
    const palette = KEYMAP.find((e) => e.id === "palette");
    expect(palette).toMatchObject({ key: "p", mod: true, shift: "any" });
    const km = code("lib/keymap.ts");
    expect(km).toContain("e.ctrlKey || e.metaKey");
    expect(km).toContain("togglePalette");
  });

  it("Ctrl+K is a chord prefix; the AltGr-hazardous Ctrl+Alt family is gone", () => {
    const km = code("lib/keymap.ts");
    expect(km).toContain("CHORDS");
    expect(km).toContain("_enterChord");
    expect(codeFilesWith("_SESSION_HOTKEYS")).toEqual([]);
    expect(codeFilesWith('"kbd">Ctrl+Alt')).toEqual([]);
    // the JSX spelling of the same retired cheat-sheet row
    expect(codeFilesWith(">Ctrl+Alt")).toEqual([]);
  });

  it("the footer button advertises the always-global binding", () => {
    // Read from the live keymap (rebinds included), so it can't drift.
    expect(code("components/TopBar.tsx")).toContain('"Command palette" + keys("palette")');
    expect(shortcutLabels("palette")).toContain("Ctrl+P");
  });
});

describe("L6 — backlog replay must not re-fire one-shot toasts", () => {
  const toasts = code("components/EventToasts.tsx");

  it("feature-detects the accessor so an older events.js can't break dispatch", () => {
    expect(toasts).toContain('typeof ev.isReplay === "function"');
  });

  it("gates clarify arming but not the state repaint", () => {
    // The replay check precedes the clarify branch as an early return.
    expect(toasts).toContain('env.new === "clarify" && inst');
  });

  it("stage (interrupt), PR-state and budget subscribers bail on replays first", () => {
    expect(toasts.split("if (isReplay(env)) return;").length - 1).toBeGreaterThanOrEqual(3);
    const stage = toasts.slice(toasts.indexOf('ev.subscribe("session.stage_changed"'));
    expect(stage.indexOf("isReplay(env)")).toBeGreaterThanOrEqual(0);
    expect(stage.indexOf("isReplay(env)")).toBeLessThan(stage.indexOf('env.new === "interrupt"'));
    const pr = toasts.slice(toasts.indexOf('ev.subscribe("session.pr_state_changed"'));
    expect(pr.indexOf("isReplay(env)")).toBeGreaterThanOrEqual(0);
    expect(pr.indexOf("isReplay(env)")).toBeLessThan(pr.indexOf('env.new === "OPEN"'));
    const budget = toasts.slice(toasts.indexOf('ev.subscribe("session.budget_exceeded"'));
    expect(budget.indexOf("isReplay(env)")).toBeGreaterThanOrEqual(0);
    expect(budget.indexOf("isReplay(env)")).toBeLessThan(budget.indexOf("exceeded its budget"));
  });
});

describe("L7 — missing-workspace row/pane treatment", () => {
  it("feature-detects the backend flag and shows a distinct chip, not a fake stage", () => {
    const pane = code("components/grid/Pane.tsx");
    expect(pane).toContain("workspace_missing");
    expect(squash(pane)).toContain(
      '<span className="stagechip s-missing" title="Workspace directory no longer exists"> missing workspace </span>'
    );
    expect(code("lib/stage.ts")).toContain("s-missing");
  });

  it("offers a single Delete action on the existing DELETE endpoint", () => {
    expect(fnBody("lib/sessionActions.ts", "async function cleanupMissing")).toContain('{ method: "DELETE" }');
    const row = code("components/sidebar/SidebarRow.tsx");
    expect(row).toContain("cleanupMissing");
    // The menu (row ⋯ and pane ⋯) offers the one Delete for a gone folder.
    const menu = code("lib/sessionMenu.ts");
    expect(menu).toContain("Delete — remove session");
    expect(menu).toContain("run: () => cleanupMissing(title)");
  });

  it("swaps the pane for a placeholder with the same action; no terminal", () => {
    const pane = code("components/grid/Pane.tsx");
    expect(pane).toContain("missing-pane");
    expect(pane).toContain("missing-cleanup");
    expect(pane).toContain("cleanupMissing");
  });

  it("suppresses the next-step button and the diff context line", () => {
    expect(code("lib/stage.ts")).toContain("if (inst.workspace_missing) return null;");
    expect(code("components/grid/Pane.tsx")).toContain("workspace_missing ? null : inst.diff_stat");
  });

  it("CSS: muted/hollow row, chip, cleanup button and pane placeholder", () => {
    const sheet = css("components/grid/missingWorkspace.css");
    for (const sel of [
      ".inst.ws-missing .inst-row",
      ".inst.ws-missing .dot",
      ".stagechip.s-missing",
      ".inst .kill.cleanup",
      ".pane.missing-pane",
      ".missing-body",
      ".missing-body .missing-cleanup",
    ]) {
      expect(sheet, sel).toContain(sel);
    }
    // Theme-var driven (muted/border), so dark AND light are covered.
    const chip = blockFrom(sheet, ".stagechip.s-missing");
    expect(chip).toContain("var(--muted)");
    expect(chip).toContain("var(--border)");
  });
});

describe("L8 — no-origin push guidance", () => {
  it("offers a runnable add-origin command when has_origin is explicitly false", () => {
    const stage = code("lib/stage.ts");
    expect(stage).toContain("inst.has_origin === false");
    // A runnable example, not a placeholder — and SSH is the one spelled out,
    // because MindFlock never rewrites a remote and pushes with plain git.
    expect(stage).toContain("git remote add origin");
    expect(stage).toContain("git@github.com:owner/repo.git");
    expect(stage).toContain("https://github.com/owner/repo.git");
    expect(stage).toContain("No remote — add origin…");
    // Clicking copies the command and toasts.
    expect(stage).toContain('"command copied"');
    // Rendered as a subdued, non-destructive hint.
    expect(code("components/grid/Pane.tsx")).toContain("nextstep-hint");
  });

  it("push errors surface as an error card, never an alert", () => {
    const actions = code("lib/sessionActions.ts");
    expect(actions).toContain('errorPop("Push failed", errMsg(err))');
    const push = fnBody("lib/sessionActions.ts", "async function pushSession");
    expect(push).toContain('errorPop("Push failed", errMsg(err))');
    expect(push).not.toContain("alert(");
  });

  it("CSS: the hint is subdued — dashed border, copy cursor", () => {
    const sheet = css("components/grid/missingWorkspace.css");
    expect(sheet).toContain(".pane-head .actions .nextstep.nextstep-hint");
    const block = blockFrom(sheet, ".nextstep.nextstep-hint");
    expect(block).toContain("dashed");
    expect(block).toContain("cursor: copy");
  });
});

describe("L9 — light-theme stage-chip contrast", () => {
  const light = css("styles/theme-light.css");

  it("the committed chip uses deep text on light rows", () => {
    const block = blockFrom(light, ".light .stagechip.s-committed {");
    // Deep blue replaces the pale #7fd1ff, which vanished on light rows.
    expect(block).toContain("#0a6aa8");
    expect(block).not.toContain("#7fd1ff");
  });

  it("every chip keeps its pill outline on active/hovered/focused surfaces", () => {
    const block = blockFrom(light, ".light .inst.active .stagechip");
    expect(block).toContain(".light .pane.focused .stagechip");
    expect(blockFrom(light, ".light .inst.active .stagechip", "\n}")).toContain(
      "inset 0 0 0 1px var(--border)"
    );
  });
});

describe("ssh-friendly-github — the GitHub CLI is optional in every UI surface", () => {
  // The user-facing remedy, verbatim — the same sentence the backend,
  // mobile.js and the docs carry (mobile.js is pinned by the Python suite).
  const REMEDY = "add a GitHub token in Settings → Connections → GitHub → Connect, or install the GitHub CLI";
  const REMEDY_OLD = "add a GitHub token in Intake → Pull requests, or install the GitHub CLI";

  it("the remedy sentence exists exactly once — one constant, no copies to drift", () => {
    expect(PR_REMEDY).toBe(REMEDY);
    const hits = codeFilesWith(REMEDY);
    expect(hits).toEqual(["lib/sessionActions.ts"]);
    expect(code("lib/sessionActions.ts").split(REMEDY).length - 1).toBe(1);
    expect(codeFilesWith(REMEDY_OLD)).toEqual([]);
    // No surface renders the backend's old gh sentence.
    expect(codeFilesWith("GitHub CLI (gh) is not installed")).toEqual([]);
  });

  it("Make PR / Merge report through toasts; the blocking alert() is gone", () => {
    for (const fn of ["async function submitMakePr", "async function mergeSession"]) {
      const body = fnBody("lib/sessionActions.ts", fn);
      expect(body, fn).not.toContain("alert(");
      expect(body.includes("toast(") || body.includes("errorPop("), fn).toBe(true);
    }
    const actions = code("lib/sessionActions.ts");
    expect(actions).toContain('errorPop("Open PR failed", errMsg(err))');
    expect(actions).toContain('errorPop("Merge failed", errMsg(err))');
  });

  it("opens the prefilled compare / PR URL on the 200 + ok:false fallback", () => {
    const actions = code("lib/sessionActions.ts");
    expect(actions).toContain("r.ok === false");
    expect(actions).toContain("r.compare_url");
    expect(actions).toContain("r.pr_url");
    expect(actions).toContain("click to open");
  });

  it("gates PR entry points on an explicit github === false; the step stays runnable", () => {
    expect(code("lib/sessionActions.ts")).toContain("github !== false");
    const stage = code("lib/stage.ts");
    expect(stage).toContain("Open PR ↗");
    expect(stage).toContain("Merge on GitHub ↗");
    expect(code("lib/sessionActions.ts")).toContain("opens GitHub");
  });

  it("GitHub's one test is the Connect dialog's; gh is never 'required'", () => {
    expect(codeFilesWith("gh not installed (optional)")).toEqual([]);
    expect(codeFilesWith('gh not installed"')).toEqual([]);
    expect(code("components/setup/SetupChecklist.tsx")).toContain("setup-connect-github");
  });

  it("Doctor and the tour stop selling gh as required", () => {
    expect(codeFilesWith("Probes git, tmux, gh,")).toEqual([]);
    expect(code("components/settings/screens/Doctor.tsx")).toContain("plus gh, which is optional");
    // The welcome tour leads with the token; `gh auth login` is no longer the
    // onboarding's concrete instruction.
    expect(codeFilesWith("gh auth login")).toEqual([]);
    expect(code("components/setup/SetupChecklist.tsx")).toContain("all MindFlock needs to open and merge PRs");
  });
});

describe("the commit dialog", () => {
  it("offers a model-written message (✨) and shows progress while writing", () => {
    const dlg = code("components/dialogs/CommitDialog.tsx");
    expect(dlg).toContain('id="commit-write"');
    // Disabled while the CLI turn is in flight: the label is the only
    // progress this dialog shows, and a second press would start a second turn.
    expect(dlg).toContain("Writing…");
    expect(css("components/dialogs/smallDialogs.css")).toContain("#commit-form #commit-write");
  });

  it("closes on a backdrop mousedown that started on the backdrop", () => {
    // The dialog renders through the shared Modal shell, which owns the rule:
    // mousedown rather than click, and only when the backdrop is the event's
    // own target, so a selection that started in the textarea and overshot
    // cannot discard the message.
    expect(squash(code("components/dialogs/CommitDialog.tsx"))).toContain(
      '<Modal id="commit-dialog" onClose={closeDialog}>'
    );
    const modal = code("components/Modal.tsx");
    expect(modal).toContain("onMouseDown={closeOnBackdrop ? backdropPress(onClose) : undefined}");
    expect(modal).toContain("if (e.target === e.currentTarget) onClose();");
    // No click handler anywhere in the shell (the type-level Omit of the prop
    // is the only mention).
    expect(modal.replace('"onMouseDown" | "onClick"', "")).not.toContain("onClick");

    const onClose = vi.fn();
    const press = backdropPress(onClose);
    const card = {};
    const backdrop = {};
    press({ target: card, currentTarget: backdrop } as never);
    expect(onClose).not.toHaveBeenCalled();
    press({ target: backdrop, currentTarget: backdrop } as never);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
