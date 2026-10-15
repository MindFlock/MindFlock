/** A template's editor — Schedule's "+ New template", a card's Edit and its
 * Schedule… — and the form it edits (TemplateForm, the POST body it builds).
 *
 * It is the New form's own parts, not copies of them: the Folder field with
 * Browse… and the one line of Recent / Home chips (FolderField, FolderAids,
 * folderReducer), the Agent picker (AgentFields), the Task box that starts
 * small and grows (TaskField) and the "Works in" row (WorksInGroup). So a
 * template looks, lines up and behaves like the form it saves — the editor
 * used to draw its own: a bare path box with no Browse…, an Agent select and
 * a Folder field on different baselines (the folder's hint wrapped onto a line
 * of its own), and its own ragged copy of Works in.
 *
 * Top to bottom: Name; Folder beside Agent; the "Use: 📦 repo" chip; Task;
 * then the label column the New form uses for Works in — Works in, Schedule,
 * Fast-track — and a footer (Cancel / Save) that stays in view while the form
 * scrolls.
 *
 * A card's Schedule… / Change schedule open ScheduleEditor instead: the same
 * schedule fields (ScheduleWhen — days, a 24-hour time, the next-runs line)
 * and Save, nothing else. */

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type MutableRefObject,
  type SetStateAction,
} from "react";
import { deviceApi } from "../../lib/devices";
import {
  AUTOPILOT_CHOICES,
  atOfCron,
  cronOf,
  fastTrackNeedsGitHub,
  hasSchedule,
  normalizeAt,
  schedulePreviewLine,
  zoneSuffix,
  type CronPreview,
  type Template,
  type TemplateAutopilot,
  type TemplateRunsStatus,
} from "../../lib/templates";
import { openConnectGitHub } from "../../lib/githubConnect";
import { openSettings } from "../../lib/openSettings";
import { useUi } from "../../state/store";
import {
  folderReducer,
  type FolderAction,
  type FolderState,
  type InstalledMap,
  type RepoSuggestion,
} from "./newSession";
import { AgentFields } from "./newSession/AgentAccount";
import { FolderAids, FolderField, matchPicker, useFolderLookups, type FolderCheck, type FolderSearch } from "./newSession/FolderPicker";
import { TaskField } from "./newSession/PromptFold";
import {
  IN_PLACE_HINT,
  InitRepoCheck,
  ModeText,
  ProvisionCheck,
  StrategyPicker,
  WorksInGroup,
} from "./newSession/WorkspaceFold";

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
  /** Run on a schedule: every day / weekdays at `at` (local "HH:MM"), or
   * on the cron expression `cron` ("cron"). */
  scheduled: boolean;
  at: string;
  days: "daily" | "weekdays" | "cron";
  cron: string;
  /** How far each run carries its work on its own ("" = leave it). */
  autopilot: TemplateAutopilot | "";
  /** The row's Pause / Skip next, carried through an edit untouched (the
   * editor doesn't show them; the card does). */
  paused: boolean;
  skip_until: number;
  /** The cadence the form was seeded with: a skip is for a slot of THAT
   * cadence, so changing it drops the skip. */
  seeded: string;
}

/** A schedule's cadence as one comparable string ("" = none). */
function cadenceKey(s: { at?: string; days?: string; cron?: string } | null | undefined): string {
  if (!s) return "";
  return s.cron ? "cron:" + s.cron.trim().replace(/\s+/g, " ") : s.at ? s.at + "/" + (s.days || "daily") : "";
}

/** The folder a NEW template starts on: the most recent repo the folder
 * chips offer (a template with a folder can run; one without can't), else "". */
export function defaultFolder(suggestions: RepoSuggestion[] | null | undefined): string {
  const list = suggestions || [];
  const pick = list.find((s) => s.source === "recent" && s.is_git) || list.find((s) => s.is_git);
  return pick?.path || "";
}

/** The agent a form names, when it is KNOWN not to be installed here ("" =
 * installed, unknown, or the default agent — the server checks that one). */
export function missingAgent(program: string, installed: InstalledMap | null | undefined): string {
  const name = (program || "").trim();
  return name && installed && installed[name] === false ? name : "";
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
    scheduled: !!t && hasSchedule(t),
    at: t?.schedule?.at || "21:00",
    days: t?.schedule?.cron ? "cron" : t?.schedule?.days === "weekdays" ? "weekdays" : "daily",
    cron: t?.schedule?.cron || "0 21 * * 1-5",
    autopilot: t?.autopilot || "",
    paused: !!t?.schedule?.paused,
    skip_until: Number(t?.schedule?.skip_until) || 0,
    seeded: t && hasSchedule(t) ? cadenceKey(t.schedule) : "",
  };
}

