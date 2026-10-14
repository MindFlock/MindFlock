/** Templates dialog: the ONE list of saved New-session setups (server-side,
 * `/api/templates`, synced between devices), managed here — list, edit
 * inline, delete behind an inline confirm row. Opened from the New dialog's
 * Templates strip ("Manage…", always shown) and the command palette
 * ("Templates…"). It replaces the vanilla-JS addon modal, which asked
 * `window.confirm` on Delete — a call that does nothing in Electron — and
 * never looked at the server's answer.
 *
 * Each card also has **Run now** (a fresh session from the template, its
 * prompt already sent — lib/templateRun) and may carry a **schedule** (every
 * day or weekdays at a local time; the server's template_schedule loop
 * starts it), with the next and last run under the name.
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
import { Modal } from "../Modal";
import {
  TEMPLATES_CHANGED,
  deleteTemplate,
  ensureTemplates,
  readTemplatesTarget,
  runProblem,
  saveTemplate,
  scheduleLabel,
  templateRunsNow,
  templatesNow,
  whenLabel,
  type Template,
  type TemplateRunInfo,
  type TemplateRunsStatus,
} from "../../lib/templates";
import { runTemplateNow } from "../../lib/templateRun";
import { api } from "../../api/client";
import { LANE_LABEL, type Lane } from "../../lib/laneActions";
import { leafName, newSessionResume } from "./newSession";
import { FastTrackChoice } from "./FastTrackChoice";
import { IN_PLACE_HINT, InitRepoCheck, ProvisionCheck, StrategyPicker, WorktreeText } from "./newSession/WorkspaceFold";

/** A template as the server stores it, fast-track included (lib/templates'
 * Template predates the field). */
/** A run row, with the time zone the schedule's "at" is in. */
type RunInfoX = TemplateRunInfo & { tz?: string };

/** This browser's zone abbreviation ("EDT") — the stand-in for the
 * server's until a scheduled template's run row names it. */
function localZone(): string {
  try {
    const parts = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" }).formatToParts(new Date());
    return parts.find((x) => x.type === "timeZoneName")?.value || "";
  } catch {
    return "";
  }
}

/** The fast-track choices a template can carry (Off = none stored). */
export const TEMPLATE_LANES: Lane[] = ["leave", "commit", "push", "pr", "merge"];

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
  | { t: "schedule"; name: string }
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
    case "schedule":
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
  /** Run on a schedule: every day / weekdays at `at` (local "HH:MM"). */
  scheduled: boolean;
  at: string;
  days: "daily" | "weekdays";
  /** How far a session from it ships itself ("leave" = off). */
  fasttrack: Lane;
  ask_first: boolean;
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
    scheduled: !!t?.schedule?.at,
    at: t?.schedule?.at || "21:00",
    days: t?.schedule?.days === "weekdays" ? "weekdays" : "daily",
    fasttrack: TEMPLATE_LANES.includes(t?.fasttrack as Lane) ? (t!.fasttrack as Lane) : "leave",
    ask_first: !!t?.ask_first,
  };
}

/** The POST body for a form, or the reason it cannot be saved. */
export function templateBody(f: TemplateForm): { body: Template } | { error: string } {
  const name = f.name.trim();
  if (!name) return { error: "A template needs a name." };
  if (name.length > 100) return { error: "Names are at most 100 characters." };
  const at = f.at.trim();
  if (f.scheduled) {
    if (!/^([01]?\d|2[0-3]):[0-5]\d$/.test(at)) return { error: "Pick a time for the schedule, like 21:00." };
    if (!f.repo_path.trim() && f.mode !== "provisioned")
      return { error: "A scheduled template needs a folder to start its session in." };
    if (!f.prompt.trim()) return { error: "A scheduled template needs a task — it's what the session is told to do." };
  }
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
      ...(f.scheduled ? { schedule: { at, days: f.days } } : {}),
      ...(f.fasttrack && f.fasttrack !== "leave"
        ? { fasttrack: f.fasttrack, ...(f.ask_first ? { ask_first: true } : {}) }
        : {}),
    } as Template,
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
  // The folder's own name; the card's tooltip carries the whole path, which
  // wrapped beside the buttons into a four-line column.
  if (t.repo_path) bits.push(leafName(t.repo_path));
  // The New form's words: a separate worktree, or this folder directly, and
  // "provisioned" as the box on top of the worktree.
  bits.push(
    t.provisioned
      ? "separate worktree, provisioned" + (t.workspace_strategy === "clone" ? " (full clone)" : "")
      : t.in_place
        ? "this folder directly"
        : "separate worktree"
  );
  if (t.init_repo) bits.push("git init");
  const ft = t.fasttrack;
  if (ft && ft !== "leave" && ft in LANE_LABEL)
    bits.push("fast-track → " + LANE_LABEL[ft as Lane] + (t.ask_first ? " (asks first)" : ""));
  return bits.join(" · ");
}

