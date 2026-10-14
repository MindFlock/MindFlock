/** The ONE Templates list: server-side (`/api/templates`, synced between
 * devices as settings sync's `store:templates` unit), read through a small
 * in-memory cache with a localStorage mirror so the surfaces that need a
 * synchronous answer (the sidebar Prompts bar, the New dialog's "Saved
 * prompt…" list — see lib/presets.ts) paint at once and repaint on
 * TEMPLATES_CHANGED when the fetch lands.
 *
 * A template is a saved New-session setup; one that carries only a prompt is
 * what used to be a per-browser "saved prompt". Those are migrated here once
 * per browser (migratePresetsOnce), so the two lists that overlapped are one.
 */

import { api } from "../api/client";
import { presetKey, writeLocal } from "./prefs";

export interface Template {
  name: string;
  program?: string;
  repo_path?: string;
  prompt?: string;
  provisioned?: boolean;
  workspace_strategy?: string;
  in_place?: boolean;
  init_repo?: boolean;
  /** Start a fresh session from this template on its own: daily (or
   * weekdays) at a local "HH:MM", or on a cron expression. Absent = only by
   * hand. */
  schedule?: TemplateSchedule;
  /** How far each run (Run now or scheduled) carries its work on its own
   * once its turn ends — the ⏩ autopilot. Absent = it stops where the
   * agent leaves it. */
  autopilot?: TemplateAutopilot;
}

/** Either `{at, days}` or `{cron}` (5-field, local time), plus two
 * switches the Schedule surface flips per row: `paused` (keeps the cadence,
 * starts nothing) and `skip_until` (epoch seconds — every slot at or before
 * it passes without a session: "Skip next"). Both are stored only when set. */
export interface TemplateSchedule {
  at?: string;
  days?: "daily" | "weekdays";
  cron?: string;
  paused?: boolean;
  skip_until?: number;
}

export type TemplateAutopilot = "commit" | "push" | "pr";

/** The autopilot choices, in ladder order, with the editor's words. */
export const AUTOPILOT_CHOICES: Array<[TemplateAutopilot | "", string]> = [
  ["", "Leave it for me"],
  ["commit", "Commit"],
  ["push", "Commit + push"],
  ["pr", "Open a PR"],
];

/** One run in a template's history (newest first): started (`session`),
 * failed (`error`) or skipped (`skipped` — the previous run hadn't
 * finished). */
export interface TemplateRunEntry {
  at: number;
  trigger: string;
  session?: string;
  error?: string;
  skipped?: string;
  autopilot?: string;
}

/** This device's record of a template's runs (GET /api/templates `runs`,
 * keyed by lowercased name): epochs in seconds. */
export interface TemplateRunInfo {
  next_at?: number | null;
  last_at?: number | null;
  last_session?: string;
  last_trigger?: string;
  last_error?: string;
  /** The server's words for the schedule ("Every 30 minutes"). */
  schedule_label?: string;
  /** A scheduled run skipped since the last start: why. */
  last_skip?: string;
  last_skip_at?: number;
  history?: TemplateRunEntry[];
  /** The schedule is paused (no `next_at`). */
  paused?: boolean;
  /** "Skip next" is set: the slot that will pass without a session
   * (`next_at` is then the one after it). */
  skipping_at?: number;
}

export interface TemplateRunsStatus {
  runs: Record<string, TemplateRunInfo>;
  /** Schedules fire on this device (the group's automation device). */
  runs_here: boolean;
  /** Else the device that fires them. */
  runs_on: string;
}

/** Fired on document after every change to the list, cache or server. The
 * same event name lib/presets.ts always fired, so every listener that
 * repainted on a saved prompt keeps working unchanged. */
export const TEMPLATES_CHANGED = "mf-presets-changed";

/** The localStorage mirror of the last fetched list — an instant first paint
 * on the next page load, never the source of truth. */
export const TEMPLATES_MIRROR_KEY = "mindflock.templates_cache";
/** The legacy per-browser saved-prompt list (lib/presets.ts before the
 * server held the list) — the migration's source. */
export const OLD_PRESETS_STORAGE_KEY = "mindflock.prompt_presets";
/** Set once the legacy list has been pushed to the server, so the migration
 * runs once per browser and never re-creates a template that was deleted. */
export const PRESETS_MIGRATED_KEY = "mf_presets_migrated";

type KV = { getItem(k: string): string | null; setItem(k: string, v: string): void };

function storage(): KV | null {
  try {
    return typeof localStorage !== "undefined" ? localStorage : null;
  } catch {
    return null;
  }
}

