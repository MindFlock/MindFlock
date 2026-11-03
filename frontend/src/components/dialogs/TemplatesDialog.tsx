/** Schedule: the top-bar surface for what starts by itself — and the ONE
 * list of saved New-session setups (server-side, `/api/templates`, synced
 * between devices) it starts from. It was the Templates dialog, reached only
 * from the palette and New's strip; it is the same component, store key
 * "schedule" ("templates" still opens it — state/store.ts).
 *
 * Top to bottom: what runs next ("Next: nightly in 2h — today 21:00"), the
 * SCHEDULED templates (THE one schedule line — lib/templates'
 * templateScheduleLine, the same words New's chip and Intake show — last
 * run, Recent runs), then the unscheduled ones ("Not scheduled"). Every card
 * has **Run now** (a fresh session, its task already sent — lib/templateRun;
 * a refusal is the server's own sentence, never "Started…"), **Edit** (the
 * whole form, inline), **Schedule…** / **Change schedule** (a compact
 * schedule-only row in the card: days, a 24-hour time or a cron expression,
 * and the next runs the server previews as you type; the server's
 * template_schedule loop starts it), **Delete** behind an inline confirm row,
 * and a scheduled one **Skip next** / **Pause** (schedule.skip_until /
 * schedule.paused). An optional **Fast-track** level arms each run.
 *
 * Saved stops whose trigger is a schedule are cards in the SAME list, with
 * the same verbs: **Run now** (a flight takes off on it in a new session; one
 * with no repository asks for a folder in the card first, never a dead-end
 * error), **Schedule…** / **Change schedule** (the same compact row),
 * **Skip next** / **Pause** (trigger.schedule.skip_until / trigger.paused),
 * and **Edit** — its Takes off dialog (when, repository, agent, model).
 *
 * Every confirm is a row in the card, never a browser dialog: Electron has
 * no window.confirm/prompt/alert, and a Delete that silently does nothing is
 * the bug this dialog exists to not have again.
 *
 * The state machine is a pure reducer (templatesReducer) and the markup a
 * props-only view (TemplatesDialogView), so both are tested without a DOM. */

