/** Templates dialog: the ONE list of saved New-session setups (server-side,
 * `/api/templates`, synced between devices), managed here — list, edit
 * inline, delete behind an inline confirm row. Opened from the New dialog's
 * Templates strip ("Manage…", always shown) and the command palette
 * ("Templates…"). It replaces the vanilla-JS addon modal, which asked
 * `window.confirm` on Delete — a call that does nothing in Electron — and
 * never looked at the server's answer.
 *
 * Every confirm is a row in the card, never a browser dialog: Electron has
 * no window.confirm/prompt/alert, and a Delete that silently does nothing is
 * the bug this dialog exists to not have again.
 *
 * The state machine is a pure reducer (templatesReducer) and the markup a
 * props-only view (TemplatesDialogView), so both are tested without a DOM. */

import { useEffect, useReducer, useRef, useState } from "react";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
import { errMsg } from "../../lib/format";
import {
  TEMPLATES_CHANGED,
  deleteTemplate,
  ensureTemplates,
  saveTemplate,
  templatesNow,
  type Template,
} from "../../lib/templates";

/** The dialog's UI state: which card is open for editing ("" = the + New
 * form, null = none) and which card is asking "delete?". One at a time —
 * opening an editor closes the question and the other way round, so the
 * card never shows two things the next click could mean. */
export interface TemplatesUiState {
  editing: string | null;
  confirming: string | null;
  error: string;
}

export type TemplatesUiAction =
  | { t: "edit"; name: string }
  | { t: "new" }
  | { t: "ask-delete"; name: string }
  | { t: "cancel" }
  | { t: "saved" }
  | { t: "deleted" }
  | { t: "error"; message: string };

export const TEMPLATES_UI_INIT: TemplatesUiState = { editing: null, confirming: null, error: "" };

export function templatesReducer(s: TemplatesUiState, a: TemplatesUiAction): TemplatesUiState {
  switch (a.t) {
    case "edit":
      return { editing: a.name, confirming: null, error: "" };
    case "new":
      return { editing: "", confirming: null, error: "" };
    case "ask-delete":
      return { editing: null, confirming: a.name, error: "" };
    case "cancel":
    case "saved":
    case "deleted":
      return { editing: null, confirming: null, error: "" };
    case "error":
      return { ...s, error: a.message };
  }
}

/** The fields the form edits, in the shape POST /api/templates stores. */
export interface TemplateForm {
  name: string;
  program: string;
  repo_path: string;
  prompt: string;
  /** "worktree" | "in_place" | "provisioned" — the New dialog's three answers
   * to "where does the work happen", stored as the two booleans. */
  mode: "worktree" | "in_place" | "provisioned";
  workspace_strategy: string;
  init_repo: boolean;
}

export function formFromTemplate(t: Template | null): TemplateForm {
  return {
    name: t?.name || "",
    program: t?.program || "",
    repo_path: t?.repo_path || "",
    prompt: t?.prompt || "",
    mode: t?.provisioned ? "provisioned" : t?.in_place ? "in_place" : "worktree",
    workspace_strategy: t?.workspace_strategy === "clone" ? "clone" : "worktree",
    init_repo: !!t?.init_repo,
  };
}

/** The POST body for a form, or the reason it cannot be saved. */
export function templateBody(f: TemplateForm): { body: Template } | { error: string } {
  const name = f.name.trim();
  if (!name) return { error: "A template needs a name." };
  if (name.length > 100) return { error: "Names are at most 100 characters." };
  return {
    body: {
      name,
      program: f.program.trim(),
      repo_path: f.repo_path.trim(),
      prompt: f.prompt,
      provisioned: f.mode === "provisioned",
      in_place: f.mode === "in_place",
      workspace_strategy: f.workspace_strategy === "clone" ? "clone" : "worktree",
      init_repo: f.init_repo,
    },
  };
}

/** A rename is a save under the new name AND a delete of the old one — the
 * server upserts by name, so without the delete both would remain. Same
 * name ignoring case is the server's own overwrite: nothing to delete. */
export function renameLeaves(oldName: string, newName: string): string | null {
  const a = oldName.trim().toLowerCase();
  const b = newName.trim().toLowerCase();
  return a && a !== b ? oldName : null;
}

/** One line about a template, for the card under its name. */
export function templateMeta(t: Template): string {
  const bits: string[] = [];
  if (t.program) bits.push(t.program);
  if (t.repo_path) bits.push(t.repo_path);
  bits.push(t.provisioned ? "provisioned" + (t.workspace_strategy === "clone" ? " (clone)" : "") : t.in_place ? "in this folder" : "new worktree");
  if (t.init_repo) bits.push("git init");
  return bits.join(" · ");
}