/** The card's schedule line: what the schedule is, when it fires next and
 * how the last run went ("" for a template that never ran and has no
 * schedule). `now` is injectable for tests. */
export function runLine(
  t: Template,
  info: TemplateRunInfo,
  status: Pick<TemplateRunsStatus, "runs_here" | "runs_on">,
  now: Date = new Date()
): string {
  const bits: string[] = [];
  const tz = (info as RunInfoX).tz;
  if (t.schedule?.at) {
    bits.push(scheduleLabel(t.schedule) + (tz ? " " + tz : ""));
    if (!status.runs_here && status.runs_on) bits.push("starts on " + status.runs_on);
    else if (info.next_at) bits.push("next " + whenLabel(info.next_at, now));
  }
  if (info.last_at)
    bits.push(
      "last " +
        whenLabel(info.last_at, now) +
        (info.last_trigger === "schedule" ? " (scheduled)" : info.last_trigger === "manual" ? " (by hand)" : "") +
        (info.last_session ? " → " + info.last_session : "")
    );
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
  /** Next/last runs (GET /api/templates `runs`), and whether schedules fire
   * here. */
  runs?: TemplateRunsStatus;
  /** Run now: a fresh session from the template. */
  onRun?: (name: string) => void;
  /** The template whose Run now is in flight. */
  running?: string;
  /** The agents to offer in the editor (GET /api/providers/manage). */
  agents?: string[];
  /** The editor was opened by a card's Schedule…: the time takes the caret. */
  scheduleFocus?: boolean;
  /** This device's time zone ("EDT"), which a schedule's time is in. */
  tz?: string;
}

