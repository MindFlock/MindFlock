/** The four top-bar dialogs — New, Intake, Verify, Schedule — open at the SAME
 * place: one fixed top edge, written once on the modal shell (Modal.css,
 * `.modal.top-dialog`), so hopping between them with Alt+N / I / V / S never
 * jumps and a tab, fold or editor inside one never moves its top. The owner
 * asked for it after New and Intake sat high while Verify and Schedule sat
 * in the true middle. */

import { describe, expect, it } from "vitest";
import { ALL_CSS, attr, cssRule, src, squash, tagsWithId } from "./sourceText";

const MODAL_CSS = src("components/Modal.css");

/** [dialog source, the shell's id, the card's id, the card's own sheet(s)] */
const DIALOGS: [string, string, string, string[]][] = [
  ["components/dialogs/NewSessionDialog.tsx", "new-dialog", "new-form", ["components/dialogs/NewSessionDialog.css"]],
  ["components/intake/IntakeDialog.tsx", "intake-dialog", "intake-panel", ["components/intake/IntakeDialog.css"]],
  ["components/dialogs/VerifyDialog.tsx", "verify-dialog", "verify-panel", ["components/dialogs/VerifyDialog.css"]],
  [
    "components/dialogs/TemplatesDialog.tsx",
    "templates-dialog",
    "templates-card",
    ["components/dialogs/ScheduleDialog.css", "components/dialogs/NewSessionDialog.css"],
  ],
];

describe("the shared top-bar dialog placement", () => {
  it("is one fixed top edge on the modal shell, floored below the top bar", () => {
    const block = cssRule(MODAL_CSS, ".modal.top-dialog");
    expect(block).toContain("--dlg-anchor-h: 620px;");
    expect(block).toContain(
      "--dlg-top: max(calc(var(--topbar-h, 40px) + 16px), calc((100vh - var(--dlg-anchor-h)) / 2));"
    );
    expect(block).toContain("--dlg-max-h: calc(100vh - var(--dlg-top) - 24px);");
    expect(block).toContain("align-items: flex-start;");
    expect(block).toContain("padding-top: var(--dlg-top);");
    // Phone: straight under the bar.
    expect(block).toContain("--dlg-top: calc(var(--topbar-h, 40px) + 8px);");
    // Every card grows to the shared bottom margin, then scrolls inside.
    expect(cssRule(MODAL_CSS, ".modal.top-dialog > *")).toBe("max-height: var(--dlg-max-h);");
  });

  it("is written once: no other sheet defines the anchor or the top", () => {
    expect(ALL_CSS.split("--dlg-anchor-h:").length - 1).toBe(1);
    expect(ALL_CSS.split("--dlg-top:").length - 1).toBe(2); // base + phone
  });

  it.each(DIALOGS)("%s carries it, and its card sets no top, centring or ceiling", (file, shell, card, sheets) => {
    const tags = tagsWithId(src(file), shell);
    expect(tags).toHaveLength(1);
    const cls = attr(tags[0], "className") ?? "";
    expect(cls.split(/\s+/)).toContain("top-dialog");
    for (const sheet of sheets) {
      const css = src(sheet);
      for (const sel of ["#" + card, "#" + shell + " > #" + card, "#" + shell]) {
        const body = cssRule(css, sel);
        expect(body, sel + " in " + sheet).not.toMatch(/(^|[;\s])max-height:/);
        expect(body, sel + " in " + sheet).not.toMatch(/margin-top:|align-self:|align-items:|padding-top:/);
      }
    }
  });

  it("leaves the per-dialog anchors it replaced gone", () => {
    const flat = squash(ALL_CSS);
    for (const gone of ["nf-landing", "--nf-top", "--nf-anchor-h", "min(8vh, 64px)", "min(84vh, 760px)"]) {
      expect(flat).not.toContain(gone);
    }
  });
});