interface ViewProps {
  templates: Template[];
  ui: TemplatesUiState;
  /** The form being edited (when ui.editing is not null). */
  form: TemplateForm;
  busy: boolean;
  loaded: boolean;
  /** Why the list could not be fetched on open ("" = it was, or still is
   * loading). Shown at list level: `ui.error` only renders inside the editor
   * or a confirming card, so a failed GET used to leave "Loading templates…"
   * up forever. */
  loadError?: string;
  onForm: (patch: Partial<TemplateForm>) => void;
  dispatch: (a: TemplatesUiAction) => void;
  onSave: () => void;
  onDelete: (name: string) => void;
  onClose: () => void;
}

/** Markup only: everything it shows comes in as props. */
export function TemplatesDialogView(p: ViewProps) {
  const { templates, ui, form, busy, loaded } = p;
  const loadError = p.loadError || "";
  const editor = (
    <form
      className="tpl-editor"
      onSubmit={(e) => {
        e.preventDefault();
        p.onSave();
      }}
    >
      <label>
        Name
        <input
          id="tpl-name"
          autoComplete="off"
          value={form.name}
          onChange={(e) => p.onForm({ name: e.target.value })}
          placeholder="e.g. fix-tests"
        />
      </label>
      <div className="tpl-two">
        <label>
          Agent <span className="muted">— optional; the default CLI when blank</span>
          <input
            id="tpl-program"
            autoComplete="off"
            value={form.program}
            onChange={(e) => p.onForm({ program: e.target.value })}
            placeholder="claude"
          />
        </label>
        <label>
          Folder <span className="muted">— optional</span>
          <input
            id="tpl-repo"
            autoComplete="off"
            value={form.repo_path}
            onChange={(e) => p.onForm({ repo_path: e.target.value })}
            placeholder="/home/me/projects/foo"
          />
        </label>
      </div>
      <label>
        Prompt <span className="muted">— sent to the agent at launch; a prompt-only template is a saved prompt</span>
        <textarea
          id="tpl-prompt"
          rows={4}
          value={form.prompt}
          onChange={(e) => p.onForm({ prompt: e.target.value })}
        />
      </label>
      <div className="tpl-mode" role="radiogroup" aria-label="Where the work happens">
        {(
          [
            ["worktree", "New worktree"],
            ["in_place", "Work directly in the folder"],
            ["provisioned", "Provision workspace"],
          ] as Array<[TemplateForm["mode"], string]>
        ).map(([mode, label]) => (
          <label key={mode} className="check">
            <input
              type="radio"
              name="tpl-mode"
              checked={form.mode === mode}
              onChange={() => p.onForm({ mode })}
            />
            {label}
          </label>
        ))}
        {form.mode === "provisioned" && (
          <select
            id="tpl-strategy"
            value={form.workspace_strategy}
            onChange={(e) => p.onForm({ workspace_strategy: e.target.value })}
          >
            <option value="worktree">shared base clone (worktree)</option>
            <option value="clone">full clone</option>
          </select>
        )}
        <label className="check">
          <input
            type="checkbox"
            id="tpl-init"
            checked={form.init_repo}
            onChange={(e) => p.onForm({ init_repo: e.target.checked })}
          />
          Create a git repo in the folder
        </label>
      </div>
      {ui.error && (
        <p className="error" aria-live="polite">
          {ui.error}
        </p>
      )}
      <div className="tpl-row-actions">
        <button type="button" className="linklike" onClick={() => p.dispatch({ t: "cancel" })}>
          Cancel
        </button>
        <button type="submit" disabled={busy}>
          {busy ? "Saving…" : "Save"}
        </button>
      </div>
    </form>
  );

  return (
    <div
      id="templates-dialog"
      className="modal"
      onClick={(e) => {
        if (e.target === e.currentTarget) p.onClose();
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          if (ui.editing !== null || ui.confirming) p.dispatch({ t: "cancel" });
          else p.onClose();
        }
      }}
    >
      <div id="templates-card" role="dialog" aria-modal="true" aria-labelledby="templates-title">
        <div className="tpl-head">
          <h2 id="templates-title">Templates</h2>
          <button type="button" id="templates-close" onClick={p.onClose}>
            Close
          </button>
        </div>
        <p className="tpl-intro muted">
          A template is a saved New-session setup — agent, folder, workspace and first prompt — and the
          list is the same on every device. The New dialog's Name → Enter reuses the one you last
          picked for that folder. A template that is only a prompt is also what the Prompts bar pastes.
        </p>
        <div className="tpl-list" id="templates-list">
          {ui.editing === "" ? (
            editor
          ) : (
            <button
              type="button"
              id="templates-new"
              className="tpl-new"
              onClick={() => p.dispatch({ t: "new" })}
            >
              + New template
            </button>
          )}
          {loadError && (
            <p className="error tpl-load-error" role="alert">
              Could not load templates: {loadError}
            </p>
          )}
          {!loaded && !loadError && templates.length === 0 && (
            <p className="tpl-empty muted">Loading templates…</p>
          )}
          {loaded && templates.length === 0 && (
            <p className="tpl-empty muted">No templates yet — save one above, or from the New dialog's Prompt fold.</p>
          )}
          {templates.map((t) =>
            ui.editing === t.name ? (
              <div key={t.name} className="tpl-card tpl-card-editing" data-name={t.name}>
                {editor}
              </div>
            ) : (
              <div key={t.name} className="tpl-card" data-name={t.name}>
                <div className="tpl-card-top">
                  <div className="tpl-card-text">
                    <div className="tpl-name">{t.name}</div>
                    <div className="tpl-meta">{templateMeta(t)}</div>
                    {t.prompt && <div className="tpl-prompt">{t.prompt}</div>}
                  </div>
                  <div className="tpl-actions">
                    <button type="button" className="tpl-btn" onClick={() => p.dispatch({ t: "edit", name: t.name })}>
                      Edit
                    </button>
                    <button
                      type="button"
                      className="tpl-btn tpl-del"
                      onClick={() => p.dispatch({ t: "ask-delete", name: t.name })}
                      disabled={busy}
                    >
                      Delete
                    </button>
                  </div>
                </div>
                {ui.confirming === t.name && (
                  <div className="tpl-confirm" role="group" aria-live="polite">
                    <span>
                      Delete “{t.name}”? It goes from every device.
                    </span>
                    <button
                      type="button"
                      className="tpl-btn tpl-del tpl-confirm-yes"
                      disabled={busy}
                      onClick={() => p.onDelete(t.name)}
                    >
                      {busy ? "Deleting…" : "Delete"}
                    </button>
                    <button type="button" className="tpl-btn" onClick={() => p.dispatch({ t: "cancel" })}>
                      Keep
                    </button>
                  </div>
                )}
                {ui.confirming === t.name && ui.error && (
                  <p className="error" aria-live="polite">
                    {ui.error}
                  </p>
                )}
              </div>
            )
          )}
        </div>
      </div>
    </div>
  );
}