function cleanList(raw: unknown): Template[] {
  return Array.isArray(raw)
    ? raw.filter((t) => t && typeof t.name === "string" && t.name.trim()).map((t) => ({ ...t }))
    : [];
}

let cache: Template[] | null = null;
let loading: Promise<Template[]> | null = null;
let runsStatus: TemplateRunsStatus = { runs: {}, runs_here: true, runs_on: "" };

/** Next/last run per template, as of the last fetch. */
export function templateRunsNow(): TemplateRunsStatus {
  return runsStatus;
}

export function runInfoFor(name: string): TemplateRunInfo {
  return runsStatus.runs[(name || "").trim().toLowerCase()] || {};
}

function notify(): void {
  if (typeof document !== "undefined") document.dispatchEvent(new Event(TEMPLATES_CHANGED));
}

/** Replace the cache (what a POST answers with is the whole list). */
export function setTemplates(list: Template[], kv: KV | null = storage()): Template[] {
  cache = cleanList(list);
  try {
    kv?.setItem(TEMPLATES_MIRROR_KEY, JSON.stringify(cache));
  } catch {
    /* storage unavailable */
  }
  notify();
  return cache;
}

/** The list as this page knows it right now: the fetched list, else the
 * mirror from last time, else nothing. Never waits. */
export function templatesNow(kv: KV | null = storage()): Template[] {
  if (cache) return cache;
  try {
    return cleanList(JSON.parse(kv?.getItem(TEMPLATES_MIRROR_KEY) || "[]"));
  } catch {
    return [];
  }
}

/** Whether a fetch has landed on this page (vs. the mirror / nothing). */
export function templatesLoaded(): boolean {
  return cache !== null;
}

/** Drop what this page knows, so the next read fetches again (tests). */
export function resetTemplatesCache(): void {
  cache = null;
  loading = null;
}

/** Fetch the server's list once per page (callers share one in-flight
 * request); re-fetch with `force`. Runs the one-time saved-prompt migration
 * first, so a browser's old local list is on the server before anything
 * reads the answer. */
export function ensureTemplates(force = false): Promise<Template[]> {
  if (cache && !force) return Promise.resolve(cache);
  if (loading) return loading;
  loading = (async () => {
    try {
      const d = await api<{ templates?: Template[] } & Partial<TemplateRunsStatus>>("/api/templates");
      runsStatus = {
        runs: (d.runs && typeof d.runs === "object" ? d.runs : {}) as Record<string, TemplateRunInfo>,
        runs_here: d.runs_here !== false,
        runs_on: String(d.runs_on || ""),
      };
      let list = setTemplates(d.templates || []);
      if (await migratePresetsOnce(list)) {
        const again = await api<{ templates?: Template[] }>("/api/templates");
        list = setTemplates(again.templates || []);
      }
      return list;
    } finally {
      loading = null;
    }
  })();
  return loading;
}

/** Save (upsert by name, ignoring case — the server's rule). Answers the
 * server's whole list, which becomes the cache. */
export async function saveTemplate(t: Template): Promise<Template[]> {
  const d = await api<{ templates?: Template[] }>("/api/templates", { json: t });
  return setTemplates(d.templates || []);
}

export async function deleteTemplate(name: string): Promise<boolean> {
  const d = await api<{ deleted?: boolean }>("/api/templates/" + encodeURIComponent(name), {
    method: "DELETE",
  });
  setTemplates(templatesNow().filter((t) => presetKey(t.name) !== presetKey(name)));
  return !!d.deleted;
}

/** Why a template can't start a session on its own ("" = it can): a plain
 * session with no folder would run in the server's own directory. The
 * server's rule (template_schedule.run_problem), mirrored so a button that
 * can't work is never offered. */
export function runProblem(t: Template): string {
  return !(t.repo_path || "").trim() && !t.provisioned ? "Pick a folder for this template first" : "";
}

/** Who opened the Schedule surface (the Templates dialog that was) and for what, as the store's
 * dialogTarget carries it: "new" (from New — Close hands back to it),
 * "schedule" (the palette's "Schedule a session…": a new template, schedule
 * on), and "new|schedule:<name>" (a template chip's Schedule… in New). */
export interface TemplatesTarget {
  fromNew: boolean;
  /** null = just the list; "" = a NEW template's editor, schedule on; a
   * name = that template's editor, schedule on. */
  schedule: string | null;
}

export function templatesTarget(t: TemplatesTarget): string {
  const sched = t.schedule === null ? "" : t.schedule ? "schedule:" + t.schedule : "schedule";
  if (t.fromNew) return sched ? "new|" + sched : "new";
  return sched;
}

