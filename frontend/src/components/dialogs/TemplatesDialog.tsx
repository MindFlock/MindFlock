/** Schedule: the top-bar surface for what starts by itself — and the ONE
 * list of saved New-session setups (server-side, `/api/templates`, synced
 * between devices) it starts from. It was the Templates dialog, reached only
 * from the palette and New's strip; it is the same component, store key
 * "schedule" ("templates" still opens it — state/store.ts).
 *
 * Top to bottom: what runs next ("Next: nightly in 2h — today 21:00"), the
 * SCHEDULED templates (cadence, next run, last run, Recent runs), then the
 * unscheduled ones ("Saved setups — run now or add a schedule"). Every card
 * has **Run now** (a fresh session, its prompt already sent —
 * lib/templateRun), **Edit** (inline), **Schedule…** / **Change schedule**
 * (every day or weekdays at a local time, or a cron expression whose meaning
 * and next runs the server previews as you type; the server's
 * template_schedule loop starts it), **Delete** behind an inline confirm row,
 * and a scheduled one **Skip next** / **Pause** (schedule.skip_until /
 * schedule.paused). An optional **autopilot** depth arms each run.
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
  cronPreview,
  deleteTemplate,
  hasSchedule,
  ensureTemplates,
  inLabel,
  nextUp,
  withScheduleFlags,
  readTemplatesTarget,
  runProblem,
  saveTemplate,
  scheduleLabel,
  templateRunsNow,
  templatesNow,
  whenLabel,
  type CronPreview,
  type Template,
  type TemplateAutopilot,
  type TemplateRunEntry,
  type TemplateRunInfo,
  type TemplateRunsStatus,
} from "../../lib/templates";
import { runTemplateNow } from "../../lib/templateRun";
import { api } from "../../api/client";
import { useConfig } from "../../state/queries";
import { leafName, newSessionResume, type RepoSuggestion } from "./newSession";
import { TemplateEditor, formFromTemplate, templateBody, type TemplateForm } from "./TemplateEditor";
import { TourButton } from "../onboarding/TourButton";
import { useAutoTour } from "../onboarding/useAutoTour";

// The editor's form and its helpers live with the editor; the dialog's tests
// (and anything else) keep importing them from here.
export { cronPreviewLine, formFromTemplate, templateBody, type TemplateForm } from "./TemplateEditor";

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

/** A rename is a save under the new name AND a delete of the old one — the
 * server upserts by name, so without the delete both would remain. Same
 * name ignoring case is the server's own overwrite: nothing to delete. */
export function renameLeaves(oldName: string, newName: string): string | null {
  const a = oldName.trim().toLowerCase();
  const b = newName.trim().toLowerCase();
  return a && a !== b ? oldName : null;
}

/** The card's words for what a run does once its turn ends. */
const AUTOPILOT_THEN: Record<TemplateAutopilot, string> = {
  commit: "commit",
  push: "commit + push",
  pr: "open a PR",
};

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
  if (t.autopilot) bits.push("then: " + AUTOPILOT_THEN[t.autopilot]);
  return bits.join(" · ");
}

/** One history row: "today 21:00 · scheduled → nightly-oct14" (or why it
 * failed / was skipped). */
export function historyLine(h: TemplateRunEntry, now: Date = new Date()): string {
  const bits = [whenLabel(h.at, now), h.trigger === "schedule" ? "scheduled" : "by hand"];
  let line = bits.join(" · ");
  if (h.skipped) line += " — skipped: " + h.skipped;
  else if (h.error) line += " — failed: " + h.error;
  else if (h.session) line += " → " + h.session + (h.autopilot ? " (autopilot: " + h.autopilot + ")" : "");
  return line;
}

/** A scheduled card's cadence, in this device's zone: "Weekdays at 21:00
 * EDT", "Every 30 minutes". */
export function cadenceLine(t: Template, info: TemplateRunInfo): string {
  const tz = (info as RunInfoX).tz;
  return (info.schedule_label || scheduleLabel(t.schedule)) + (tz && t.schedule?.at ? " " + tz : "");
}

/** A scheduled card's "when next": "Next today 21:00 · in 9h", "Paused",
 * "Starts on mac-mini" (another device fires schedules), or "". */
