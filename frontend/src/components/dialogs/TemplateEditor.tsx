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
 * Top to bottom: Name; Folder beside Agent; the folder chips; Task; then the
 * label column the New form uses for Works in — Works in, Schedule, When a
 * run's turn ends — and Cancel / Save. */

import {
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
  hasSchedule,
  whenLabel,
  type CronPreview,
  type Template,
  type TemplateAutopilot,
  type TemplateRunsStatus,
} from "../../lib/templates";
import { useUi } from "../../state/store";
import { folderReducer, type FolderAction, type FolderState, type RepoSuggestion } from "./newSession";
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

/** The POST body for a form, or the reason it cannot be saved. */
export function templateBody(f: TemplateForm): { body: Template } | { error: string } {
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
export function cronPreviewLine(pv: CronPreview | null | undefined, now: Date = new Date()): string {
  if (!pv) return "";
  if (!pv.ok) return pv.error || "Not a cron expression";
  const next = (pv.next || []).map((t) => whenLabel(t, now)).join(", ");
  return (pv.label || "") + (next ? " — next " + next : "");
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
  const elsewhere = p.runs && !p.runs.runs_here && p.runs.runs_on;

  return (
    <form
      className="tpl-editor"
      onSubmit={(e) => {
        e.preventDefault();
        p.onSave();
      }}
    >
      <label className="tpl-name-field">
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
        <FolderField
          scope="tpl"
          placeholder="Needed to Run now or schedule"
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
        <AgentFields
          scope="tpl"
          defaultLabel="Your default agent"
          program={form.program}
          providers={(p.agents || []).map((name) => ({ name }))}
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
        suggestions={p.folderSuggestions || []}
        folderPath={folderPath}
        folderDo={folderDo}
        browserOpen={browsing}
        device=""
      />

      <TaskField
        scope="tpl"
        hint="what each session from it starts on"
        placeholder="e.g. update dependencies, run the tests and fix what breaks"
        prompt={form.prompt}
        setPrompt={setPrompt}
      />

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
              ? "Turns the autopilot off — it needs its own worktree"
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

        <div className="rt-row rt-row-top" id="tpl-schedule-row">
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
              <div className="tpl-schedule-when">
                <select
                  id="tpl-days"
                  aria-label="Which days"
                  value={form.days}
                  onChange={(e) =>
                    onForm({
                      days: e.target.value === "weekdays" ? "weekdays" : e.target.value === "cron" ? "cron" : "daily",
                    })
                  }
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
                    <input
                      id="tpl-at"
                      type="time"
                      aria-label="Time"
                      autoFocus={p.scheduleFocus === true}
                      value={form.at}
                      onChange={(e) => onForm({ at: e.target.value })}
                    />
                    {p.tz && <span className="muted tpl-tz">{p.tz} — this device's time</span>}
                  </>
                )}
              </div>
            )}
            {form.scheduled && form.days === "cron" && (
              <p
                className={"tpl-cron-preview" + (p.cronPreview && !p.cronPreview.ok ? " error" : " muted")}
                id="tpl-cron-preview"
                aria-live="polite"
              >
                {cronPreviewLine(p.cronPreview) || "minute hour day-of-month month weekday — local time"}
              </p>
            )}
            {form.scheduled && (
              <p className="tpl-cron-preview muted">
                {elsewhere ? "Starts on " + p.runs?.runs_on + " — the device that runs automation. " : ""}
                Skipped while the last run is still working or waiting on you.
              </p>
            )}
          </div>
        </div>

        <div className="rt-row" id="tpl-autopilot-row">
          <span className="rt-label" id="tpl-autopilot-label">
            When a run's turn ends
          </span>
          <div className="rt-ctl">
            <select
              id="tpl-autopilot"
              aria-labelledby="tpl-autopilot-label"
              value={form.autopilot}
              onChange={(e) => {
                const autopilot = e.target.value as TemplateForm["autopilot"];
                // Autopilot commits for this session, so it needs its own worktree.
                onForm(autopilot && form.mode === "in_place" ? { autopilot, mode: "worktree" } : { autopilot });
              }}
            >
              {AUTOPILOT_CHOICES.map(([v, label]) => (
                <option key={v || "leave"} value={v}>
                  {label}
                </option>
              ))}
            </select>
            <span className="muted tpl-autopilot-hint">each Run now and scheduled run</span>
          </div>
        </div>
      </div>

      <div className="tpl-row-actions">
        {p.error && (
          <p className="error" aria-live="polite">
            {p.error}
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