/** The POST body for a form, or the reason it cannot be saved. `installed`
 * (this device's agents) refuses a schedule whose agent is missing — it
 * would fail every night with nobody watching. */
export function templateBody(
  f: TemplateForm,
  installed: InstalledMap | null = null
): { body: Template } | { error: string } {
  const name = f.name.trim();
  if (!name) return { error: "A template needs a name." };
  if (name.length > 100) return { error: "Names are at most 100 characters." };
  const at = f.at.trim();
  const cron = f.cron.trim().replace(/\s+/g, " ");
  if (f.scheduled) {
    if (f.days === "cron") {
      if (!cron) return { error: "Type a cron expression, like 0 21 * * 1-5 (weekdays at 21:00)." };
    } else if (!/^([01]?\d|2[0-3]):[0-5]\d$/.test(at)) return { error: "Pick a time for the schedule, like 21:00." };
    if (!f.repo_path.trim() && f.mode !== "provisioned")
      return { error: "A scheduled template needs a folder to start its session in." };
    if (!f.prompt.trim()) return { error: "A scheduled template needs a task — it's what the session is told to do." };
    const missing = missingAgent(f.program, installed);
    if (missing) return { error: missing + " isn't installed — Install or pick another agent" };
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
      ...(f.scheduled ? { schedule: scheduleOf(f, at, cron) } : {}),
      ...(f.autopilot ? { autopilot: f.autopilot } : {}),
    },
  };
}

/** The schedule a form saves: its cadence, plus the card's Pause and (for
 * the same cadence, still ahead) Skip next. */
function scheduleOf(f: TemplateForm, at: string, cron: string): NonNullable<Template["schedule"]> {
  const cadence: NonNullable<Template["schedule"]> =
    f.days === "cron" ? { cron } : { at, days: f.days === "weekdays" ? "weekdays" : "daily" };
  const out = { ...cadence };
  if (f.paused) out.paused = true;
  const sameCadence = (f.seeded || "") === cadenceKey(cadence);
  if (f.skip_until && sameCadence && f.skip_until > Date.now() / 1000) out.skip_until = f.skip_until;
  return out;
}

/** The line under a cron field: its words and next runs, or why it can't be
 * saved ("" while nothing is known yet). */
export function cronPreviewLine(pv: CronPreview | null | undefined, now: Date = new Date(), zone = ""): string {
  return schedulePreviewLine(pv, now, zone);
}

/** The form after its days select changes. Custom (cron) starts from the
 * days + time on screen ("30 7 * * *" for Every day 07:30) — it used to drop
 * them for a stock "0 21 * * 1-5" — and leaving it keeps a simple cron's
 * time. */
export function daysPatch(f: Pick<TemplateForm, "days" | "at" | "cron">, raw: string): Partial<TemplateForm> {
  const days: TemplateForm["days"] = raw === "weekdays" ? "weekdays" : raw === "cron" ? "cron" : "daily";
  if (days === f.days) return {};
  if (days === "cron") return { days, cron: cronOf(f.at, f.days) || f.cron };
  if (f.days === "cron") {
    const back = atOfCron(f.cron);
    return back ? { days, at: back.at } : { days };
  }
  return { days };
}

/** The Folder field's browse state over a path the FORM owns. The New form
 * keeps the path in folderReducer itself; here the form's repo_path is the
 * one truth (the editor is seeded after it mounts, so a second copy would
 * race the seed), and only the browser's open/undo live here — every action
 * still goes through folderReducer, so Browse…, Escape and "use this folder"
 * mean exactly what they mean in New. A template has no pre-fill to outvote:
 * the field is always "touched". */
export type TemplateBrowse = Pick<FolderState, "browsing" | "undo">;

export const TEMPLATE_BROWSE_INIT: TemplateBrowse = { browsing: false, undo: null };

/** One Folder-field action on (the form's path, the browse state): the New
 * form's folderReducer, with the path handed back for the form to keep. */