export function nextLine(
  t: Template,
  info: TemplateRunInfo,
  status: Pick<TemplateRunsStatus, "runs_here" | "runs_on">,
  now: Date = new Date()
): string {
  if (t.schedule?.paused || info.paused) return "Paused";
  if (!status.runs_here && status.runs_on) return "Starts on " + status.runs_on;
  if (!info.next_at) return "";
  return "Next " + whenLabel(info.next_at, now) + " · " + inLabel(info.next_at, now);
}

/** The card's last-run line when the last run STARTED fine: "Last run today
 * 09:00 · scheduled → nightly-oct14" ("" when it never ran). */
export function lastLine(info: TemplateRunInfo, now: Date = new Date()): string {
  if (!info.last_at) return "";
  const how = info.last_trigger === "schedule" ? "scheduled" : info.last_trigger === "manual" ? "by hand" : "";
  return (
    "Last run " +
    whenLabel(info.last_at, now) +
    (how ? " · " + how : "") +
    (info.last_session ? " → " + info.last_session : "")
  );
}

/** The scheduled templates in the order the surface lists them: soonest
 * next run first, then any with no next run, then the paused ones. */
export function scheduledOrder(templates: Template[], status: Pick<TemplateRunsStatus, "runs">): Template[] {
  const rank = (t: Template) => {
    if (t.schedule?.paused) return [2, 0];
    const at = Number(status.runs[t.name.trim().toLowerCase()]?.next_at) || 0;
    return at ? [0, at] : [1, 0];
  };
  return templates
    .filter(hasSchedule)
    .map((t, i) => ({ t, i, r: rank(t) }))
    .sort((a, b) => a.r[0] - b.r[0] || a.r[1] - b.r[1] || a.i - b.i)
    .map((x) => x.t);
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
  /** The server's reading of the cron field being typed. */
  cronPreview?: CronPreview | null;
  /** A card's Pause / Resume (true = pause). */
  onPause?: (name: string, paused: boolean) => void;
  /** A card's Skip next (the slot's epoch) / Undo skip (0). */
  onSkip?: (name: string, until: number) => void;
  /** The template whose Pause / Skip is being saved. */
  flagging?: string;
  /** The editor's folder chips (GET /api/repos/suggest) and $HOME. */
  folderSuggestions?: RepoSuggestion[];
  homePath?: string;
  /** Injectable clock for the "in 2h" words (tests). */
  now?: Date;
}

