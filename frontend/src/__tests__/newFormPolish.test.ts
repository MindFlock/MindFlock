/** The New form's polish pass: the one-line folder suggestions with their
 * "N more…" chip, the structured "Works in" answers, and the Task box that
 * starts short and grows. */

import { describe, expect, it } from "vitest";
import { foldChipRow } from "../components/dialogs/newSession";
import { cssRule, squash, src } from "./sourceText";

const CSS = src("components/dialogs/NewSessionDialog.css");
const FOLDER = src("components/dialogs/newSession/FolderPicker.tsx");
const WORKSPACE = src("components/dialogs/newSession/WorkspaceFold.tsx");
const TEMPLATES = src("components/dialogs/TemplateEditor.tsx");
const PROMPT = src("components/dialogs/newSession/PromptFold.tsx");

describe("foldChipRow: which suggestion chips the one line hides", () => {
  const L = (right: number) => ({ label: true, right });
  const C = (right: number) => ({ label: false, right });

  it("hides nothing (and needs no More chip) when everything fits", () => {
    expect(foldChipRow([L(50), C(120), C(200)], 600, 80)).toEqual([false, false, false]);
  });

  it("keeps room for the More chip once anything overflows", () => {
    // Row ends at 600; the More chip needs 80, so the line really ends at 520.
    expect(foldChipRow([L(50), C(300), C(510), C(560), C(700)], 600, 80)).toEqual([
      false,
      false,
      false,
      true,
      true,
    ]);
  });

  it("drops a group label left with no visible chip after it", () => {
    // "Home" fits, but every chip of its run is past the edge.
    expect(foldChipRow([L(50), C(300), L(360), C(560), C(700)], 600, 80)).toEqual([
      false,
      false,
      true,
      true,
      true,
    ]);
  });
});

describe("the folder suggestions are one quiet line", () => {
  it("is no longer a filled card with a heading", () => {
    expect(FOLDER).not.toContain("Folders you might mean — click one to use it");
    expect(FOLDER).toContain('className={"nf-suggest nf-folders" + (open ? " open" : "")}');
    expect(FOLDER).not.toContain('className="new-templates nf-suggest"');
    const row = cssRule(CSS, ".nf-suggest.nf-folders");
    expect(row).toContain("flex-wrap: nowrap;");
    expect(row).toContain("overflow: hidden;");
    // In a .nf-body that scrolls, an overflow:hidden child shrinks to 0.
    expect(row).toContain("flex: none;");
  });

  it("puts the rest behind an N more… chip that unfolds the line", () => {
    // #new-suggest-more in New (scope "new"), #tpl-suggest-more in a template's editor.
    expect(FOLDER).toContain('id={scope + "-suggest-more"}');
    expect(FOLDER).toContain("aria-expanded={open}");
    expect(FOLDER).toContain('more.textContent = n + " more…";');
    expect(cssRule(CSS, ".nf-suggest.nf-folders.open")).toContain("flex-wrap: wrap;");
  });

  it("still never repeats the field and still fills it through the reducer", () => {
    expect(FOLDER).toContain("suggestions.every((s) => s.path === folderPath)");
    expect(FOLDER).toContain('folderDo({ t: "user-set", path: s.path });');
  });
});

describe("Works in: two structured answers", () => {
  it("draws title and one muted line in a column beside the radio", () => {
    expect(WORKSPACE).toContain('<span className="nf-opt-title">{title}</span>');
    expect(WORKSPACE).toContain('<span className="nf-opt-desc">{desc}</span>');
    // Two in WorksInGroup, two in PlainWorksIn (a folder with no repo).
    expect(WORKSPACE.split('<label className="check nf-opt">').length - 1).toBe(4);
    const opt = squash(CSS).split(":is(#new-form, .tpl-editor) label.check.nf-opt {")[1].split("}")[0];
    expect(opt).toContain("display: grid;");
    expect(opt).toContain("grid-template-columns: 16px minmax(0, 1fr);");
    // Lines that belong to an answer hang on the text's edge: 16px + 9px gap.
    expect(cssRule(CSS, ".nf-opt-sub")).toContain("margin: -6px 0 0 25px;");
  });

  it("shows the branch on ONE ellipsised line with the whole of it as a title", () => {
    const line = cssRule(CSS, ".nf-branch-line");
    expect(line).toContain("white-space: nowrap;");
    expect(line).toContain("text-overflow: ellipsis;");
    expect(WORKSPACE).toContain('title={full}');
    // .rt-ctl's wrap would size the column to the widest line.
    expect(cssRule(CSS, ".nf-works-in .nf-mode")).toContain("flex-wrap: nowrap;");
  });

  it("the Templates editor draws the same row, not a copy of it", () => {
    // WorksInGroup is the one place the two answers are drawn; the editor
    // only hangs its provisioning box and git init on it.
    expect(TEMPLATES).toContain("<WorksInGroup");
    expect(TEMPLATES).toContain('scope="tpl"');
    expect(TEMPLATES).not.toContain('type="radio"');
    expect(TEMPLATES).not.toContain("<WorktreeText");
  });
});

describe("the Task box", () => {
  it("starts at two rows and grows with its text, then scrolls", () => {
    expect(PROMPT).toContain("rows={2}");
    expect(PROMPT).toContain("const TASK_MAX_PX = 220;");
    expect(PROMPT).toContain("Math.min(TASK_MAX_PX, ta.scrollHeight + edge)");
  });
});