export function templateFolderStep(
  path: string,
  browse: TemplateBrowse,
  a: FolderAction
): { path: string; browse: TemplateBrowse } {
  const next = folderReducer({ path, browsing: browse.browsing, undo: browse.undo, touched: true }, a);
  return { path: next.path, browse: { browsing: next.browsing, undo: next.undo } };
}

export function useTemplateFolder(path: string, setPath: (p: string) => void) {
  const [browse, setBrowse] = useState<TemplateBrowse>(TEMPLATE_BROWSE_INIT);
  const folderDo: Dispatch<FolderAction> = (a) => {
    const next = templateFolderStep(path, browse, a);
    if (next.path !== path) setPath(next.path);
    setBrowse(next.browse);
  };
  return { browsing: browse.browsing, folderDo };
}

export interface TemplateEditorProps {
  form: TemplateForm;
  onForm: (patch: Partial<TemplateForm>) => void;
  /** Why the last Save was refused ("" = it wasn't). */
  error: string;
  busy: boolean;
  onSave: () => void;
  onCancel: () => void;
  /** The agents to offer (GET /api/providers/manage). */
  agents?: string[];
  /** Opened by a card's Schedule…: the time takes the caret. */
  scheduleFocus?: boolean;
  /** This device's time zone ("EDT"), which a schedule's time is in. */
  tz?: string;
  /** The server's reading of the cron field being typed. */
  cronPreview?: CronPreview | null;
  /** Whether schedules fire here (else "Starts on <device>"). */
  runs?: Pick<TemplateRunsStatus, "runs_here" | "runs_on">;
  /** The folder chips (GET /api/repos/suggest) and $HOME, where Browse…
   * starts when the field is empty. */
  folderSuggestions?: RepoSuggestion[];
  homePath?: string;
  /** The dialog's Escape asks this first: true = it closed the browser. */
  escapeRef?: MutableRefObject<(() => boolean) | null>;
  /** This device's agents (GET /api/providers/status): a missing one is
   * tagged "(not installed)" as in New. null = unknown. */
  installed?: InstalledMap | null;
  /** Whether GitHub is connected (null/undefined = unknown): Open PR / Merge
   * when green without it says "Needs GitHub — Connect GitHub". */
  githubConnected?: boolean | null;
  /** Why the server refused the last Save ("agent_missing" /
   * "github_required" / ""), so the line can offer the fix. */
  errorReason?: string;
  /** "Add one" opened it: Folder takes the caret and a highlight. */
  focusFolder?: boolean;
}

/** Days ▾ + a 24-hour time (or a cron expression), and the line under them
 * that says what that means — "Every day at 07:30 EDT — next tomorrow 07:30,
 * Fri 07:30, Sat 07:30" — in every mode, from the server's own reading. */
export function ScheduleWhen({
  form,
  onForm,
  tz,
  cronPreview,
  runs,
  focusTime,
}: {
  form: TemplateForm;
  onForm: (patch: Partial<TemplateForm>) => void;
  tz?: string;
  cronPreview?: CronPreview | null;
  runs?: Pick<TemplateRunsStatus, "runs_here" | "runs_on">;
  focusTime?: boolean;
}) {
  const status = runs || { runs_here: true, runs_on: "" };
  const zone = zoneSuffix(tz || "", status);
  const elsewhere = !status.runs_here && status.runs_on;
  const line = cronPreviewLine(cronPreview, new Date(), zone);
  return (
    <>
      <div className="tpl-schedule-when">
        <select
          id="tpl-days"
          aria-label="Which days"
          value={form.days}
          onChange={(e) => onForm(daysPatch(form, e.target.value))}
        >
          <option value="daily">Every day</option>
          <option value="weekdays">Weekdays</option>
          <option value="cron">Custom (cron)</option>
        </select>
        {form.days === "cron" ? (
          <input
            id="tpl-cron"
            aria-label="Cron expression"
            autoComplete="off"
            spellCheck={false}
            value={form.cron}
            onChange={(e) => onForm({ cron: e.target.value })}
            placeholder="*/30 * * * *"
          />
        ) : (
          <>
            <span className="tpl-at">at</span>
            {/* A 24-hour text field, not <input type=time>: that one draws
                "09:00 PM" in a 12-hour locale while every summary says
                "21:00" — one format everywhere. */}
            <input
              id="tpl-at"
              type="text"
              inputMode="numeric"
              aria-label="Time (24-hour)"
              autoComplete="off"
              maxLength={5}
              placeholder="21:00"
              autoFocus={focusTime === true}
              value={form.at}
              onChange={(e) => onForm({ at: e.target.value })}
              onBlur={(e) => {
                const at = normalizeAt(e.target.value);
                if (at !== form.at) onForm({ at });
              }}
            />
          </>
        )}
      </div>
      <p
        className={"tpl-cron-preview" + (cronPreview && !cronPreview.ok ? " error" : " muted")}
        id="tpl-cron-preview"
        aria-live="polite"
      >
        {line || (form.days === "cron" ? "minute hour day-of-month month weekday —" + (zone || " local time") : " ")}
      </p>
      <p className="tpl-cron-preview tpl-sched-note muted">
        {elsewhere ? "Starts on " + status.runs_on + " — the computer that runs schedules. " : ""}
        Skipped while the last run is still working or waiting on you.
      </p>
    </>
  );
}