/** Markup only: everything it shows comes in as props. */
export function TemplatesDialogView(p: ViewProps) {
  const { templates, ui, form, busy, loaded } = p;
  const runs = p.runs || { runs: {}, runs_here: true, runs_on: "" };
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
          Agent
          <select
            id="tpl-program"
            value={form.program}
            onChange={(e) => p.onForm({ program: e.target.value })}
          >
            <option value="">Your default agent</option>
            {form.program && !(p.agents || []).includes(form.program) && (
              <option value={form.program}>{form.program}</option>
            )}
            {(p.agents || []).map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </label>
        <label>
          Folder <span className="muted">— needed to Run now or schedule</span>
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
        Task <span className="muted">— what the session starts on</span>
        <textarea
          id="tpl-prompt"
          rows={4}
          value={form.prompt}
          onChange={(e) => p.onForm({ prompt: e.target.value })}
        />
      </label>
      {/* Where it works: the New form's own controls and words — two answers,
          and "Provision the worktree" as a box on the worktree, not a third. */}
      <div className="tpl-mode" role="radiogroup" aria-label="Works in">
        <span className="tpl-mode-label">Works in</span>
        <label className="check">
          <input
            type="radio"
            name="tpl-mode"
            id="tpl-worktree"
            checked={form.mode !== "in_place"}
            onChange={() => p.onForm({ mode: "worktree" })}
          />
          <span>
            <WorktreeText />
          </span>
        </label>
        {form.mode !== "in_place" && (
          <div className="tpl-mode-sub">
            <ProvisionCheck
              id="tpl-provision"
              checked={form.mode === "provisioned"}
              onChange={(on) => p.onForm({ mode: on ? "provisioned" : "worktree" })}
            />
            {form.mode === "provisioned" && (
              <StrategyPicker
                id="tpl-strategy"
                value={form.workspace_strategy}
                onChange={(v) => p.onForm({ workspace_strategy: v })}
              />
            )}
          </div>
        )}
        <label className="check">
          <input
            type="radio"
            name="tpl-mode"
            id="tpl-in-place"
            checked={form.mode === "in_place"}
            // Fast-track commits for its own session, so it never drives a
            // shared checkout — the same rule as the New form.
            onChange={() => p.onForm({ mode: "in_place", fasttrack: "leave", ask_first: false })}
          />
          <span>
            This folder directly{" "}
            <span className="muted">
              {form.fasttrack !== "leave"
                ? "— turns fast-track off (it commits for this session, so it needs its own worktree)"
                : IN_PLACE_HINT}
            </span>
          </span>
        </label>
        <InitRepoCheck id="tpl-init" checked={form.init_repo} onChange={(on) => p.onForm({ init_repo: on })} />
      </div>
      <div className="tpl-schedule" role="group" aria-label="Schedule">
        <label className="check">
          <input
            type="checkbox"
            id="tpl-scheduled"
            checked={form.scheduled}
            onChange={(e) => p.onForm({ scheduled: e.target.checked })}
          />
          Run on a schedule <span className="muted">— starts a new session from it, by itself</span>
        </label>
        {form.scheduled && (
          <div className="tpl-schedule-when">
            <select
              id="tpl-days"
              aria-label="Which days"
              value={form.days}
              onChange={(e) => p.onForm({ days: e.target.value === "weekdays" ? "weekdays" : "daily" })}
            >
              <option value="daily">Every day</option>
              <option value="weekdays">Weekdays</option>
            </select>
            <span>at</span>
            <input
              id="tpl-at"
              type="time"
              aria-label="Time"
              autoFocus={ui.editing !== null && p.scheduleFocus === true}
              value={form.at}
              onChange={(e) => p.onForm({ at: e.target.value })}
            />
            {p.tz && <span className="muted">{p.tz} — this device's time</span>}
            {p.runs && !p.runs.runs_here && p.runs.runs_on && (
              <span className="muted">Starts on {p.runs.runs_on} — the device that runs automation.</span>
            )}
          </div>
        )}
      </div>
      <div className="tpl-ft" role="group" aria-label="Fast-track">
        {/* The New form's Fast-track control, not a dropdown of its own. */}
        <span className="tpl-ft-label">
          Fast-track to <span className="muted">— how far a session from it ships itself</span>
        </span>
        <FastTrackChoice
          id="tpl-fasttrack"
          label="Fast-track to"
          value={form.fasttrack}
          // A lane needs its own worktree (it commits for this session).
          onChange={(l) => p.onForm(l !== "leave" && form.mode === "in_place" ? { fasttrack: l, mode: "worktree" } : { fasttrack: l })}
          askFirst={form.ask_first}
          onAskFirst={(on) => p.onForm({ ask_first: on })}
        />
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
    <Modal
      id="templates-dialog"
      onClose={p.onClose}
      // Escape withdraws an open editor or delete question first.
      onEscape={() => {
        if (ui.editing === null && !ui.confirming) return false;
        p.dispatch({ t: "cancel" });
        return true;
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
          A template is a saved session setup — folder, agent and task — that starts a{" "}
          <strong>new</strong> session: now, or on a schedule. To send text to a session that's
          already running, use Prompts.
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
            <p className="tpl-empty muted">No templates yet — add one above, or use “Save as template…” in the New dialog.</p>
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
                    <div className="tpl-meta" title={t.repo_path || undefined}>
                      {templateMeta(t)}
                    </div>
                    {(() => {
                      const info = runs.runs[t.name.trim().toLowerCase()] || {};
                      const line = runLine(t, info, runs);
                      return (
                        <>
                          {line && <div className="tpl-runs">{line}</div>}
                          {info.last_error && <div className="tpl-run-error error">Last run failed: {info.last_error}</div>}
                        </>
                      );
                    })()}
                    {t.prompt && <div className="tpl-prompt">{t.prompt}</div>}
                    {runProblem(t) && (
                      /* Said on the card, not only in a hover title: a
                         disabled button that won't say why is furniture. */
                      <div className="tpl-needs">
                        Run now and schedules need a folder.{" "}
                        <button
                          type="button"
                          className="linklike"
                          onClick={() => p.dispatch({ t: "edit", name: t.name })}
                        >
                          Add one
                        </button>
                      </div>
                    )}
                  </div>
                  <div className="tpl-actions">
                    <button
                      type="button"
                      className="tpl-btn tpl-run"
                      disabled={!!runProblem(t) || !!p.running}
                      title={runProblem(t) ? "Run now needs a folder" : "Run now — starts a new session from this template"}
                      onClick={() => p.onRun?.(t.name)}
                    >
                      {p.running === t.name ? "Starting…" : "▶ Run now"}
                    </button>
                    <button
                      type="button"
                      className="tpl-btn tpl-sched"
                      title={t.schedule?.at ? "Change or stop its schedule" : "Start it by itself every day or weekday"}
                      onClick={() => p.dispatch({ t: "schedule", name: t.name })}
                    >
                      {t.schedule?.at ? "Schedule" : "Schedule…"}
                    </button>
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
    </Modal>
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
  const [runs, setRuns] = useState<TemplateRunsStatus>(() => templateRunsNow());
  const [running, setRunning] = useState("");
  const [agents, setAgents] = useState<string[]>([]);
  const [scheduleFocus, setScheduleFocus] = useState(false);
  const editingRef = useRef<string | null>(null);

  // The list from the server on every open, and again whenever anything
  // (this dialog, the New dialog's "Save prompt", a sync pull) changes it.
  useEffect(() => {
    if (!open) return;
    dispatch({ t: "cancel" });
    // The palette's "Schedule a session…" opens straight onto a NEW template's
    // editor with its schedule already switched on; a template chip's ⏰ in
    // New opens that template's.
    const want = readTemplatesTarget(useUi.getState().dialogTarget).schedule;
    if (want !== null) {
      setScheduleFocus(true);
      dispatch(want ? { t: "schedule", name: want } : { t: "new" });
    }
    setBusy(false);
    setLoadError("");
    let live = true;
    const reload = () => {
      setList(templatesNow());
      setRuns(templateRunsNow());
    };
    document.addEventListener(TEMPLATES_CHANGED, reload);
    // The editor's Agent picker: this device's agents.
    api<{ providers?: Array<{ name: string }> }>("/api/providers/manage")
      .then((d) => {
        if (live) setAgents((d.providers || []).map((x) => x.name));
      })
      .catch(() => {});
    ensureTemplates(true)
      .then((list) => {
        if (!live) return;
        setList(list);
        setRuns(templateRunsNow());
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
    if (ui.editing === null) {
      editingRef.current = null;
      return;
    }
    const t = ui.editing ? templates.find((x) => x.name === ui.editing) || null : null;
    // Opened for a template the list hasn't brought yet (New's ⏰ on a cold
    // list): seed once it lands, never a blank form under its name.
    if (ui.editing && !t) return;
    editingRef.current = ui.editing;
    const f = formFromTemplate(t);
    // Schedule… on a card: the same editor, with the schedule already on.
    if (scheduleFocus) f.scheduled = true;
    setForm(f);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ui.editing, templates]);

  if (!open) return null;

  const close = () => {
    closeDialog();
    // Came from the New dialog: hand the person back to it AS THEY LEFT IT
    // (newSessionResume), with the list they just edited reloaded.
    if (readTemplatesTarget(target).fromNew) {
      newSessionResume.keep = true;
      useUi.getState().openDialogFor("new-session");
    }
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
      // The next run moves with a new schedule.
      void ensureTemplates(true).catch(() => {});
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

  const run = async (name: string) => {
    setRunning(name);
    try {
      // Focusing the new session is the point; the dialog steps aside.
      if (await runTemplateNow(name)) closeDialog();
    } finally {
      setRunning("");
    }
  };

  return (
    <TemplatesDialogView
      templates={templates}
      runs={runs}
      running={running}
      onRun={(name) => void run(name)}
      ui={ui}
      form={form}
      busy={busy}
      loaded={loaded}
      loadError={loadError}
      onForm={(patch) => setForm((f) => ({ ...f, ...patch }))}
      dispatch={(a) => {
        setScheduleFocus(a.t === "schedule");
        dispatch(a);
      }}
      agents={agents}
      scheduleFocus={scheduleFocus}
      tz={Object.values(runs.runs || {}).map((r) => (r as RunInfoX).tz).find(Boolean) || localZone()}
      onSave={() => void save()}
      onDelete={(name) => void del(name)}
      onClose={close}
    />
  );
}