export function readTemplatesTarget(target: string | null | undefined): TemplatesTarget {
  let rest = String(target || "");
  let fromNew = false;
  if (rest === "new") return { fromNew: true, schedule: null };
  if (rest.startsWith("new|")) {
    fromNew = true;
    rest = rest.slice(4);
  }
  if (rest === "schedule") return { fromNew, schedule: "" };
  if (rest.startsWith("schedule:")) return { fromNew, schedule: rest.slice(9) };
  return { fromNew, schedule: null };
}

/** Whether a template starts itself. */
export function hasSchedule(t: Template): boolean {
  return !!(t.schedule && (t.schedule.at || t.schedule.cron));
}

/** A scheduled template's run row (the server's, keyed by lowercased name). */
function infoOf(t: Template, status: Pick<TemplateRunsStatus, "runs">): TemplateRunInfo {
  return status.runs[(t.name || "").trim().toLowerCase()] || {};
}

/** The scheduled templates whose LAST run failed — what the top bar's
 * Schedule badge counts, and exactly the cards that say "Last run failed"
 * (a later good run clears it on the server). A paused one is left out: it
 * isn't going to try again until you resume it, which is a choice, not news. */
export function failingSchedules(templates: Template[], status: Pick<TemplateRunsStatus, "runs">): string[] {
  return templates
    .filter((t) => hasSchedule(t) && !t.schedule?.paused && !!infoOf(t, status).last_error)
    .map((t) => t.name);
}

/** What runs next across every schedule: the earliest `next_at` of a
 * template that isn't paused, or null. */
export function nextUp(
  templates: Template[],
  status: Pick<TemplateRunsStatus, "runs">
): { name: string; at: number } | null {
  let best: { name: string; at: number } | null = null;
  for (const t of templates) {
    if (!hasSchedule(t) || t.schedule?.paused) continue;
    const at = Number(infoOf(t, status).next_at) || 0;
    if (at && (!best || at < best.at)) best = { name: t.name, at };
  }
  return best;
}

/** How far off a time is, in the fewest words: "in 5 min", "in 2h",
 * "in 2h 30m" (under 10 hours), "in 3 days". `now` is injectable for tests. */
export function inLabel(epochSec: number, now: Date = new Date()): string {
  const mins = Math.round((epochSec * 1000 - now.getTime()) / 60000);
  if (mins <= 0) return "now";
  if (mins < 60) return "in " + mins + " min";
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (h < 10) return "in " + h + "h" + (m ? " " + m + "m" : "");
  if (h < 36) return "in " + h + "h";
  return "in " + Math.round(h / 24) + " days";
}

/** The template with its schedule's switches changed — what Pause / Resume /
 * Skip next / Undo skip POST (the whole template: the server upserts by
 * name). A `skip_until` of 0 (or a past one) is dropped. */
export function withScheduleFlags(
  t: Template,
  patch: { paused?: boolean; skip_until?: number },
  nowSec: number = Date.now() / 1000
): Template {
  const sched: TemplateSchedule = { ...(t.schedule || {}), ...patch };
  if (!sched.paused) delete sched.paused;
  if (!sched.skip_until || sched.skip_until <= nowSec) delete sched.skip_until;
  return { ...t, schedule: sched };
}

/** "Every day at 21:00" / "Weekdays at 21:00" / "Cron 0 9 1 * mon" (the
 * server's `schedule_label` says common cron shapes in words). */
export function scheduleLabel(s: TemplateSchedule | undefined | null): string {
  if (!s) return "";
  if (s.cron) return "Cron " + s.cron;
  if (!s.at) return "";
  return (s.days === "weekdays" ? "Weekdays at " : "Every day at ") + s.at;
}

/** What a cron field means, from the server (the rules a save applies). */
export interface CronPreview {
  ok: boolean;
  label?: string;
  next?: number[];
  error?: string;
}

export async function cronPreview(expr: string): Promise<CronPreview> {
  return api<CronPreview>("/api/templates/cron-preview?expr=" + encodeURIComponent(expr));
}

/** A run time for a card: "today 21:00", "tomorrow 21:00", "Mon 21:00", or
 * a date further out. `now` is injectable for tests. */