/** A card's Schedule… / Change schedule: the schedule alone, inside the
 * card — days, time, the next-runs line, Save. Edit keeps the whole form.
 * "Stop schedule" (a scheduled one) is what unticking the box was. */
export function ScheduleEditor(p: {
  form: TemplateForm;
  onForm: (patch: Partial<TemplateForm>) => void;
  error: string;
  busy: boolean;
  onSave: () => void;
  onCancel: () => void;
  /** Saves the template with no schedule (shown for a scheduled one). */
  onStop?: () => void;
  tz?: string;
  cronPreview?: CronPreview | null;
  runs?: Pick<TemplateRunsStatus, "runs_here" | "runs_on">;
  /** The whole form, for an error only it can fix (no folder, no task). */
  onEdit?: () => void;
}) {
  return (
    <form
      className="tpl-editor tpl-sched-editor"
      onSubmit={(e) => {
        e.preventDefault();
        p.onSave();
      }}
    >
      <div className="tpl-schedule tpl-sched-only" data-tour="schedule-when">
        <ScheduleWhen
          form={p.form}
          onForm={p.onForm}
          tz={p.tz}
          cronPreview={p.cronPreview}
          runs={p.runs}
          focusTime
        />
      </div>
      <div className="tpl-row-actions">
        {p.error && (
          <p className="error" aria-live="polite">
            {p.error}
            {p.onEdit && /folder|task/.test(p.error) && (
              <>
                {" "}
                <button type="button" className="linklike" onClick={p.onEdit}>
                  Edit
                </button>
              </>
            )}
          </p>
        )}
        {p.onStop && (
          <button type="button" className="tpl-stop" disabled={p.busy} onClick={p.onStop}>
            Stop schedule
          </button>
        )}
        <button type="button" className="tpl-cancel" onClick={p.onCancel}>
          Cancel
        </button>
        <button type="submit" className="tpl-save" disabled={p.busy}>
          {p.busy ? "Saving…" : "Save"}
        </button>
      </div>
    </form>
  );
}


