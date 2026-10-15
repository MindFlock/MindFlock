/** The template editor (Schedule → + New template / Edit / Schedule…) is the
 * New form's own parts — its Folder field with Browse… and the Recent / Home
 * chip line, its Agent picker, its growing Task box and its "Works in" row —
 * not a copy of them: the owner's "where is the folder explorer we have like
 * in New?", and Agent and Folder sitting on different baselines because the
 * folder's hint wrapped onto a line of its own. It still saves exactly the
 * fields it saved before. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import EDITOR from "../components/dialogs/TemplateEditor.tsx?raw";
import { cssRule, src } from "./sourceText";

vi.mock("../api/client", () => ({ api: vi.fn() }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { TEMPLATES_UI_INIT, TemplatesDialogView, formFromTemplate, templateBody } = await import(
  "../components/dialogs/TemplatesDialog"
);
const { TEMPLATE_BROWSE_INIT, templateFolderStep } = await import("../components/dialogs/TemplateEditor");

const CSS = src("components/dialogs/NewSessionDialog.css");

const NIGHTLY = {
  name: "nightly-deps",
  program: "claude",
  repo_path: "/home/me/projects/app",
  prompt: "update deps and fix what breaks",
  provisioned: true,
  in_place: false,
  workspace_strategy: "clone",
  init_repo: false,
  autopilot: "pr" as const,
  schedule: { at: "21:00", days: "weekdays" as const },
};

const SUGGEST = [
  { path: "/home/me/projects/app", name: "app", is_git: true, source: "recent" },
  { path: "/home/me/notes", name: "notes", is_git: false, source: "nearby" },
];

function editor(over: Partial<Parameters<typeof TemplatesDialogView>[0]> = {}) {
  return renderToStaticMarkup(
    createElement(TemplatesDialogView, {
      templates: [NIGHTLY],
      ui: { ...TEMPLATES_UI_INIT, editing: "" },
      form: formFromTemplate(null),
      busy: false,
      loaded: true,
      onForm: () => {},
      dispatch: () => {},
      onSave: () => {},
      onDelete: () => {},
      onClose: () => {},
      agents: ["claude", "codex"],
      folderSuggestions: SUGGEST,
      homePath: "/home/me",
      ...over,
    })
  );
}

describe("the template editor draws the New form's parts", () => {
  it("Folder is New's field: Browse… beside it, the chip line under it", () => {
    const html = editor();
    expect(html).toContain('id="tpl-repo-path"');
    expect(html).toContain('role="combobox"');
    expect(html).toMatch(/<span class="repo-path-row"><input[^>]*id="tpl-repo-path"[^>]*>.*?id="tpl-browse" class="repo-browse"[^>]*>Browse…<\/button>/);
    // One line of one-click folders, said as an action ("Use: 📦 app"), not
    // a status ("Started in", "Recent") — r5-schedule finding 13. Only repos
    // and recent folders: a stray notes folder isn't offered.
    expect(html).toContain('id="tpl-suggest"');
    expect(html).toContain('class="nf-suggest nf-folders tpl-use"');
    expect(html).toMatch(/<span class="nf-suggest-label">Use:<\/span><button[^>]*data-path="\/home\/me\/projects\/app"[^>]*>📦 app<\/button>/);
    expect(html).not.toContain('data-path="/home/me/notes"');
    expect(html).not.toMatch(/>(Started in|Recent)</);
    // The old bare text box is gone.
    expect(html).not.toContain('id="tpl-repo"');
  });

  it("the folder's hint is the input's placeholder, never a line that pushes it down", () => {
    const html = editor();
    expect(html).toContain('placeholder="The repo it works in"');
    expect(html).not.toContain("— needed to Run now or schedule");
    // Folder and Agent share New's row, each a label with one line of head
    // (Folder in a wrap "Add one" rings and the tour points at).
    expect(html).toMatch(
      /<div class="nf-quick"><div class="tpl-folder-wrap" data-tour="schedule-folder"><label class="nf-folder"[^>]*>Folder<span class="repo-path-row">/
    );
    // One height for the input, Browse… and the select: one baseline.
    const row = CSS.slice(CSS.indexOf(".tpl-editor .nf-quick input,"));
    expect(row.slice(0, row.indexOf("}"))).toContain(".tpl-editor .nf-quick select,");
    expect(row.slice(0, row.indexOf("}"))).toContain("height: 40px;");
  });

  it("Agent is New's picker, with the template's own 'Your default agent'", () => {
    const html = editor();
    expect(html).toContain('<select id="tpl-program"');
    expect(html).toMatch(/<option value=""( selected="")?>Your default agent<\/option>/);
    expect(html).toContain('<option value="codex">codex</option>');
    expect(html).toContain('id="tpl-agent-manage"');
  });

  it("Task is New's box: two rows to start, growing with its text", () => {
    const html = editor();
    expect(html).toMatch(/<textarea id="tpl-prompt"[^>]*rows="2"/);
    expect(html).toContain("Task <span class=\"muted\">— what each session from it starts on</span>");
    // No "Insert a prompt…" here: its Manage… hands back to the New form.
    expect(html).not.toContain('id="new-preset"');
  });

  it("Works in is New's row, with provisioning and git init as stacked boxes", () => {
    const html = editor();
    expect(html).toContain('id="tpl-works-in" class="rt-row rt-row-top nf-works-in"');
    expect(html).toContain('id="tpl-worktree"');
    expect(html).toContain('id="tpl-in-place"');
    expect(html).toContain('<span class="nf-rec">recommended</span>');
    expect(html).toMatch(/class="check nf-opt nf-opt-check"><input type="checkbox" id="tpl-provision"/);
    expect(html).toMatch(/class="check nf-opt nf-opt-check"><input type="checkbox" id="tpl-init"/);
    expect(html).toContain('<span class="nf-opt-title">Provision the worktree</span>');
  });

  it("Schedule and Fast-track sit in the same label column", () => {
    const html = editor({ form: { ...formFromTemplate(NIGHTLY) }, ui: { ...TEMPLATES_UI_INIT, editing: "nightly-deps" } });
    expect(html).toContain('id="tpl-schedule-row"');
    expect(html).toMatch(/<span class="rt-label" id="tpl-schedule-label">Schedule<\/span>/);
    // GLOSSARY round 5: Fast-track, with its five levels, not "When a run's
    // turn ends" / "Leave it for me" / "Commit + push" / "Open a PR".
    expect(html).toMatch(/<span class="rt-label" id="tpl-autopilot-label">Fast-track<\/span>/);
    expect(html).toContain("When the agent stops. Applies to Run now and to scheduled runs.");
    expect(html).toContain('id="tpl-at"');
    expect(html).toContain('<option value="pr" selected="">Open PR</option>');
    expect(html).toContain('<option value="">Off</option>');
    // never Merge when green: a template's runs are unattended
    expect(html).not.toContain('value="merge"');
    expect(html).not.toMatch(/When a run|Leave it for me|Commit \+ push|each Run now/);
    expect(cssRule(CSS, ".tpl-editor .rt-row")).toContain("grid-template-columns: 150px minmax(0, 1fr);");
  });

  it("deletes the editor's own copies (markup and CSS)", () => {
    for (const gone of ["tpl-two", "tpl-mode", 'type="radio"', "<WorktreeText"]) expect(EDITOR).not.toContain(gone);
    expect(CSS).not.toContain(".tpl-two");
    expect(CSS).not.toContain(".tpl-mode");
    expect(CSS).not.toContain(".tpl-editor label {");
    for (const used of ["<FolderField", "<FolderAids", "<AgentFields", "<TaskField", "<WorksInGroup"])
      expect(EDITOR).toContain(used);
  });
});

describe("the editor saves what it saved before", () => {
  it("round-trips every stored field", () => {
    const r = templateBody(formFromTemplate(NIGHTLY));
    expect(r).toEqual({
      body: {
        name: "nightly-deps",
        program: "claude",
        repo_path: "/home/me/projects/app",
        prompt: "update deps and fix what breaks",
        provisioned: true,
        in_place: false,
        workspace_strategy: "clone",
        init_repo: false,
        schedule: { at: "21:00", days: "weekdays" },
        autopilot: "pr",
      },
    });
  });

  it("still refuses a scheduled template with no folder", () => {
    const f = { ...formFromTemplate(NIGHTLY), mode: "worktree" as const, repo_path: "" };
    expect(templateBody(f)).toEqual({ error: "A scheduled template needs a folder to start its session in." });
  });
});

describe("Browse… on a path the form owns", () => {
  it("selecting fills the field, Escape puts it back, use-this-folder keeps it", () => {
    let st = templateFolderStep("/home/me/a", TEMPLATE_BROWSE_INIT, { t: "browse-open" });
    expect(st.browse.browsing).toBe(true);
    st = templateFolderStep(st.path, st.browse, { t: "browse-select", path: "/home/me/b" });
    expect(st.path).toBe("/home/me/b");
    const cancelled = templateFolderStep(st.path, st.browse, { t: "browse-cancel" });
    expect(cancelled).toEqual({ path: "/home/me/a", browse: TEMPLATE_BROWSE_INIT });
    const kept = templateFolderStep(st.path, st.browse, { t: "browse-commit", path: "/home/me/b" });
    expect(kept.path).toBe("/home/me/b");
    expect(kept.browse.browsing).toBe(false);
  });

  it("a chip or typing sets the path through the same reducer", () => {
    expect(templateFolderStep("", TEMPLATE_BROWSE_INIT, { t: "user-set", path: "/home/me/notes" }).path).toBe("/home/me/notes");
  });

  it("the dialog's Escape asks the editor first, so it closes the browser before the editor", () => {
    const dlg = src("components/dialogs/TemplatesDialog.tsx");
    expect(dlg).toContain("if (ui.editing !== null && escapeRef.current?.()) return true;");
    expect(EDITOR).toContain('folderDo({ t: "browse-cancel" });');
  });
});