export function whenLabel(epochSec: number | null | undefined, now: Date = new Date()): string {
  if (!epochSec) return "";
  const d = new Date(epochSec * 1000);
  const hm = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(d) - day(now)) / 86400000);
  if (diff === 0) return "today " + hm;
  if (diff === 1) return "tomorrow " + hm;
  if (diff === -1) return "yesterday " + hm;
  if (Math.abs(diff) < 7) return d.toLocaleDateString(undefined, { weekday: "short" }) + " " + hm;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + hm;
}

/** The templates that are a prompt — what the saved-prompt surfaces list. */
/** A template that holds ONLY a prompt — what a "saved prompt" is. A full
 * template (agent, folder, workspace mode) that happens to carry a prompt is
 * not one: removing a saved prompt must never delete somebody's setup. */
export function isPromptOnly(t: Template): boolean {
  return (
    typeof t.prompt === "string" &&
    !!t.prompt.trim() &&
    !t.program &&
    !t.repo_path &&
    !t.provisioned &&
    !t.in_place &&
    !t.init_repo &&
    // An autopilot or a schedule is session setup: that is a template, even
    // with no folder in it — offering it as a snippet to paste would drop both.
    !t.autopilot &&
    !t.schedule
  );
}

export function promptTemplates(list: Template[]): Array<{ name: string; prompt: string }> {
  return list.filter(isPromptOnly).map((t) => ({ name: t.name, prompt: t.prompt as string }));
}

/** What a browser's legacy saved prompts become on the server, against the
 * list already there. One saved prompt = one template holding only a
 * prompt. Deduped by name AND body: the same name with the same text is
 * already there (skipped); the same name with different text is kept under
 * "<name> (saved prompt)" rather than overwriting somebody's template or
 * losing the prompt — unless that spelling exists too. Pure, so the rule is
 * testable without a server. */
export function planPresetMigration(
  local: Array<{ name: string; prompt: string }>,
  server: Template[]
): { save: Template[]; skipped: string[] } {
  const byKey = new Map<string, Template>();
  for (const t of server) byKey.set(presetKey(t.name), t);
  const save: Template[] = [];
  const skipped: string[] = [];
  for (const p of local) {
    const name = (p.name || "").trim();
    const prompt = typeof p.prompt === "string" ? p.prompt : "";
    if (!name || !prompt.trim()) continue;
    const hit = byKey.get(presetKey(name));
    if (!hit) {
      save.push({ name, prompt });
      byKey.set(presetKey(name), { name, prompt });
      continue;
    }
    if ((hit.prompt || "") === prompt) {
      skipped.push(name);
      continue;
    }
    const alt = name + " (saved prompt)";
    if (byKey.has(presetKey(alt))) {
      skipped.push(name);
      continue;
    }
    save.push({ name: alt, prompt });
    byKey.set(presetKey(alt), { name: alt, prompt });
  }
  return { save, skipped };
}

/** Push this browser's legacy saved prompts to the server, once. Returns how
 * many templates were saved (0 when there was nothing, or it already ran).
 * The marker is set only after every save succeeded, so a failed push is
 * retried on the next load rather than lost. */
export async function migratePresetsOnce(
  server: Template[],
  kv: KV | null = storage(),
  post: (t: Template) => Promise<unknown> = (t) => api("/api/templates", { json: t })
): Promise<number> {
  if (!kv) return 0;
  let done = false;
  let local: Array<{ name: string; prompt: string }> = [];
  try {
    done = kv.getItem(PRESETS_MIGRATED_KEY) === "1";
    const raw = JSON.parse(kv.getItem(OLD_PRESETS_STORAGE_KEY) || "[]");
    local = Array.isArray(raw) ? raw : [];
  } catch {
    return 0;
  }
  if (done) return 0;
  const { save } = planPresetMigration(local, server);
  // One refused prompt (a browser without may_configure, a prompt over the
  // server's limits) must not fail every read of the Templates list: skip it
  // and try again next load; the marker lands only once nothing is left.
  let failed = 0;
  let saved = 0;
  for (const t of save) {
    try {
      await post(t);
      saved++;
    } catch {
      failed++;
    }
  }
  if (!failed) {
    try {
      kv.setItem(PRESETS_MIGRATED_KEY, "1");
      // The legacy list is retired: empty it here AND on the server (prefs
      // sync pushes the write), so a fresh browser's pull has nothing left to
      // migrate — otherwise prompts deleted since would come back.
      if (typeof (kv as { removeItem?: unknown }).removeItem === "function")
        writeLocal("prompt_presets", [], kv as unknown as Parameters<typeof writeLocal>[2]);
      else kv.setItem(OLD_PRESETS_STORAGE_KEY, "[]");
    } catch {
      /* storage unavailable */
    }
  }
  return saved;
}