export function TemplateEditor(p: TemplateEditorProps) {
  const { form, onForm } = p;
  const path = form.repo_path;
  const { browsing, folderDo } = useTemplateFolder(path, (repo_path) => onForm({ repo_path }));
  const folderPath = path.trim();

  // The New form's lookups, on the same field: the git probe (the "no git
  // repo here — Create one" line, and whether git init is offered at all)
  // and the folder-name search.
  const dapi = useMemo(() => deviceApi(""), []);
  const [folderCheck, setFolderCheck] = useState<FolderCheck | null>(null);
  const [search, setSearch] = useState<FolderSearch>(null);
  const [searchSel, setSearchSel] = useState(0);
  const [searchOpen, setSearchOpen] = useState(true);
  const searchListRef = useRef<HTMLDivElement | null>(null);
  useFolderLookups({
    open: true,
    repoPath: path,
    dapi,
    setFolderCheck,
    setSearch,
    setSearchSel,
    searchListRef,
    searchSel,
    search,
    searchOpen,
  });
  const searchHits = search && search.asked === folderPath && searchOpen && !browsing ? search : null;
  const selIndex = searchHits && searchHits.matches.length ? Math.min(searchSel, searchHits.matches.length - 1) : -1;
  const checked = folderCheck?.asked === folderPath ? folderCheck : null;
  const plainFolder = !!checked?.plain;
  const isGit = !!checked?.isGit;
  const pickMatch = matchPicker(folderDo, setSearchOpen);

  if (p.escapeRef)
    p.escapeRef.current = () => {
      if (!browsing) return false;
      folderDo({ t: "browse-cancel" });
      return true;
    };

  const setPrompt: Dispatch<SetStateAction<string>> = (v) =>
    onForm({ prompt: typeof v === "function" ? v(form.prompt) : v });
  const missing = missingAgent(form.program, p.installed);
  const needsGitHub = fastTrackNeedsGitHub(form.autopilot) && p.githubConnected === false;
  // "Use: 📦 repo": the folders worth one click, recent first — the chips
  // that used to say "Started in" / "Recent" (a status, not an action).
  const useChips = (p.folderSuggestions || []).filter((x) => x.is_git || x.source === "recent").slice(0, 3);
  const folderEmpty = !folderPath && form.mode !== "provisioned";
  // "Add one" on a card: straight to the field that makes it runnable.
  useEffect(() => {
    if (!p.focusFolder) return;
    const el = document.getElementById("tpl-repo-path");
    el?.scrollIntoView?.({ block: "center" });
    el?.focus();
  }, [p.focusFolder]);

  return (
    <form
      className="tpl-editor"
      onSubmit={(e) => {
        e.preventDefault();
        p.onSave();
      }}
    >
      <label className="tpl-name-field" data-tour="schedule-name">
        Name
        <input
          id="tpl-name"
          autoComplete="off"
          value={form.name}
          onChange={(e) => onForm({ name: e.target.value })}
          placeholder="e.g. nightly-deps"
        />
      </label>

      {/* Folder (wide) beside Agent (narrow): the New form's row. */}
      <div className="nf-quick">
        <div
          className={"tpl-folder-wrap" + (p.focusFolder && folderEmpty ? " tpl-flash" : "")}
          data-tour="schedule-folder"
        >
          <FolderField
            scope="tpl"
            placeholder="The repo it works in"
            repoPath={path}
            folderPath={folderPath}
            folderDo={folderDo}
            search={search}
            searchHits={searchHits}
            searchSel={searchSel}
            setSearchSel={setSearchSel}
            setSearchOpen={setSearchOpen}
            selIndex={selIndex}
            browserOpen={browsing}
            pickMatch={pickMatch}
          />
        </div>
        <AgentFields
          scope="tpl"
          defaultLabel="Your default agent"
          program={form.program}
          providers={(p.agents || []).map((name) => ({ name }))}
          installed={p.installed ?? null}
          setAgent={(program) => onForm({ program })}
          device=""
          closeDialog={() => useUi.getState().closeDialog()}
        />
      </div>
      <FolderAids
        scope="tpl"
        searchHits={searchHits}
        selIndex={selIndex}
        searchListRef={searchListRef}
        setSearchSel={setSearchSel}
        pickMatch={pickMatch}
        homePath={p.homePath || ""}
        newFolderRow={null}
        plainFolder={plainFolder}
        initRepo={form.init_repo}
        armInitRepo={() => onForm({ init_repo: true })}
        suggestions={[]}
        folderPath={folderPath}
        folderDo={folderDo}
        browserOpen={browsing}
        device=""
      />
      {useChips.length > 0 && !browsing && (
        <div id="tpl-suggest" className="nf-suggest nf-folders tpl-use" role="group" aria-label="Folders you might mean">
          <span className="nf-suggest-label">Use:</span>
          {useChips.map((x) => {
            const active = folderPath === x.path;
            return (
              <button
                key={x.path}
                type="button"
                className={"nt-chip" + (x.is_git ? " is-git" : "") + (active ? " active" : "")}
                data-path={x.path}
                aria-pressed={active}
                title={x.path}
                onClick={() => folderDo({ t: "user-set", path: x.path })}
              >
                {(x.is_git ? "📦 " : "📁 ") + x.name}
              </button>
            );
          })}
        </div>
      )}
      {missing && (
        <p className={"tpl-agent-missing" + (form.scheduled ? " error" : " muted")} role="note" aria-live="polite">
          {missing} isn't installed —{" "}
          <button
            type="button"
            className="linklike"
            onClick={() => {
              useUi.getState().closeDialog();
              openSettings("coding");
            }}
          >
            Install
          </button>{" "}
          or pick another agent
        </p>
      )}

      <div data-tour="schedule-task">
        <TaskField
          scope="tpl"
          hint="what each session from it starts on"
          placeholder="e.g. update dependencies, run the tests and fix what breaks"
          prompt={form.prompt}
          setPrompt={setPrompt}
        />
      </div>

      <div className="tpl-rows">
        {/* Where it works: the New form's own row. Provisioning is a box on
            the worktree answer, not a third answer. */}
        <WorksInGroup
          scope="tpl"
          worktree={form.mode !== "in_place"}
          onWorktree={() => onForm({ mode: "worktree" })}
          // Autopilot commits for its own session, so it never drives a
          // shared checkout — the same rule as the New form's fast-track.
          onInPlace={() => onForm({ mode: "in_place", autopilot: "" })}
          inPlaceDesc={
            form.autopilot
              ? "Can't commit or open a PR for you — needs its own worktree"
              : IN_PLACE_HINT
          }
          underWorktree={
            form.mode !== "in_place" && (
              <div className="nf-opt-sub tpl-provision">
                <ProvisionCheck
                  stacked
                  id="tpl-provision"
                  checked={form.mode === "provisioned"}
                  onChange={(on) => onForm({ mode: on ? "provisioned" : "worktree" })}
                />
                {form.mode === "provisioned" && (
                  <StrategyPicker
                    id="tpl-strategy"
                    value={form.workspace_strategy}
                    onChange={(v) => onForm({ workspace_strategy: v })}
                  />
                )}
              </div>
            )
          }
          after={
            // Not offered for a folder that already IS a repo (as in New).
            (!isGit || form.init_repo) && (
              <InitRepoCheck
                stacked
                id="tpl-init"
                checked={form.init_repo}
                onChange={(on) => onForm({ init_repo: on })}
              />
            )
          }
        />

        <div className="rt-row rt-row-top" id="tpl-schedule-row" data-tour="schedule-when">
          <span className="rt-label" id="tpl-schedule-label">
            Schedule
          </span>
          <div className="rt-ctl tpl-schedule" role="group" aria-labelledby="tpl-schedule-label">
            <label className="check nf-opt nf-opt-check">
              <input
                type="checkbox"
                id="tpl-scheduled"
                checked={form.scheduled}
                onChange={(e) => onForm({ scheduled: e.target.checked })}
              />
              <ModeText title="Run on a schedule" desc="Starts a new session from it, by itself" />
            </label>
            {form.scheduled && (
              <ScheduleWhen
                form={form}
                onForm={onForm}
                tz={p.tz}
                cronPreview={p.cronPreview}
                runs={p.runs}
                focusTime={p.scheduleFocus}
              />
            )}
          </div>
        </div>

        <div className="rt-row rt-row-top" id="tpl-autopilot-row">
          <span className="rt-label" id="tpl-autopilot-label">
            Fast-track
          </span>
          <div className="rt-ctl tpl-fast-track">
            <span className="muted tpl-autopilot-hint">
              When the agent stops. Applies to Run now and to scheduled runs.
            </span>
            <select
              id="tpl-autopilot"
              aria-labelledby="tpl-autopilot-label"
              value={form.autopilot}
              onChange={(e) => {
                const autopilot = e.target.value as TemplateForm["autopilot"];
                // Fast-track commits for this session, so it needs its own worktree.
                onForm(autopilot && form.mode === "in_place" ? { autopilot, mode: "worktree" } : { autopilot });
              }}
            >
              {AUTOPILOT_CHOICES.map(([v, label]) => (
                <option key={v || "leave"} value={v}>
                  {label}
                </option>
              ))}
            </select>
            {needsGitHub && (
              <p className="tpl-gh-needed error" role="note">
                Needs GitHub —{" "}
                <button type="button" className="linklike" onClick={() => openConnectGitHub()}>
                  Connect GitHub
                </button>
              </p>
            )}
          </div>
        </div>
      </div>

      <div className="tpl-row-actions tpl-sticky">
        {/* A missing agent is already said, with its Install link, under
            Agent: the footer doesn't repeat it. */}
        {p.error && !(missing && p.errorReason === "agent_missing") && (
          <p className="error" aria-live="polite">
            {p.error}
            {p.errorReason === "github_required" && (
              <>
                {" "}
                <button type="button" className="linklike" onClick={() => openConnectGitHub()}>
                  Connect GitHub
                </button>
              </>
            )}
          </p>
        )}
        <button type="button" className="tpl-cancel" onClick={p.onCancel}>
          Cancel
        </button>
        <button type="submit" className="tpl-save" disabled={p.busy}>
          {p.busy ? "Saving…" : "Save"}
        </button>
      </div>
    </form>
  );
}