export function TemplatesDialog() {
  const open = useUi((s) => s.openDialog === "templates");
  const target = useUi((s) => s.dialogTarget);
  const closeDialog = useUi((s) => s.closeDialog);
  const [templates, setList] = useState<Template[]>(() => templatesNow());
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [ui, dispatch] = useReducer(templatesReducer, TEMPLATES_UI_INIT);
  const [form, setForm] = useState<TemplateForm>(() => formFromTemplate(null));
  const [busy, setBusy] = useState(false);
  const editingRef = useRef<string | null>(null);

  // The list from the server on every open, and again whenever anything
  // (this dialog, the New dialog's "Save prompt", a sync pull) changes it.
  useEffect(() => {
    if (!open) return;
    dispatch({ t: "cancel" });
    setBusy(false);
    setLoadError("");
    let live = true;
    const reload = () => setList(templatesNow());
    document.addEventListener(TEMPLATES_CHANGED, reload);
    ensureTemplates(true)
      .then((list) => {
        if (!live) return;
        setList(list);
        setLoaded(true);
      })
      .catch((err) => {
        if (live) setLoadError(errMsg(err));
      });
    return () => {
      live = false;
      document.removeEventListener(TEMPLATES_CHANGED, reload);
    };
  }, [open]);

  // Opening an editor seeds the form from the card it is for.
  useEffect(() => {
    if (ui.editing === editingRef.current) return;
    editingRef.current = ui.editing;
    if (ui.editing === null) return;
    const t = ui.editing ? templates.find((x) => x.name === ui.editing) || null : null;
    setForm(formFromTemplate(t));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ui.editing]);

  if (!open) return null;

  const close = () => {
    closeDialog();
    // Came from the New dialog: hand the person back to it, with the list
    // they just edited reloaded by its own open.
    if (target === "new") useUi.getState().openDialogFor("new-session");
  };

  const save = async () => {
    const r = templateBody(form);
    if ("error" in r) {
      dispatch({ t: "error", message: r.error });
      return;
    }
    setBusy(true);
    try {
      await saveTemplate(r.body);
      const gone = ui.editing ? renameLeaves(ui.editing, r.body.name) : null;
      if (gone) await deleteTemplate(gone);
      setList(templatesNow());
      dispatch({ t: "saved" });
      toast(`Saved template “${r.body.name}”`);
    } catch (err) {
      dispatch({ t: "error", message: errMsg(err) });
    } finally {
      setBusy(false);
    }
  };

  const del = async (name: string) => {
    setBusy(true);
    try {
      const ok = await deleteTemplate(name);
      setList(templatesNow());
      dispatch({ t: "deleted" });
      // The old modal never looked: a 403 (not the owner) or a name the
      // server no longer had read as "deleted" while the card stayed put.
      toast(ok ? `Deleted template “${name}”` : `“${name}” was already gone`);
    } catch (err) {
      dispatch({ t: "error", message: "Couldn't delete: " + errMsg(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <TemplatesDialogView
      templates={templates}
      ui={ui}
      form={form}
      busy={busy}
      loaded={loaded}
      loadError={loadError}
      onForm={(patch) => setForm((f) => ({ ...f, ...patch }))}
      dispatch={dispatch}
      onSave={() => void save()}
      onDelete={(name) => void del(name)}
      onClose={close}
    />
  );
}