/** Markup only: everything it shows comes in as props. */
export function TemplatesDialogView(p: ViewProps) {
  const { templates, ui, form, busy, loaded } = p;
  const runs = p.runs || { runs: {}, runs_here: true, runs_on: "" };
  const loadError = p.loadError || "";
  // The editor's folder browser takes Escape first (it puts the folder back).
  const escapeRef = useRef<(() => boolean) | null>(null);
  const editor = (
    <TemplateEditor
      form={form}
      onForm={p.onForm}
      error={ui.error}
      busy={busy}
      onSave={p.onSave}
      onCancel={() => p.dispatch({ t: "cancel" })}
      agents={p.agents}
      scheduleFocus={ui.editing !== null && p.scheduleFocus === true}
      tz={p.tz}
      cronPreview={p.cronPreview}
      runs={p.runs}
      folderSuggestions={p.folderSuggestions}
      homePath={p.homePath}
      escapeRef={escapeRef}
    />
  );

  const now = p.now || new Date();
  const infoFor = (t: Template): TemplateRunInfo => runs.runs[t.name.trim().toLowerCase()] || {};

  /** Run now / Edit / Delete — every card's own verbs; the schedule's live on
   * its strip. An unscheduled card adds Schedule…, the way to give it one. */
  const actions = (t: Template) => (
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
      {!hasSchedule(t) && (
        <button
          type="button"
          className="tpl-btn tpl-sched"
          title="Start it by itself — every day, weekdays, or on a cron expression"
          onClick={() => p.dispatch({ t: "schedule", name: t.name })}
        >
          Schedule…
        </button>
      )}
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
  );

  /** The schedule strip of a scheduled card: cadence, when next, and its
   * three switches — Change schedule, Skip next / Undo skip, Pause / Resume. */
  const strip = (t: Template, info: TemplateRunInfo) => {
    const paused = !!(t.schedule?.paused || info.paused);
    const next = nextLine(t, info, runs, now);
    const skipping = !paused && info.skipping_at ? info.skipping_at : 0;
    const flagging = p.flagging === t.name;
    return (
      <div className={"sch-strip" + (paused ? " is-paused" : "")}>
        <div className="sch-when">
          <span className="sch-cadence">{cadenceLine(t, info)}</span>
          {next && <span className={"sch-next" + (paused ? " sch-paused-chip" : "")}>{next}</span>}
        </div>
        <div className="sch-switches">
          <button
            type="button"
            className="sch-sw tpl-sched"
            title="Change when it runs, or stop its schedule"
            onClick={() => p.dispatch({ t: "schedule", name: t.name })}
          >
            Change schedule
          </button>
          {!paused &&
            (skipping ? (
              <button
                type="button"
                className="sch-sw sch-skip"
                disabled={flagging}
                onClick={() => p.onSkip?.(t.name, 0)}
              >
                Undo skip
              </button>
            ) : (
              info.next_at && (
                <button
                  type="button"
                  className="sch-sw sch-skip"
                  disabled={flagging}
                  title={"Let " + whenLabel(info.next_at, now) + " pass without a session — the one after runs as usual"}
                  onClick={() => p.onSkip?.(t.name, Number(info.next_at))}
                >
                  Skip next
                </button>
              )
            ))}
          <button
            type="button"
            className="sch-sw sch-pause"
            disabled={flagging}
            title={paused ? "Start running on its schedule again" : "Keep the schedule, start nothing until you resume"}
            onClick={() => p.onPause?.(t.name, !paused)}
          >
            {paused ? "Resume" : "Pause"}
          </button>
        </div>
      </div>
    );
  };

  /** What happened last, and the fold with the rest. */
  const outcome = (t: Template, info: TemplateRunInfo) => {
    const skipping = !(t.schedule?.paused || info.paused) && info.skipping_at ? info.skipping_at : 0;
    const last = lastLine(info, now);
    return (
      <>
        {skipping > 0 && (
          <div className="sch-skipping muted">Skipping {whenLabel(skipping, now)} — you asked to skip it</div>
        )}
        {info.last_error ? (
          <div className="tpl-run-error error">Last run failed: {info.last_error}</div>
        ) : info.last_skip ? (
          <div className="tpl-run-skip muted">
            Skipped {whenLabel(info.last_skip_at, now)}: {info.last_skip}
          </div>
        ) : (
          last && <div className="tpl-runs">{last}</div>
        )}
        {info.history && info.history.length > 0 && (
          <details className="tpl-history">
            <summary>Recent runs ({info.history.length})</summary>
            <ul>
              {info.history.map((h, i) => (
                <li key={i} className={h.error ? "error" : h.skipped ? "muted" : ""}>
                  {historyLine(h, now)}
                </li>
              ))}
            </ul>
          </details>
        )}
      </>
    );
  };

  const card = (t: Template) => {
    if (ui.editing === t.name)
      return (
        <div key={t.name} className="tpl-card tpl-card-editing" data-name={t.name}>
          {editor}
        </div>
      );
    const info = infoFor(t);
    const scheduled = hasSchedule(t);
    const paused = scheduled && !!(t.schedule?.paused || info.paused);
    return (
      <div
        key={t.name}
        className={
          "tpl-card" +
          (scheduled ? " is-scheduled" : "") +
          (paused ? " is-paused" : "") +
          (scheduled && !paused && info.last_error ? " is-failing" : "")
        }
        data-name={t.name}
      >
        <div className="tpl-card-top">
          <div className="tpl-card-text">
            <div className="tpl-name">{t.name}</div>
            <div className="tpl-meta" title={t.repo_path || undefined}>
              {templateMeta(t)}
            </div>
          </div>
          {actions(t)}
        </div>
        {scheduled && strip(t, info)}
        {outcome(t, info)}
        {t.prompt && <div className="tpl-prompt">{t.prompt}</div>}
        {runProblem(t) && (
          /* Said on the card, not only in a hover title: a disabled button
             that won't say why is furniture. */
          <div className="tpl-needs">
            Run now and schedules need a folder.{" "}
            <button type="button" className="linklike" onClick={() => p.dispatch({ t: "edit", name: t.name })}>
              Add one
            </button>
          </div>
        )}
        {ui.confirming === t.name && (
          <div className="tpl-confirm" role="group" aria-live="polite">
            <span>Delete “{t.name}”? It goes from every device.</span>
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
    );
  };

  const scheduledList = scheduledOrder(templates, runs);
  const saved = templates.filter((t) => !hasSchedule(t));
  const up = nextUp(templates, runs);
  const elsewhere = !runs.runs_here && !!runs.runs_on;
  const empty = loaded && templates.length === 0 && ui.editing !== "";

  return (
    <Modal
      id="templates-dialog"
      // The top-bar dialogs' shared fixed top edge (Modal.css): opening the
      // editor grows the card down instead of re-centring it.
      className="top-dialog"
      onClose={p.onClose}
      // Escape withdraws an open editor or delete question first.
      onEscape={() => {
        if (ui.editing !== null && escapeRef.current?.()) return true;
        if (ui.editing === null && !ui.confirming) return false;
        p.dispatch({ t: "cancel" });
        return true;
      }}
    >
      <div id="templates-card" role="dialog" aria-modal="true" aria-labelledby="templates-title">
        <div className="tpl-head">
          <h2 id="templates-title">Schedule</h2>
          <div className="tpl-head-acts">
            {ui.editing !== "" && !empty && (
              <button type="button" id="templates-new" className="tpl-new" onClick={() => p.dispatch({ t: "new" })}>
                + New template
              </button>
            )}
            <TourButton tour="schedule" />
            <button type="button" id="templates-close" onClick={p.onClose}>
              Close
            </button>
          </div>
        </div>
        {!empty && (
          <p className="tpl-intro muted">
            Templates start a <strong>new</strong> session — when you run one, or by themselves on a schedule.
            <span className="tpl-intro-aside">To send text to a session that's already running, use Prompts.</span>
          </p>
        )}
        <div className="tpl-list" id="templates-list">
          {ui.editing === "" && <div className="tpl-card tpl-card-editing tpl-card-new">{editor}</div>}
          {loadError && (
            <p className="error tpl-load-error" role="alert">
              Could not load templates: {loadError}
            </p>
          )}
          {!loaded && !loadError && templates.length === 0 && <p className="tpl-empty muted">Loading templates…</p>}
          {empty && (
            <div className="sch-empty" id="schedule-empty">
              <div className="sch-empty-mark" aria-hidden="true">
                <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                  <circle cx="12" cy="13" r="8" />
                  <path d="M12 9v4l2.5 2.5M9 2.5h6" />
                </svg>
              </div>
              <p>Save a session setup once, then start it with one click — or every night by itself.</p>
              <button
                type="button"
                id="schedule-empty-create"
                className="tpl-btn tpl-run"
                onClick={() => p.dispatch({ t: "new" })}
              >
                Create a template
              </button>
            </div>
          )}
          {templates.length > 0 && (
            <>
              {(up || elsewhere) && (
                <div className="sch-next-up" id="schedule-next">
                  {elsewhere ? (
                    <>
                      Schedules start on <strong>{runs.runs_on}</strong> — the device that runs automation.
                    </>
                  ) : (
                    up && (
                      <>
                        <span className="sch-next-label">Next</span>
                        <strong>{up.name}</strong>
                        <span>{inLabel(up.at, now)}</span>
                        <span className="muted">— {whenLabel(up.at, now)}</span>
                      </>
                    )
                  )}
                </div>
              )}
              <section className="sch-section" id="schedule-scheduled" aria-labelledby="schedule-scheduled-h">
                <h3 id="schedule-scheduled-h">
                  Scheduled{scheduledList.length > 0 && <span className="sch-count">{scheduledList.length}</span>}
                </h3>
                {scheduledList.length ? (
                  scheduledList.map(card)
                ) : (
                  <p className="sch-none muted">
                    Nothing runs by itself yet — <strong>Schedule…</strong> on a setup below starts it every day,
                    weekday or on any cron.
                  </p>
                )}
              </section>
              {saved.length > 0 && (
                <section className="sch-section" id="schedule-saved" aria-labelledby="schedule-saved-h">
                  <h3 id="schedule-saved-h">
                    Saved setups<span className="sch-count">{saved.length}</span>
                    <span className="sch-section-hint">run now or add a schedule</span>
                  </h3>
                  {saved.map(card)}
                </section>
              )}
            </>
          )}
        </div>
      </div>
    </Modal>
  );
}

export function TemplatesDialog() {
  const open = useUi((s) => s.openDialog === "schedule");
  useAutoTour("schedule", open);
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
  const [flagging, setFlagging] = useState("");
  const [agents, setAgents] = useState<string[]>([]);
  const [scheduleFocus, setScheduleFocus] = useState(false);
  const [preview, setPreview] = useState<CronPreview | null>(null);
  const editingRef = useRef<string | null>(null);
  const [folderSuggestions, setFolderSuggestions] = useState<RepoSuggestion[]>([]);
  const suggestAsked = useRef(false);
  const { data: config } = useConfig();

  // The editor's folder chips — the New form's Recent / Home line. Asked
  // once per opening, and only when an editor opens: the endpoint walks the
  // disk, which a glance at the list should not pay for.
  const editorOpen = open && ui.editing !== null;
  useEffect(() => {
    if (!open) suggestAsked.current = false;
    if (!editorOpen || suggestAsked.current) return;
    suggestAsked.current = true;
    api<{ suggestions?: RepoSuggestion[] }>("/api/repos/suggest")
      .then((d) => setFolderSuggestions(d.suggestions || []))
      // Chips are sugar: the field and Browse… work without them.
      .catch(() => setFolderSuggestions([]));
  }, [open, editorOpen]);

  // The cron field's meaning, asked of the server as it is typed (debounced;
  // a stale answer never overwrites a newer one).
  const wantsPreview = open && ui.editing !== null && form.scheduled && form.days === "cron";
  const cronText = form.cron.trim();
  useEffect(() => {
    if (!wantsPreview || !cronText) {
      setPreview(null);
      return;
    }
    let live = true;
    const timer = setTimeout(() => {
      cronPreview(cronText)
        .then((pv) => {
          if (live) setPreview(pv);
        })
        .catch(() => {
          if (live) setPreview(null);
        });
    }, 300);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [wantsPreview, cronText]);

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

  // Pause / Resume / Skip next / Undo skip: the whole template POSTed with
  // its schedule's switch flipped, then the run rows re-read (the next run
  // moves).
  const flag = async (name: string, patch: { paused?: boolean; skip_until?: number }, said: string) => {
    const t = templates.find((x) => x.name === name);
    if (!t) return;
    setFlagging(name);
    try {
      await saveTemplate(withScheduleFlags(t, patch));
      setList(templatesNow());
      await ensureTemplates(true).catch(() => {});
      setRuns(templateRunsNow());
      toast(said);
    } catch (err) {
      toast("Couldn't change the schedule: " + errMsg(err));
    } finally {
      setFlagging("");
    }
  };

  return (
    <TemplatesDialogView
      templates={templates}
      flagging={flagging}
      onPause={(name, paused) =>
        void flag(name, { paused }, paused ? `Paused “${name}” — it starts nothing until you resume it` : `Resumed “${name}”`)
      }
      onSkip={(name, until) =>
        void flag(
          name,
          { skip_until: until },
          until ? `“${name}” will skip ${whenLabel(until)} — the run after it goes ahead` : `“${name}” runs next time as usual`
        )
      }
      runs={runs}
      running={running}
      cronPreview={preview}
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
      folderSuggestions={folderSuggestions}
      homePath={config?.home || ""}
      scheduleFocus={scheduleFocus}
      tz={Object.values(runs.runs || {}).map((r) => (r as RunInfoX).tz).find(Boolean) || localZone()}
      onSave={() => void save()}
      onDelete={(name) => void del(name)}
      onClose={close}
    />
  );
}
