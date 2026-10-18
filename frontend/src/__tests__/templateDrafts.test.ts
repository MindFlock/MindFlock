/** A template's draft is never thrown away or saved over another one by
 * accident: another template's name is refused (the server upserts by name),
 * and leaving an editor with changes asks "Discard changes?" first. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code, squash } from "./sourceText";

vi.mock("../api/client", () => ({ api: vi.fn() }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { TEMPLATES_UI_INIT, TemplatesDialogView, formDirty, formFromTemplate, needsDiscard, templateBody, templatesReducer } =
  await import("../components/dialogs/TemplatesDialog");
const { templateNameTaken } = await import("../components/dialogs/newSession");

const T = [{ name: "Nightly", prompt: "x", repo_path: "/r" }, { name: "docs" }];

describe("a template name already in use is refused", () => {
  it("ignoring case, unless it is the template being edited", () => {
    expect(templateNameTaken("nightly", T)).toBe("A template named “Nightly” already exists — pick another name.");
    expect(templateNameTaken(" NIGHTLY ", T, "Nightly")).toBe("");
    expect(templateNameTaken("fresh", T)).toBe("");
    expect(templateNameTaken("", T)).toBe("");
  });

  it("templateBody refuses it for a new template and for a rename onto it", () => {
    const f = { ...formFromTemplate(null), name: "nightly" };
    expect(templateBody(f, null, T, "")).toEqual({ error: "A template named “Nightly” already exists — pick another name." });
    expect(templateBody({ ...formFromTemplate(T[1]), name: "Nightly" }, null, T, "docs")).toHaveProperty("error");
    expect(templateBody({ ...formFromTemplate(T[0]) }, null, T, "Nightly")).toHaveProperty("body");
  });

  it("Schedule and New's Save as template both check it", () => {
    expect(code("components/dialogs/TemplatesDialog.tsx")).toContain("templateBody(f, installed, templates, ui.editing)");
    expect(squash(code("components/dialogs/NewSessionDialog.tsx"))).toContain(
      'templateNameTaken(saveTplName || "", device ? templates : templatesNow())'
    );
  });
});

describe("leaving an edited template asks first", () => {
  const seed = formFromTemplate(T[0]);
  const editing = templatesReducer(TEMPLATES_UI_INIT, { t: "edit", name: "Nightly" });

  it("formDirty compares against what the editor opened with", () => {
    expect(formDirty(seed, seed)).toBe(false);
    expect(formDirty({ ...seed, prompt: "y" }, seed)).toBe(true);
    expect(formDirty(seed, null)).toBe(false);
  });

  it("Edit / + New / Delete / Escape ask when dirty; Cancel, a clean form and the same template don't", () => {
    for (const a of [
      { t: "edit", name: "docs" },
      { t: "new" },
      { t: "ask-delete", name: "docs" },
      { t: "schedule", name: "docs" },
      { t: "dismiss" },
    ] as const)
      expect(needsDiscard(editing, a, true)).toBe(true);
    expect(needsDiscard(editing, { t: "cancel" }, true)).toBe(false);
    expect(needsDiscard(editing, { t: "new" }, false)).toBe(false);
    expect(needsDiscard(editing, { t: "edit", name: "Nightly" }, true)).toBe(false);
    expect(needsDiscard(TEMPLATES_UI_INIT, { t: "new" }, true)).toBe(false);
  });

  it("the question keeps the editor open; Keep editing withdraws it", () => {
    const asked = templatesReducer(editing, { t: "ask-discard", then: { t: "new" } });
    expect(asked.editing).toBe("Nightly");
    expect(asked.discarding).toEqual({ then: { t: "new" } });
    expect(templatesReducer(asked, { t: "keep-editing" }).discarding).toBeUndefined();
  });

  it("the row renders inside the editing card, and a dirty editor ignores the backdrop", () => {
    const html = renderToStaticMarkup(
      createElement(TemplatesDialogView, {
        templates: T,
        ui: { ...editing, discarding: { then: "close" } },
        form: seed,
        busy: false,
        loaded: true,
        onForm: () => {},
        dispatch: () => {},
        onSave: () => {},
        onDelete: () => {},
        onClose: () => {},
        dirty: true,
      })
    );
    expect(html).toContain('class="tpl-confirm tpl-discard" role="alert"');
    expect(html).toContain("Discard changes to “Nightly”?");
    expect(html).toContain("Keep editing");
    expect(code("components/dialogs/TemplatesDialog.tsx")).toContain("closeOnBackdrop={!p.dirty}");
  });
});