import { useEffect, useReducer, useRef, useState, type ReactNode } from "react";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
import { errMsg } from "../../lib/format";
import { Modal } from "../Modal";
import {
  TEMPLATES_CHANGED,
  cadenceText,
  cronOf,
  cronPreview,
  onceOf,
  deleteTemplate,
  fastTrackNeedsGitHub,
  hasSchedule,
  ensureTemplates,
  inLabel,
  lastRunFailed,
  nextUp,
  withScheduleFlags,
  readTemplatesTarget,
  runProblem,
  saveTemplate,
  templateRefusal,
  templateRunsNow,
  templateScheduleLine,
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
import { selectSession } from "../../lib/sessionActions";
import { LANE_LABEL } from "../../lib/autopilot";
import { api } from "../../api/client";
import type { FleetStatus } from "../../api/types";
import { useConfig, useFlightPlans, useGithubConnected, useInstances } from "../../state/queries";
import { scheduleOfWhen, whenFormOf, type FlightPlan, type FlightSchedule, type WhenForm } from "../../lib/flights";
import { savePlan, takeOff } from "../../lib/flightsApi";
import { Bird } from "../flights/Bird";
import { TakeoffDialog } from "../flights/TakeoffDialog";
import { PathField } from "../PathField";
import { leafName, newSessionResume, type InstalledMap, type RepoSuggestion } from "./newSession";
import {
  ScheduleEditor,
  TemplateEditor,
  defaultFolder,
  formFromTemplate,
  templateBody,
  type TemplateForm,
  type WhenFields,
} from "./TemplateEditor";
import { TourButton } from "../onboarding/TourButton";
import { useAutoTour } from "../onboarding/useAutoTour";
import { embeddedGuard } from "../flights/embedGuard";

// The editor's form and its helpers live with the editor; the dialog's tests
// (and anything else) keep importing them from here.
export { cronPreviewLine, formFromTemplate, templateBody, type TemplateForm } from "./TemplateEditor";

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
 * form, null = none), whether that is the whole form or just its schedule
 * (a card's Schedule… / Change schedule), and which card is asking
 * "delete?". One at a time — opening an editor closes the question and the
 * other way round, so the card never shows two things the next click could
 * mean. */
export interface TemplatesUiState {
  editing: string | null;
  /** The editor is the compact schedule-only row, not the whole form. */
  scheduleOnly?: boolean;
  /** "Add one" opened the form: Folder takes the caret and a highlight. */
  focus?: "folder" | "";
  confirming: string | null;
  error: string;
  /** Why the server refused ("agent_missing" / "github_required" / …). */
  reason?: string;
  /** The open editor has unsaved changes and something asked to leave it:
   * the "Discard changes?" row is up, and `then` is what Discard goes on to
   * do ("close" = close the dialog). */
  discarding?: { then: TemplatesUiAction | "close" };
}

export type TemplatesUiAction =
  | { t: "edit"; name: string; focus?: "folder" }
  | { t: "schedule"; name: string }
  | { t: "new" }
  | { t: "ask-delete"; name: string }
  | { t: "cancel" }
  /** Escape on an open editor: Cancel, but asked first when it has changes. */
  | { t: "dismiss" }
  | { t: "ask-discard"; then: TemplatesUiAction | "close" }
  | { t: "keep-editing" }
  | { t: "saved" }
  | { t: "deleted" }
  | { t: "error"; message: string; reason?: string };

export const TEMPLATES_UI_INIT: TemplatesUiState = { editing: null, confirming: null, error: "" };

export function templatesReducer(s: TemplatesUiState, a: TemplatesUiAction): TemplatesUiState {
  switch (a.t) {
    case "edit":
      return { editing: a.name, confirming: null, error: "", focus: a.focus };
    case "schedule":
      // A NEW template's schedule (the palette's "New scheduled template…")
      // is the whole form: it has no name or folder yet.
      return { editing: a.name, confirming: null, error: "", scheduleOnly: a.name ? true : undefined };
    case "new":
      return { editing: "", confirming: null, error: "" };
    case "ask-delete":
      return { editing: null, confirming: a.name, error: "" };
    case "cancel":
    case "dismiss":
    case "saved":
    case "deleted":
      return { editing: null, confirming: null, error: "" };
    case "ask-discard":
      return { ...s, discarding: { then: a.then } };
    case "keep-editing":
      return { ...s, discarding: undefined };
    case "error":
      return { ...s, error: a.message, reason: a.reason };
  }
}

/** Whether the form differs from what its editor opened with. */
export function formDirty(form: TemplateForm, seed: TemplateForm | null): boolean {
  return !!seed && JSON.stringify(form) !== JSON.stringify(seed);
}

/** Whether `a` would throw away the open editor's changes — another card's
 * Edit / Schedule… / Delete, + New, Escape — and so asks "Discard changes?"
 * first. Cancel is itself the answer, and needs no question. */
export function needsDiscard(ui: TemplatesUiState, a: TemplatesUiAction, dirty: boolean): boolean {
  if (ui.editing === null || !dirty) return false;
  // Schedule-only row → its whole form: the same template, the edits go along.
  if (a.t === "edit" && a.name === ui.editing) return false;
  return a.t === "edit" || a.t === "schedule" || a.t === "new" || a.t === "ask-delete" || a.t === "dismiss";
}

/** A rename is a save under the new name AND a delete of the old one — the
 * server upserts by name, so without the delete both would remain. Same
 * name ignoring case is the server's own overwrite: nothing to delete. */
export function renameLeaves(oldName: string, newName: string): string | null {
  const a = oldName.trim().toLowerCase();
  const b = newName.trim().toLowerCase();
  return a && a !== b ? oldName : null;
}

/** The card's words for a run's Fast-track: the ladder's own labels. */
const AUTOPILOT_THEN: Record<TemplateAutopilot, string> = {
  commit: LANE_LABEL.commit,
  push: LANE_LABEL.push,
  pr: LANE_LABEL.pr,
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
  if (t.autopilot) bits.push("Fast-track: " + (AUTOPILOT_THEN[t.autopilot] || t.autopilot));
  return bits.join(" · ");
}

/** Whether a history row is a failure (round-5 rows say so; older ones only
 * carry the error). */
function failedRow(h: TemplateRunEntry): boolean {
  return h.status ? h.status === "failed" : !!h.error && !h.skipped;
}

/** A Fast-track level as the ladder says it ("Open PR"). */
function levelWord(v: string): string {
  return AUTOPILOT_THEN[v as TemplateAutopilot] || v;
}

/** One history row: "today 21:00 · scheduled → nightly-oct14" (or why it
 * failed / was skipped). */
export function historyLine(h: TemplateRunEntry, now: Date = new Date()): string {
  const bits = [whenLabel(h.at, now), h.trigger === "schedule" ? "scheduled" : "by hand"];
  let line = bits.join(" · ");
  if (h.skipped) line += " — skipped: " + h.skipped;
  else if (failedRow(h)) line += " — failed: " + (h.error || "it couldn't start");
  else if (h.session) line += " → " + h.session + (h.autopilot ? " (Fast-track: " + levelWord(h.autopilot) + ")" : "");
  return line;
}

/** A scheduled card's cadence, with the zone of the computer that runs
 * schedules: "Weekdays at 21:00 EDT", "Every 30 minutes". */
export function cadenceLine(
  t: Template,
  info: TemplateRunInfo,
  status: Pick<TemplateRunsStatus, "runs_here" | "runs_on" | "tz"> = { runs_here: true, runs_on: "" }
): string {
  return cadenceText(t, info, status);
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
  // The schedule line already says when ("next Fri 07:30"): this is how far.
  return inLabel(info.next_at, now);
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

// --- Scheduled saved stops ------------------------------------------------------
// A saved stop whose trigger is a schedule starts itself like a template does
// (docs/flights.md "Scheduled plans"), so Schedule lists it IN THE SAME LIST
// as the templates — one list of what starts by itself on a timer — with a
// template card's anatomy (name, schedule line, next run, last result) and
// its verbs: Run now, Schedule… / Change schedule, Skip next, Pause / Resume,
// and Edit (its Takes off dialog).

/** A flyway's schedule record (`runs` on GET /api/flights/plans): a
 * template's run record, plus where schedules fire. */
export type FlywayRuns = TemplateRunInfo & { runs_here?: boolean; runs_on?: string };
export type ScheduledFlyway = FlightPlan & { runs?: FlywayRuns };

/** The flyways that start themselves, in the order the surface lists them:
 * soonest next run first, then any with no next run, then the paused ones. */
export function scheduledFlyways(plans: readonly ScheduledFlyway[] | null | undefined): ScheduledFlyway[] {
  const rank = (p: ScheduledFlyway): [number, number] => {
    if (p.trigger.paused || p.runs?.paused) return [2, 0];
    const at = Number(p.runs?.next_at) || 0;
    return at ? [0, at] : [1, 0];
  };
  return (plans || [])
    .filter((p) => p.trigger?.kind === "schedule")
    .map((p) => ({ p, r: rank(p) }))
    .sort((a, b) => a.r[0] - b.r[0] || a.r[1] - b.r[1])
    .map((x) => x.p);
}

/** A scheduled plan read as a template, so its card says its schedule in a
 * template card's exact words (templateScheduleLine, nextLine, Skip next):
 * a stand-in template named by the plan id, whose schedule is the trigger's
 * (its `paused` folded in), and a run status holding the plan's row. */
export function planAsTemplate(f: ScheduledFlyway): { t: Template; info: TemplateRunInfo; status: TemplateRunsStatus } {
  const r: FlywayRuns = f.runs || {};
  const sched = f.trigger.schedule ? ({ ...f.trigger.schedule } as NonNullable<Template["schedule"]>) : undefined;
  if (sched && f.trigger.paused) sched.paused = true;
  const info: TemplateRunInfo = { ...r };
  return {
    t: { name: f.id, repo_path: f.session?.repo_path || "", schedule: sched },
    info,
    status: { runs: { [f.id.trim().toLowerCase()]: info }, runs_here: r.runs_here !== false, runs_on: r.runs_on || "", tz: r.tz || "" },
  };
}

/** One card of Schedule's list: a template or a saved stop on a schedule. */
export type ScheduleEntry = { kind: "template"; t: Template } | { kind: "plan"; f: ScheduledFlyway };

/** Schedule's ONE list of what starts by itself on a timer: the scheduled
 * templates and the scheduled saved stops together — soonest next run
 * first, then any with no next run, then the paused ones; a tie keeps
 * templates first. A saved stop whose trigger is a schedule with no time
 * set yet is not on it (it is "Not scheduled"). */
export function scheduleEntries(
  templates: Template[],
  status: Pick<TemplateRunsStatus, "runs">,
  plans: readonly ScheduledFlyway[] | null | undefined
): ScheduleEntry[] {
  const rank = (paused: boolean, at: number): [number, number] => (paused ? [2, 0] : at ? [0, at] : [1, 0]);
  const rows: Array<{ e: ScheduleEntry; r: [number, number] }> = [
    ...scheduledOrder(templates, status).map((t) => ({
      e: { kind: "template" as const, t },
      r: rank(!!t.schedule?.paused, Number(status.runs[t.name.trim().toLowerCase()]?.next_at) || 0),
    })),
    ...scheduledFlyways(plans)
      .filter((f) => !!f.trigger.schedule)
      .map((f) => ({
        e: { kind: "plan" as const, f },
        r: rank(!!(f.trigger.paused || f.runs?.paused), Number(f.runs?.next_at) || 0),
      })),
  ];
  return rows
    .map((x, i) => ({ ...x, i }))
    .sort((a, b) => a.r[0] - b.r[0] || a.r[1] - b.r[1] || a.i - b.i)
    .map((x) => x.e);
}

/** What runs next across the whole list, templates and saved stops alike. */
export function nextAcross(
  templates: Template[],
  status: Pick<TemplateRunsStatus, "runs">,
  plans: readonly ScheduledFlyway[] | null | undefined
): { name: string; at: number } | null {
  let best = nextUp(templates, status);
  for (const f of scheduledFlyways(plans)) {
    if (!f.trigger.schedule || f.trigger.paused || f.runs?.paused || f.runs?.runs_here === false) continue;
    const at = Number(f.runs?.next_at) || 0;
    if (at && (!best || at < best.at)) best = { name: f.name, at };
  }
  return best;
}

/** A plan's schedule after Skip next (the slot's epoch) / Undo skip (0). */
export function planWithSkip(f: FlightPlan, until: number, nowSec: number = Date.now() / 1000): FlightPlan {
  const sched = { ...(f.trigger.schedule || {}) } as Record<string, unknown>;
  if (until && until > nowSec) sched.skip_until = until;
  else delete sched.skip_until;
  return { ...f, trigger: { ...f.trigger, schedule: sched as unknown as FlightSchedule } };
}

/** The plan with the compact row's schedule: its cadence; a Skip next held
 * only while the cadence is the same one it was set for. null = the fields
 * don't name a schedule yet. */
export function planWithWhen(f: FlightPlan, when: WhenFields): FlightPlan | null {
  const next = scheduleOfWhen({ ...when, seeded: when.seeded || "" } as WhenForm);
  if (!next) return null;
  const old = f.trigger.schedule as (FlightSchedule & { skip_until?: number }) | null;
  const same = !!old && JSON.stringify(scheduleOfWhen(whenFormOf(old))) === JSON.stringify(next);
  const sched = { ...next } as Record<string, unknown>;
  if (same && old?.skip_until) sched.skip_until = old.skip_until;
  return { ...f, trigger: { ...f.trigger, schedule: sched as unknown as FlightSchedule } };
}

/** The plan as savePlan sends it: the run record is the server's to add. */
export function barePlan(f: ScheduledFlyway): FlightPlan {
  const out = { ...f } as ScheduledFlyway;
  delete out.runs;
  return out;
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
  /** This device's agents (GET /api/providers/status), null = unknown. */
  installed?: InstalledMap | null;
  /** Whether GitHub is connected (null = unknown / not asked). */
  githubConnected?: boolean | null;
  /** The sessions that exist right now: a run's session is a link to it, or
   * "(closed)" once it's gone. undefined = unknown (no link, no "(closed)"). */
  liveSessions?: ReadonlySet<string>;
  /** A run's session link: close Schedule and focus the session. */
  onOpenSession?: (title: string) => void;
  /** This template list syncs to other linked devices (Delete says so). */
  otherDevices?: boolean;
  /** A card's "Stop schedule" (the compact row): save it unscheduled. */
  onStopSchedule?: () => void;
  /** The open editor has unsaved changes: a backdrop press doesn't close. */
  dirty?: boolean;
  /** "Discard changes?" → Discard: drop the edits and do what was asked. */
  onDiscard?: () => void;
  /** Drawn inside another surface: no Modal of its own and no Close. */
  embedded?: boolean;
  /** Every flyway (GET /api/flights/plans); the scheduled ones are cards in
   * the list beside the templates. */
  flyways?: readonly ScheduledFlyway[] | null;
  /** A plan card's Run now: a flight takes off on it in a new session (one
   * with no repository opens the card's folder row instead). */
  onRunPlan?: (plan: ScheduledFlyway) => void;
  /** The plan whose Run now is starting. */
  runningPlan?: string;
  /** A plan card's Edit: its Takes off dialog. */
  onEditPlan?: (plan: ScheduledFlyway) => void;
  /** A plan card's Pause / Resume (true = pause: trigger.paused). */
  onPausePlan?: (plan: ScheduledFlyway, paused: boolean) => void;
  /** A plan card's Skip next (the slot's epoch) / Undo skip (0). */
  onSkipPlan?: (plan: ScheduledFlyway, until: number) => void;
  /** The plan whose Pause / Skip is being saved. */
  planFlagging?: string;
  /** A plan card's open Schedule… / Change schedule row. */
  planSched?: { id: string; form: WhenFields; error: string; busy: boolean } | null;
  /** Open (a plan) or close (null) that row. */
  onPlanSchedule?: (plan: ScheduledFlyway | null) => void;
  onPlanForm?: (patch: Partial<WhenFields>) => void;
  onPlanSave?: () => void;
  /** "Stop schedule": the trigger keeps no time (it starts nothing). */
  onPlanStop?: () => void;
  /** The server's reading of the plan row's schedule. */
  planPreview?: CronPreview | null;
  /** A plan card's folder row (Run now with no repository). */
  planFolder?: { id: string; path: string; keep: boolean } | null;
  /** Change it (a patch) or close it (null). */
  onPlanFolder?: (patch: Partial<{ path: string; keep: boolean }> | null) => void;
  /** The folder row's Run now: start it in `path` (and keep it when `keep`). */
  onRunPlanIn?: (plan: ScheduledFlyway, path: string, keep: boolean) => void;
}

/** Markup only: everything it shows comes in as props. */
export function TemplatesDialogView(p: ViewProps) {
  const { templates, ui, form, busy, loaded } = p;
  const runs = p.runs || { runs: {}, runs_here: true, runs_on: "" };
  const loadError = p.loadError || "";
  // The editor's folder browser takes Escape first (it puts the folder back).
  const escapeRef = useRef<(() => boolean) | null>(null);
  // Leaving an editor with changes asks first (Edit on another card, + New,
  // Delete, Escape, Close) rather than dropping what was typed.
  const discardRow = ui.discarding ? (
    <div className="tpl-confirm tpl-discard" role="alert">
      <span>Discard changes{ui.editing ? ` to “${ui.editing}”` : ""}?</span>
      <button type="button" className="tpl-btn tpl-del" onClick={p.onDiscard}>
        Discard
      </button>
      <button type="button" className="tpl-btn" onClick={() => p.dispatch({ t: "keep-editing" })}>
        Keep editing
      </button>
    </div>
  ) : null;
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
      installed={p.installed}
      githubConnected={p.githubConnected}
      errorReason={ui.reason}
      focusFolder={ui.focus === "folder"}
    />
  );
  const scheduleEditor = (t: Template) => (
    <ScheduleEditor
      form={form}
      onForm={p.onForm}
      error={ui.error}
      busy={busy}
      onSave={p.onSave}
      onCancel={() => p.dispatch({ t: "cancel" })}
      onStop={hasSchedule(t) ? p.onStopSchedule : undefined}
      onEdit={() => p.dispatch({ t: "edit", name: t.name })}
      tz={p.tz}
      cronPreview={p.cronPreview}
      runs={p.runs}
    />
  );

  const now = p.now || new Date();
  const infoFor = (t: Template): TemplateRunInfo => runs.runs[t.name.trim().toLowerCase()] || {};

  /** A run's session: a link that closes Schedule and focuses it, or the
   * name + "(closed)" once it's gone (plain text when nobody knows). */
  const sessionRef = (title: string): ReactNode => {
    const live = p.liveSessions;
    if (!live) return title;
    if (!live.has(title))
      return (
        <span className="tpl-session-gone">
          {title} <span className="muted">(closed)</span>
        </span>
      );
    return (
      <button
        type="button"
        className="linklike tpl-session-link"
        title={"Show “" + title + "”"}
        onClick={() => p.onOpenSession?.(title)}
      >
        {title}
      </button>
    );
  };

  /** historyLine, with its session as a link. */
  const historyRow = (h: TemplateRunEntry): ReactNode => {
    const line = historyLine(h, now);
    if (!h.session || h.skipped || failedRow(h)) return line;
    const at = line.indexOf(" → " + h.session);
    if (at < 0) return line;
    return (
      <>
        {line.slice(0, at)} → {sessionRef(h.session)}
        {line.slice(at + 3 + h.session.length)}
      </>
    );
  };

  /** Run now / Edit / Delete — every card's own verbs; the schedule's live on
   * its strip. An unscheduled card adds Schedule…, the way to give it one. */
  const actions = (t: Template) => (
    <div className="tpl-actions">
      <button
        type="button"
        className="tpl-btn tpl-run"
        data-tour="schedule-run"
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
          title="Start it by itself — every day, weekdays, once or on a cron"
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

  /** The schedule strip of a scheduled card — a template's, or a saved
   * stop's read as one (planAsTemplate): cadence, when next, and its three
   * switches — Change schedule, Skip next / Undo skip, Pause / Resume. */
  type StripActs = { flagging: boolean; onChange(): void; onSkip(until: number): void; onPause(paused: boolean): void };
  const templateActs = (t: Template): StripActs => ({
    flagging: p.flagging === t.name,
    onChange: () => p.dispatch({ t: "schedule", name: t.name }),
    onSkip: (until) => p.onSkip?.(t.name, until),
    onPause: (paused) => p.onPause?.(t.name, paused),
  });
  const strip = (t: Template, info: TemplateRunInfo, st: TemplateRunsStatus = runs, acts: StripActs = templateActs(t)) => {
    const paused = !!(t.schedule?.paused || info.paused);
    const next = nextLine(t, info, st, now);
    const skipping = !paused && info.skipping_at ? info.skipping_at : 0;
    const flagging = acts.flagging;
    return (
      <div className={"sch-strip" + (paused ? " is-paused" : "")}>
        <div className="sch-when">
          {/* THE schedule line (New's chip and Intake say the same), then how
              far off the next run is. */}
          {paused ? (
            <>
              <span className="sch-cadence">{cadenceText(t, info, st)}</span>
              <span className="sch-next sch-paused-chip">Paused</span>
            </>
          ) : (
            <>
              <span className="sch-cadence">{templateScheduleLine(t, st, now)}</span>
              {next && <span className="sch-next">{next}</span>}
            </>
          )}
        </div>
        <div className="sch-switches">
          <button
            type="button"
            className="sch-sw tpl-sched"
            title="Change when it runs, or stop its schedule — just the schedule"
            onClick={acts.onChange}
          >
            Change schedule
          </button>
          {!paused &&
            (skipping ? (
              <button
                type="button"
                className="sch-sw sch-skip"
                disabled={flagging}
                onClick={() => acts.onSkip(0)}
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
                  onClick={() => acts.onSkip(Number(info.next_at))}
                >
                  Skip next
                </button>
              )
            ))}
          {/* A spent one-off has nothing left to pause: Change schedule is
              the way to run it again. */}
          {(paused || !info.once_done) && (
            <button
              type="button"
              className="sch-sw sch-pause"
              disabled={flagging}
              title={paused ? "Start running on its schedule again" : "Keep the schedule, start nothing until you resume"}
              onClick={() => acts.onPause(!paused)}
            >
              {paused ? "Resume" : "Pause"}
            </button>
          )}
        </div>
      </div>
    );
  };

  /** What happened last, and the fold with the rest. */
  const outcome = (info: TemplateRunInfo) => {
    const last = lastLine(info, now);
    const session = info.last_session || "";
    return (
      <>
        {lastRunFailed(info) ? (
          <div className="tpl-run-error error">Last run failed: {info.last_error}</div>
        ) : info.last_skip ? (
          <div className="tpl-run-skip muted">
            Skipped {whenLabel(info.last_skip_at, now)}: {info.last_skip}
          </div>
        ) : (
          last && (
            <div className="tpl-runs">
              {session && last.endsWith(" → " + session) ? (
                <>
                  {last.slice(0, last.length - session.length)}
                  {sessionRef(session)}
                </>
              ) : (
                last
              )}
            </div>
          )
        )}
        {info.history && info.history.length > 0 && (
          <details className="tpl-history">
            <summary>Recent runs ({info.history.length})</summary>
            <ul>
              {info.history.map((h, i) => (
                <li key={i} className={failedRow(h) ? "error" : h.skipped ? "muted" : ""}>
                  {historyRow(h)}
                </li>
              ))}
            </ul>
          </details>
        )}
      </>
    );
  };

  const card = (t: Template) => {
    if (ui.editing === t.name && !ui.scheduleOnly)
      return (
        <div key={t.name} className="tpl-card tpl-card-editing" data-name={t.name}>
          {discardRow}
          {editor}
        </div>
      );
    const schedOpen = ui.editing === t.name && !!ui.scheduleOnly;
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
          (!paused && lastRunFailed(info) ? " is-failing" : "")
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
        {schedOpen ? (
          <div className="tpl-sched-row">
            {discardRow}
            {scheduleEditor(t)}
          </div>
        ) : (
          scheduled && strip(t, info)
        )}
        {outcome(info)}
        {t.prompt && <div className="tpl-prompt">{t.prompt}</div>}
        {runProblem(t) && (
          /* Said on the card, not only in a hover title: a disabled button
             that won't say why is furniture. */
          <div className="tpl-needs">
            Run now and schedules need a folder.{" "}
            <button
              type="button"
              className="linklike"
              onClick={() => p.dispatch({ t: "edit", name: t.name, focus: "folder" })}
            >
              Add one
            </button>
          </div>
        )}
        {ui.confirming === t.name && (
          <div className="tpl-confirm" role="group" aria-live="polite">
            <span>
              Delete “{t.name}”?{p.otherDevices ? " This also removes it from your other linked devices." : ""}
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
    );
  };

  /** A plan card's folder row: Run now on a saved stop with no repository
   * asks where, in the card — the folder picker (Browse…), the most recent
   * repo already filled in, and "Keep it for its scheduled runs" (its
   * schedule can't start without one either). Never a dead-end error. */
  const folderRow = (f: ScheduledFlyway) => {
    const pf = p.planFolder;
    if (!pf || pf.id !== f.id) return null;
    const running = p.runningPlan === f.id;
    return (
      <div className="tpl-sched-row sch-plan-folder">
        <form
          className="tpl-editor tpl-sched-editor"
          onSubmit={(e) => {
            e.preventDefault();
            if (pf.path.trim()) p.onRunPlanIn?.(f, pf.path.trim(), pf.keep);
          }}
        >
          <label className="sch-plan-folder-label" htmlFor={"sch-plan-folder-" + f.id}>
            Which folder should it start in?
          </label>
          <PathField
            id={"sch-plan-folder-" + f.id}
            value={pf.path}
            placeholder="A folder — usually a git repository"
            pickerTitle={"The folder “" + f.name + "” starts its session in"}
            onChange={(v) => p.onPlanFolder?.({ path: v })}
          />
          <label className="check sch-plan-keep">
            <input type="checkbox" checked={pf.keep} onChange={(e) => p.onPlanFolder?.({ keep: e.target.checked })} />
            Keep it for its scheduled runs too
          </label>
          <div className="tpl-row-actions">
            <p className="error" aria-live="polite" />
            <button type="button" className="tpl-cancel" onClick={() => p.onPlanFolder?.(null)}>
              Cancel
            </button>
            <button type="submit" className="tpl-save" disabled={!pf.path.trim() || running}>
              {running ? "Starting…" : "▶ Run now"}
            </button>
          </div>
        </form>
      </div>
    );
  };

  /** A saved stop that starts on a timer, as a template card: what it is,
   * when it runs, how its last run went — and a template card's verbs. */
  const planCard = (f: ScheduledFlyway) => {
    const { t, info, status: st } = planAsTemplate(f);
    const scheduled = hasSchedule(t);
    const paused = scheduled && !!(t.schedule?.paused || info.paused);
    const stops = f.steps.length + (f.steps.length === 1 ? " stop" : " stops");
    const schedOpen = p.planSched?.id === f.id;
    const folderOpen = p.planFolder?.id === f.id;
    const noFolder = !(f.session?.repo_path || "").trim();
    return (
      <div
        key={"plan:" + f.id}
        className={
          "tpl-card sch-flyway" +
          (scheduled ? " is-scheduled" : "") +
          (paused ? " is-paused" : "") +
          (!paused && lastRunFailed(info) ? " is-failing" : "")
        }
        data-plan={f.id}
      >
        <div className="tpl-card-top">
          <div className="tpl-card-text">
            <div className="tpl-name">
              <Bird size={12} className="sch-flyway-bird" />
              {f.name}
            </div>
            <div className="tpl-meta" title={f.session?.repo_path || undefined}>
              {[stops, f.session?.repo_path ? leafName(f.session.repo_path) : "", f.session?.program || ""].filter(Boolean).join(" · ")}
            </div>
          </div>
          <div className="tpl-actions">
            <button
              type="button"
              className="tpl-btn tpl-run sch-plan-run"
              disabled={!!p.runningPlan}
              title={
                noFolder
                  ? "Run now — pick the folder its new session starts in"
                  : "Run now — starts a new session and runs these stops in it"
              }
              onClick={() => p.onRunPlan?.(f)}
            >
              {p.runningPlan === f.id ? "Starting…" : "▶ Run now"}
            </button>
            {!scheduled && (
              <button
                type="button"
                className="tpl-btn tpl-sched"
                title="Start it by itself — every day, weekdays, once or on a cron"
                onClick={() => p.onPlanSchedule?.(f)}
              >
                Schedule…
              </button>
            )}
            <button
              type="button"
              className="tpl-btn sch-plan-edit"
              title="Edit — when it runs, and the repository, agent and model its sessions start in"
              onClick={() => p.onEditPlan?.(f)}
            >
              Edit
            </button>
          </div>
        </div>
        {schedOpen && p.planSched ? (
          <div className="tpl-sched-row">
            <ScheduleEditor
              form={p.planSched.form}
              onForm={(patch) => p.onPlanForm?.(patch)}
              error={p.planSched.error}
              busy={p.planSched.busy}
              onSave={() => p.onPlanSave?.()}
              onCancel={() => p.onPlanSchedule?.(null)}
              onStop={scheduled ? p.onPlanStop : undefined}
              tz={info.tz || st.tz || p.tz}
              cronPreview={p.planPreview}
              runs={st}
            />
          </div>
        ) : (
          scheduled &&
          strip(t, info, st, {
            flagging: p.planFlagging === f.id,
            onChange: () => p.onPlanSchedule?.(f),
            onSkip: (until) => p.onSkipPlan?.(f, until),
            onPause: (paused) => p.onPausePlan?.(f, paused),
          })
        )}
        {folderRow(f)}
        {outcome(info)}
        {noFolder && !folderOpen && (
          <div className="tpl-needs">
            Run now and schedules need a folder.{" "}
            <button type="button" className="linklike" onClick={() => p.onRunPlan?.(f)}>
              Pick one
            </button>
          </div>
        )}
      </div>
    );
  };
  const planList = scheduledFlyways(p.flyways);
  const unscheduledPlans = planList.filter((f) => !f.trigger.schedule);

  const entries = scheduleEntries(templates, runs, p.flyways);
  const saved = templates.filter((t) => !hasSchedule(t));
  const up = nextAcross(templates, runs, p.flyways);
  const elsewhere = !runs.runs_here && !!runs.runs_on;
  // No templates yet: the invitation to make one leads (saved stops on a
  // schedule, the built-ins among them, still list below it).
  const empty = loaded && templates.length === 0 && ui.editing !== "";
  const entry = (e: ScheduleEntry) => (e.kind === "template" ? card(e.t) : planCard(e.f));

  const onEscape = () => {
    if (ui.discarding) {
      p.dispatch({ t: "keep-editing" });
      return true;
    }
    if (ui.editing !== null && escapeRef.current?.()) return true;
    if (ui.editing === null && !ui.confirming) return false;
    p.dispatch(ui.editing !== null ? { t: "dismiss" } : { t: "cancel" });
    return true;
  };
  const body = (
      <div
        id="templates-card"
        role={p.embedded ? undefined : "dialog"}
        aria-modal={p.embedded ? undefined : "true"}
        aria-labelledby={p.embedded ? undefined : "templates-title"}
      >
        <div className="tpl-head">
          {p.embedded ? <span className="tpl-head-title muted">Templates</span> : <h2 id="templates-title">Schedule</h2>}
          <div className="tpl-head-acts">
            {ui.editing !== "" && !empty && (
              <button
                type="button"
                id="templates-new"
                className="tpl-new"
                data-tour="schedule-new"
                onClick={() => p.dispatch({ t: "new" })}
              >
                + New template
              </button>
            )}
            <TourButton tour="schedule" />
            {!p.embedded && (
              <button type="button" id="templates-close" onClick={p.onClose}>
                Close
              </button>
            )}
          </div>
        </div>
        {!empty && (
          <p className="tpl-intro muted">
            Everything here starts a <strong>new</strong> session by itself on a timer — or right now, with Run now.
            <span className="tpl-intro-aside">To send text to a session that's already running, use Prompts.</span>
          </p>
        )}
        <div className="tpl-list" id="templates-list">
          {ui.editing === "" && (
            <div className="tpl-card tpl-card-editing tpl-card-new">
              {discardRow}
              {editor}
            </div>
          )}
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
              <p>Save a session as a template once, then start it with one click — or every night by itself.</p>
              <button
                type="button"
                id="schedule-empty-create"
                className="tpl-btn tpl-run"
                data-tour="schedule-new"
                onClick={() => p.dispatch({ t: "new" })}
              >
                Create a template
              </button>
            </div>
          )}
          {(templates.length > 0 || planList.length > 0) && (
            <>
              {(up || elsewhere) && (
                <div className="sch-next-up" id="schedule-next">
                  {elsewhere ? (
                    <>
                      Schedules start on <strong>{runs.runs_on}</strong> — the computer that runs schedules.
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
              {/* ONE list of what starts by itself on a timer: templates and
                  saved stops on a schedule, side by side, the same verbs. */}
              {(entries.length > 0 || templates.length > 0) && (
                <section className="sch-section" id="schedule-scheduled" aria-labelledby="schedule-scheduled-h">
                  <h3 id="schedule-scheduled-h">
                    Scheduled{entries.length > 0 && <span className="sch-count">{entries.length}</span>}
                  </h3>
                  {entries.length ? (
                    entries.map(entry)
                  ) : (
                    <p className="sch-none muted">
                      Nothing runs by itself yet — <strong>Schedule…</strong> on a card below starts it every day,
                      weekday, once or on any cron.
                    </p>
                  )}
                </section>
              )}
              {saved.length + unscheduledPlans.length > 0 && (
                <section className="sch-section" id="schedule-saved" aria-labelledby="schedule-saved-h">
                  <h3 id="schedule-saved-h">
                    Not scheduled<span className="sch-count">{saved.length + unscheduledPlans.length}</span>
                    <span className="sch-section-hint">run now or add a schedule</span>
                  </h3>
                  {saved.map(card)}
                  {unscheduledPlans.map(planCard)}
                </section>
              )}
            </>
          )}
        </div>
      </div>
  );
  // Embedded, the host is the Modal: it asks this view about Escape and
  // about leaving with unsaved edits (embedGuard).
  if (p.embedded) {
    embeddedGuard.current = { escape: onEscape, dirty: () => !!p.dirty };
    return body;
  }
  return (
    <Modal
      id="templates-dialog"
      // The top-bar dialogs' shared fixed top edge (Modal.css): opening the
      // editor grows the card down instead of re-centring it.
      className="top-dialog"
      onClose={p.onClose}
      // A press outside never throws away a half-edited template.
      closeOnBackdrop={!p.dirty}
      // Escape withdraws an open editor or delete question first; on "Discard
      // changes?" it keeps editing.
      onEscape={onEscape}
    >
      {body}
    </Modal>
  );
}

/** `embedded`: drawn inside another surface — its deep link ("new",
 * "schedule:<name>", …) comes in as `target` rather than from the store.
 * Nothing embeds it today; the top bar's Schedule opens it standalone. */
export function TemplatesDialog({ embedded = false, target: embeddedTarget = null }: { embedded?: boolean; target?: string | null } = {}) {
  const storeOpen = useUi((s) => s.openDialog === "schedule");
  const open = embedded || storeOpen;
  useAutoTour("schedule", open);
  const storeTarget = useUi((s) => s.dialogTarget);
  const target = embedded ? embeddedTarget : storeTarget;
  const closeDialog = useUi((s) => s.closeDialog);
  const [templates, setList] = useState<Template[]>(() => templatesNow());
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [ui, dispatch] = useReducer(templatesReducer, TEMPLATES_UI_INIT);
  const [form, setForm] = useState<TemplateForm>(() => formFromTemplate(null));
  // What the open editor started from: the form differs from it = unsaved.
  const [seed, setSeed] = useState<TemplateForm | null>(null);
  const [busy, setBusy] = useState(false);
  const [runs, setRuns] = useState<TemplateRunsStatus>(() => templateRunsNow());
  const [running, setRunning] = useState("");
  const [flagging, setFlagging] = useState("");
  // Saved stops on a schedule (the plans with a schedule trigger) are cards
  // in the same list.
  const flyways = useFlightPlans(open).data as ScheduledFlyway[] | null | undefined;
  const [runningPlan, setRunningPlan] = useState("");
  const [planFlagging, setPlanFlagging] = useState("");
  const [planSched, setPlanSched] = useState<{ id: string; form: WhenFields; error: string; busy: boolean } | null>(null);
  const [planPreview, setPlanPreview] = useState<CronPreview | null>(null);
  const [planFolder, setPlanFolder] = useState<{ id: string; path: string; keep: boolean } | null>(null);
  const planFolderSeeded = useRef(false);
  // A plan card's Edit: its Takes off dialog, over Schedule.
  const [takeoff, setTakeoff] = useState<ScheduledFlyway | null>(null);
  const [agents, setAgents] = useState<string[]>([]);
  const [installed, setInstalled] = useState<InstalledMap | null>(null);
  const [otherDevices, setOtherDevices] = useState(false);
  const fleetAsked = useRef(false);
  const folderSeeded = useRef(false);
  const [scheduleFocus, setScheduleFocus] = useState(false);
  const [preview, setPreview] = useState<CronPreview | null>(null);
  const editingRef = useRef<string | null>(null);
  const [folderSuggestions, setFolderSuggestions] = useState<RepoSuggestion[]>([]);
  const suggestAsked = useRef(false);
  const { data: config } = useConfig();
  const { data: instances } = useInstances();
  const github = useGithubConnected(open && ui.editing !== null && fastTrackNeedsGitHub(form.autopilot));
  const liveSessions = instances ? new Set(instances.map((i) => i.title)) : undefined;

  // The editor's folder chips — the New form's Recent / Home line. Asked
  // once per opening, and only when an editor opens: the endpoint walks the
  // disk, which a glance at the list should not pay for.
  const editorOpen = open && (ui.editing !== null || !!planFolder);
  useEffect(() => {
    if (!open) suggestAsked.current = false;
    if (!editorOpen || suggestAsked.current) return;
    suggestAsked.current = true;
    api<{ suggestions?: RepoSuggestion[] }>("/api/repos/suggest")
      .then((d) => setFolderSuggestions(d.suggestions || []))
      // Chips are sugar: the field and Browse… work without them.
      .catch(() => setFolderSuggestions([]));
  }, [open, editorOpen]);

  // A NEW template starts on the most recent repo (the "Use: 📦 repo" chip),
  // once per opening: a template with a folder can run, and clearing the
  // field afterwards is the person's call.
  useEffect(() => {
    if (ui.editing !== "") {
      folderSeeded.current = false;
      return;
    }
    if (folderSeeded.current || !folderSuggestions.length) return;
    folderSeeded.current = true;
    const path = defaultFolder(folderSuggestions);
    if (path) {
      setForm((f) => (f.repo_path ? f : { ...f, repo_path: path }));
      setSeed((f) => (f && !f.repo_path ? { ...f, repo_path: path } : f));
    }
  }, [ui.editing, folderSuggestions]);

  // A plan's folder row starts on the most recent repo too, once the chips
  // are in (a path typed meanwhile is never replaced).
  useEffect(() => {
    if (!planFolder) {
      planFolderSeeded.current = false;
      return;
    }
    if (planFolderSeeded.current || !folderSuggestions.length) return;
    planFolderSeeded.current = true;
    const path = defaultFolder(folderSuggestions);
    if (path) setPlanFolder((pf) => (pf && !pf.path ? { ...pf, path } : pf));
  }, [planFolder, folderSuggestions]);

  // A plan row's schedule, previewed by the server as it is typed — the
  // template editor's rule (debounced; a stale answer never lands).
  const planCron = planSched
    ? (planSched.form.days === "cron"
        ? planSched.form.cron
        : planSched.form.days === "once"
          ? onceOf(planSched.form.date, planSched.form.at)
          : cronOf(planSched.form.at, planSched.form.days)
      ).trim()
    : "";
  useEffect(() => {
    if (!open || !planCron) {
      setPlanPreview(null);
      return;
    }
    let live = true;
    const timer = setTimeout(() => {
      cronPreview(planCron)
        .then((pv) => live && setPlanPreview(pv))
        .catch(() => live && setPlanPreview(null));
    }, 300);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [open, planCron]);

  // Delete says "your other linked devices" only when there are some.
  useEffect(() => {
    if (!open || !ui.confirming || fleetAsked.current) return;
    fleetAsked.current = true;
    api<FleetStatus>("/api/fleet")
      .then((f) => setOtherDevices(!!f?.in_fleet && (f.members || []).some((m) => !m.self)))
      .catch(() => setOtherDevices(false));
  }, [open, ui.confirming]);

  // What the schedule means, asked of the server as it is typed (debounced;
  // a stale answer never overwrites a newer one) — in EVERY mode: days +
  // time asks as its cron ("30 7 * * 1-5"), so Every day and Weekdays show
  // their next runs too, and Once asks as its minute ("2026-10-28T21:00").
  const wantsPreview = open && ui.editing !== null && (form.scheduled || !!ui.scheduleOnly);
  const cronText = (
    form.days === "cron" ? form.cron : form.days === "once" ? onceOf(form.date, form.at) : cronOf(form.at, form.days)
  ).trim();
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
    setPlanSched(null);
    setPlanFolder(null);
    setTakeoff(null);
    // The palette's "Schedule a session…" opens straight onto a NEW template's
    // editor with its schedule already switched on; a template chip's ⏰ in
    // New opens that template's.
    const want = readTemplatesTarget(embedded ? embeddedTarget : useUi.getState().dialogTarget).schedule;
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
    // The editor's Agent picker: this device's agents, and which of them are
    // installed (a missing one is tagged, and can't be scheduled).
    api<{ providers?: Array<{ name: string }> }>("/api/providers/manage")
      .then((d) => {
        if (live) setAgents((d.providers || []).map((x) => x.name));
      })
      .catch(() => {});
    api<{ providers?: Array<{ name: string; installed?: boolean }> }>("/api/providers/status")
      .then((d) => {
        if (!live) return;
        const have: InstalledMap = {};
        for (const x of d.providers || []) if (typeof x.installed === "boolean") have[x.name] = x.installed;
        setInstalled(have);
      })
      .catch(() => {
        if (live) setInstalled(null);
      });
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
  }, [open, embedded ? embeddedTarget : null]);

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
    // A NEW template starts on the most recent repo (when the chips are in
    // already; the effect above fills it when they land later).
    if (!t && folderSuggestions.length) {
      f.repo_path = defaultFolder(folderSuggestions);
      folderSeeded.current = true;
    }
    // Schedule… on a card (its compact row) or the palette's New scheduled
    // template…: the schedule already on.
    if (scheduleFocus || ui.scheduleOnly) f.scheduled = true;
    setForm(f);
    setSeed(f);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ui.editing, templates]);

  if (!open) return null;

  const dirty = ui.editing !== null && formDirty(form, seed);

  const close = () => {
    closeDialog();
    // Came from the New dialog: hand the person back to it AS THEY LEFT IT
    // (newSessionResume), with the list they just edited reloaded.
    if (readTemplatesTarget(target).fromNew) {
      newSessionResume.keep = true;
      useUi.getState().openDialogFor("new-session");
    }
  };

  /** Stop schedule's Undo: the template as it was, schedule and all. */
  const undoStop = async (before: Template) => {
    try {
      await saveTemplate(before);
      setList(templatesNow());
      await ensureTemplates(true).catch(() => {});
      setRuns(templateRunsNow());
      toast(`“${before.name}” runs on its schedule again — ${templateScheduleLine(before, templateRunsNow())}`);
    } catch (err) {
      toast("Couldn't put the schedule back: " + errMsg(err));
    }
  };

  const save = async (f: TemplateForm = form, stopping = false) => {
    const r = templateBody(f, installed, templates, ui.editing);
    if ("error" in r) {
      dispatch({ t: "error", message: r.error, reason: /isn't installed/.test(r.error) ? "agent_missing" : "" });
      return;
    }
    const before = ui.editing ? templates.find((x) => x.name === ui.editing) || null : null;
    setBusy(true);
    try {
      await saveTemplate(r.body);
      const gone = ui.editing ? renameLeaves(ui.editing, r.body.name) : null;
      if (gone) await deleteTemplate(gone);
      const body = r.body;
      // The next run moves with a new schedule; a scheduled save says THE
      // schedule line (as the card will) once the server has placed it.
      void ensureTemplates(true)
        .catch(() => {})
        .then(() => {
          const st = templateRunsNow();
          setRuns(st);
          if (body.schedule && !stopping) toast(`Saved “${body.name}” — ${templateScheduleLine(body, st)}`);
        });
      setList(templatesNow());
      dispatch({ t: "saved" });
      if (stopping && before && hasSchedule(before)) {
        // Stopping a schedule is one click from a nightly run never
        // happening again: say so, and offer it back.
        toast(`Stopped the schedule of “${body.name}” — nothing starts on its own`, {
          action: { label: "Undo", onClick: () => void undoStop(before) },
        });
      } else if (!body.schedule) {
        // Saved, but it can't run yet: say what's missing, once.
        toast(runProblem(body) ? "Saved — add a folder to run it" : `Saved template “${body.name}”`);
      }
    } catch (err) {
      // The server's own sentence and what would fix it (409 agent_missing /
      // github_required), not its terse error.
      const why = templateRefusal(err);
      dispatch({ t: "error", message: why.text, reason: why.reason });
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

  // --- Saved stops on a schedule: a template card's verbs -------------------

  /** Save the plan as `change` makes it (the run record left off), and say
   * so; a refusal is the server's own sentence. */
  const putPlan = async (f: ScheduledFlyway, change: (b: FlightPlan) => FlightPlan, said: string): Promise<boolean> => {
    try {
      await savePlan(change(barePlan(f)));
      if (said) toast(said);
      return true;
    } catch (err) {
      toast("Couldn't change “" + f.name + "”: " + errMsg(err));
      return false;
    }
  };

  const flagPlan = async (f: ScheduledFlyway, change: (b: FlightPlan) => FlightPlan, said: string) => {
    setPlanFlagging(f.id);
    try {
      await putPlan(f, change, said);
    } finally {
      setPlanFlagging("");
    }
  };

  /** Run now: a flight takes off on the plan in a new session. With no
   * repository there is nowhere to start one, so the card asks (its folder
   * row) instead of failing with "pick a folder". */
  const runPlan = async (f: ScheduledFlyway, path = "", keep = false) => {
    if (runningPlan) return;
    const repo = (path || f.session?.repo_path || "").trim();
    if (!repo) {
      setPlanSched(null);
      setPlanFolder({ id: f.id, path: defaultFolder(folderSuggestions), keep: true });
      return;
    }
    setRunningPlan(f.id);
    try {
      if (path && keep) await putPlan(f, (b) => ({ ...b, session: { ...b.session, repo_path: repo } }), "");
      // takeOff selects the new session once its row exists.
      const flight = await takeOff(path ? { plan_id: f.id, repo_path: repo } : { plan_id: f.id });
      if (flight) {
        setPlanFolder(null);
        closeDialog();
      }
    } finally {
      setRunningPlan("");
    }
  };

  const savePlanSchedule = async (stop = false) => {
    if (!planSched) return;
    const f = (flyways || []).find((x) => x.id === planSched.id);
    if (!f) return setPlanSched(null);
    const next = stop ? { ...barePlan(f), trigger: { ...f.trigger, schedule: null } } : planWithWhen(barePlan(f), planSched.form);
    if (!next) {
      setPlanSched({ ...planSched, error: "Pick a time (24-hour, like 21:00), a date, or a cron expression" });
      return;
    }
    setPlanSched({ ...planSched, busy: true, error: "" });
    try {
      await savePlan(next);
      setPlanSched(null);
      if (stop) {
        const before = barePlan(f);
        toast(`Stopped the schedule of “${f.name}” — nothing starts on its own`, {
          action: { label: "Undo", onClick: () => void putPlan(f, () => before, `“${f.name}” runs on its schedule again`) },
        });
      } else toast(`Saved the schedule of “${f.name}”`);
    } catch (err) {
      setPlanSched((ps) => (ps ? { ...ps, busy: false, error: errMsg(err) } : ps));
    }
  };

  return (
    <>
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
          if (needsDiscard(ui, a, dirty)) {
            dispatch({ t: "ask-discard", then: a });
            return;
          }
          if (a.t !== "keep-editing") setScheduleFocus(a.t === "schedule");
          // A template's editor or row and a plan's row are never open at once.
          if (a.t === "new" || a.t === "edit" || a.t === "schedule") {
            setPlanSched(null);
            setPlanFolder(null);
          }
          dispatch(a);
        }}
        dirty={dirty}
        embedded={embedded}
        onDiscard={() => {
          const then = ui.discarding?.then;
          if (!then) return;
          dispatch({ t: "cancel" });
          if (then === "close") {
            close();
            return;
          }
          setScheduleFocus(then.t === "schedule");
          dispatch(then);
        }}
        agents={agents}
        folderSuggestions={folderSuggestions}
        homePath={config?.home || ""}
        scheduleFocus={scheduleFocus}
        tz={Object.values(runs.runs || {}).map((r) => r.tz).find(Boolean) || runs.tz || localZone()}
        onSave={() => void save()}
        onStopSchedule={() => void save({ ...form, scheduled: false }, true)}
        onDelete={(name) => void del(name)}
        onClose={() => (dirty ? dispatch({ t: "ask-discard", then: "close" }) : close())}
        installed={installed}
        githubConnected={github.data ?? null}
        liveSessions={liveSessions}
        onOpenSession={(title) => {
          closeDialog();
          selectSession(title);
        }}
        otherDevices={otherDevices}
        flyways={flyways}
        runningPlan={runningPlan}
        onRunPlan={(f) => void runPlan(f)}
        onRunPlanIn={(f, path, keep) => void runPlan(f, path, keep)}
        planFolder={planFolder}
        onPlanFolder={(patch) => setPlanFolder((pf) => (patch && pf ? { ...pf, ...patch } : null))}
        onEditPlan={(f) => setTakeoff(f)}
        planFlagging={planFlagging}
        onPausePlan={(f, paused) =>
          void flagPlan(
            f,
            (b) => ({ ...b, trigger: { ...b.trigger, paused } }),
            paused ? `Paused “${f.name}” — it starts nothing until you resume it` : `Resumed “${f.name}”`
          )
        }
        onSkipPlan={(f, until) =>
          void flagPlan(
            f,
            (b) => planWithSkip(b, until),
            until ? `“${f.name}” will skip ${whenLabel(until)} — the run after it goes ahead` : `“${f.name}” runs next time as usual`
          )
        }
        planSched={planSched}
        planPreview={planPreview}
        onPlanSchedule={(f) => {
          if (!f) return setPlanSched(null);
          // Its row replaces a template's (an unsaved template edit keeps its
          // place; only a clean one steps aside).
          if (!dirty) dispatch({ t: "cancel" });
          setPlanFolder(null);
          const when = whenFormOf(f.trigger.schedule);
          setPlanSched({ id: f.id, form: { ...when, seeded: "" }, error: "", busy: false });
        }}
        onPlanForm={(patch) => setPlanSched((ps) => (ps ? { ...ps, form: { ...ps.form, ...patch }, error: "" } : ps))}
        onPlanSave={() => void savePlanSchedule()}
        onPlanStop={() => void savePlanSchedule(true)}
      />
      {takeoff && <TakeoffDialog plan={takeoff} onClose={() => setTakeoff(null)} />}
    </>
  );
}
