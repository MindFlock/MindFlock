/** TemplatesDialog — the one Templates list's manager: list, edit inline,
 * delete behind an INLINE confirm row (never window.confirm, which does
 * nothing in Electron). The reducer and the body builder are pure; the view
 * is props-only and rendered through react-dom/server. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import SRC from "../components/dialogs/TemplatesDialog.tsx?raw";

vi.mock("../api/client", () => ({ api: vi.fn() }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const {
  TEMPLATES_UI_INIT,
  TemplatesDialogView,
  formFromTemplate,
  renameLeaves,
  templateBody,
  templateMeta,
  templatesReducer,
} = await import("../components/dialogs/TemplatesDialog");


const T = [
  { name: "fix-tests", program: "claude", prompt: "fix the tests", in_place: false },
  { name: "docs", prompt: "write docs", in_place: true, init_repo: true },
];

function view(over: Partial<Parameters<typeof TemplatesDialogView>[0]> = {}) {
  return renderToStaticMarkup(
    createElement(TemplatesDialogView, {
      templates: T,
      ui: TEMPLATES_UI_INIT,
      form: formFromTemplate(null),
      busy: false,
      loaded: true,
      onForm: () => {},
      dispatch: () => {},
      onSave: () => {},
      onDelete: () => {},
      onClose: () => {},
      ...over,
    })
  );
}

describe("templatesReducer — one thing open at a time", () => {
  it("Delete asks first: the card gets a confirm row, nothing is deleted yet", () => {
    const s = templatesReducer(TEMPLATES_UI_INIT, { t: "ask-delete", name: "docs" });
    expect(s).toEqual({ editing: null, confirming: "docs", error: "" });
  });
  it("Keep (or Escape) withdraws the question", () => {
    const asked = templatesReducer(TEMPLATES_UI_INIT, { t: "ask-delete", name: "docs" });
    expect(templatesReducer(asked, { t: "cancel" })).toEqual(TEMPLATES_UI_INIT);
  });
  it("opening an editor closes the question, and the other way round", () => {
    const asked = templatesReducer(TEMPLATES_UI_INIT, { t: "ask-delete", name: "docs" });
    expect(templatesReducer(asked, { t: "edit", name: "fix-tests" })).toEqual({ editing: "fix-tests", confirming: null, error: "" });
    const editing = templatesReducer(TEMPLATES_UI_INIT, { t: "new" });
    expect(editing.editing).toBe("");
    expect(templatesReducer(editing, { t: "ask-delete", name: "docs" }).editing).toBeNull();
  });
  it("a save or a delete lands back on the plain list; an error stays where it was raised", () => {
    const editing = templatesReducer(TEMPLATES_UI_INIT, { t: "edit", name: "docs" });
    expect(templatesReducer(editing, { t: "error", message: "nope" })).toEqual({ editing: "docs", confirming: null, error: "nope" });
    expect(templatesReducer(editing, { t: "saved" })).toEqual(TEMPLATES_UI_INIT);
    expect(templatesReducer(templatesReducer(TEMPLATES_UI_INIT, { t: "ask-delete", name: "docs" }), { t: "deleted" })).toEqual(TEMPLATES_UI_INIT);
  });
});

describe("templateBody / formFromTemplate — the stored shape both ways", () => {
  it("round-trips a template through the form", () => {
    const f = formFromTemplate({ name: "p", program: "codex", repo_path: "/r", prompt: "x", provisioned: true, workspace_strategy: "clone", init_repo: true });
    expect(f.mode).toBe("provisioned");
    const r = templateBody(f);
    expect("body" in r && r.body).toEqual({ name: "p", program: "codex", repo_path: "/r", prompt: "x", provisioned: true, in_place: false, workspace_strategy: "clone", init_repo: true });
    expect(formFromTemplate({ name: "q", in_place: true }).mode).toBe("in_place");
    expect(formFromTemplate(null).mode).toBe("worktree");
  });
  it("refuses a nameless or over-long name in a sentence, not a server round-trip", () => {
    expect(templateBody(formFromTemplate({ name: "  " }))).toEqual({ error: "A template needs a name." });
    expect("error" in templateBody({ ...formFromTemplate(null), name: "x".repeat(101) })).toBe(true);
  });
  it("a rename deletes the old name; a case change is the server's own overwrite", () => {
    expect(renameLeaves("docs", "Docs v2")).toBe("docs");
    expect(renameLeaves("docs", "DOCS")).toBeNull();
    expect(renameLeaves("", "x")).toBeNull();
  });
  it("sums a template up in one line", () => {
    expect(templateMeta(T[0])).toBe("claude · separate worktree");
    expect(templateMeta(T[1])).toBe("this folder directly · git init");
    // The folder's own name (the card's tooltip has the path), and the New
    // form's words for a provisioned worktree.
    expect(templateMeta({ name: "c", repo_path: "/home/me/r", provisioned: true, workspace_strategy: "clone" })).toBe(
      "r · separate worktree, provisioned (full clone)"
    );
  });
});

describe("TemplatesDialogView — the markup", () => {
  it("lists every template with Edit and Delete, and + New template", () => {
    const html = view();
    expect(html).toContain('data-name="fix-tests"');
    expect(html).toContain('data-name="docs"');
    expect(html).toContain('id="templates-new"');
    expect((html.match(/>Edit</g) || []).length).toBe(2);
    expect(html).not.toContain("tpl-confirm");
  });
  it("draws the inline confirm row on the card that asked, with Delete and Keep", () => {
    const html = view({ ui: { editing: null, confirming: "docs", error: "" } });
    const card = html.slice(html.indexOf('data-name="docs"'));
    expect(card).toContain("tpl-confirm");
    expect(card).toContain("Delete “docs”?");
    expect(card).toContain(">Keep<");
    expect(card).toContain("tpl-confirm-yes");
    const other = html.slice(html.indexOf('data-name="fix-tests"'), html.indexOf('data-name="docs"'));
    expect(other).not.toContain("tpl-confirm");
  });
  it("edits inline: the card being edited becomes the editor, seeded from it", () => {
    const html = view({ ui: { editing: "docs", confirming: null, error: "" }, form: formFromTemplate(T[1]) });
    expect(html).toContain('class="tpl-card tpl-card-editing" data-name="docs"');
    const card = html.slice(html.indexOf('data-name="docs"'));
    expect(card).toContain('value="docs"');
    expect(card).toContain("write docs");
    expect(html).toContain('id="templates-new"'); // the + New row is still there
  });
  it("+ New replaces the row with an empty editor", () => {
    const html = view({ ui: { editing: "", confirming: null, error: "" } });
    expect(html).not.toContain('id="templates-new"');
    expect(html).toContain('id="tpl-name"');
  });
  it("says so while loading; empty, it says what Schedule is for in one line and offers to start", () => {
    expect(view({ templates: [], loaded: false })).toContain("Loading templates…");
    const empty = view({ templates: [] });
    expect(empty).toContain('id="schedule-empty"');
    expect(empty).toContain("Save a session as a template once, then start it with one click");
    expect(empty).toContain('id="schedule-empty-create"');
    expect(empty).toContain(">Create a template<");
    // One way in, not two: no head "+ New template", no intro paragraph.
    expect(empty).not.toContain('id="templates-new"');
    expect(empty).not.toContain("tpl-intro");
  });
  it("shows a failed load at list level instead of loading forever", () => {
    const html = view({ templates: [], loaded: false, loadError: "HTTP 500" });
    expect(html).toContain("Could not load templates: HTTP 500");
    expect(html).toContain('role="alert"');
    expect(html).not.toContain("Loading templates…");
    // Not hidden behind the editor or a confirming card, which is all ui.error reaches.
    expect(view({ templates: [], loaded: false })).not.toContain("tpl-load-error");
  });
  it("is a [role=dialog], so the Ctrl+W / Delete guard sees it as an open overlay", async () => {
    const { OVERLAY_SELECTOR } = await import("../lib/keymap");
    expect(OVERLAY_SELECTOR).toContain('[role="dialog"]');
    expect(view()).toMatch(/id="templates-card" role="dialog" aria-modal="true"/);
    // the CALL sites, not the docstring that names them as the thing avoided
    expect(SRC).not.toMatch(/window\.(confirm|prompt|alert)\(|[^.\w](confirm|alert|prompt)\(/);
  });
});
