/** Flights — one route from waiting to landed (docs/flights.md).
 *
 * The words are birds', never planes': a FLYWAY is the route a flock follows
 * (the wire calls it a plan: `plans`, `plan_id`, `FlightPlan`); a FLIGHT is one
 * run of a flyway bound to one session; a STOP is one box on the flyway (the
 * wire's step) — a robot stop the server carries out, or a PERCH (a `you`
 * step) where the flight waits until somebody presses Done. The types below mirror
 * the contract's data model field for field (the backend serves exactly these
 * shapes) and the helpers are pure, so the Board, the pane strip, the rail and
 * the top-bar badge read one rule each and are pinned by tests without a DOM. */

import { relTime } from "./format";

// --- The contract's shapes ---------------------------------------------------

export type StepActor = "robot" | "you";
export type FlightState = "running" | "waiting_you" | "paused" | "halted" | "done" | "cancelled";
export type StepState = "pending" | "running" | "waiting_you" | "done" | "skipped" | "failed";
export type OnFail = "halt" | "agent" | "skip" | "you";
export type TriggerKind = "manual" | "ticket" | "pr" | "issue" | "schedule";
export type SubjectKind = "ticket" | "pr" | "issue" | "schedule" | "manual";

/** `template_schedule.normalize_schedule`'s shape. */
export type FlightSchedule =
  | { at: string; days: "daily" | "weekdays" }
  | { cron: string }
  | { once: string };

export interface FlightStep {
  id: string;
  kind: string;
  title: string;
  config: Record<string, unknown>;
  on_fail: OnFail;
}

export interface FlightTrigger {
  kind: TriggerKind;
  schedule: FlightSchedule | null;
  paused: boolean;
}

export interface FlightPlan {
  id: string;
  name: string;
  description: string;
  builtin: boolean;
  trigger: FlightTrigger;
  session: { repo_path: string; program: string; model: string; in_place: boolean };
  steps: FlightStep[];
  updated: number;
}

export interface StepRunState {
  state: StepState;
  since: number;
  note: string;
  detail: Record<string, unknown>;
  rounds: number;
}

export interface FlightSubject {
  kind: SubjectKind;
  source: string;
  id: string;
  title: string;
  url: string;
}

export interface FlightHistoryEntry {
  at: number;
  step: string;
  event: "started" | "done" | "failed" | "skipped" | "you" | "halted" | "resumed" | "jumped";
  note: string;
}

export interface Flight {
  id: string;
  plan_id: string;
  plan_name: string;
  steps: FlightStep[];
  subject: FlightSubject;
  session: string;
  repo_root: string;
  branch: string;
  state: FlightState;
  cursor: number;
  step_states: Record<string, StepRunState>;
  reason: string;
  history: FlightHistoryEntry[];
  started: number;
  updated: number;
  finished: number;
  by: string;
  boot?: string;
  owner?: string;
  owner_at?: number;
}

export type FieldType = "text" | "textarea" | "int" | "bool" | "select";

export interface StepKindField {
  name: string;
  type: FieldType;
  label: string;
  /** select only. The contract leaves the item shape open: a plain value, or
   * a value with its own label — `fieldOptions` reads both. */
  options?: Array<string | { value: string; label?: string }>;
  default?: unknown;
  placeholder?: string;
}

/** One entry of `GET /api/flights/kinds` — what the stop palette and the stop
 * forms are generated from. */
export interface StepKindDef {
  kind: string;
  label: string;
  actor: StepActor;
  summary: string;
  fields: StepKindField[];
  default_title: string;
}

/** The session row's `flight` field (`/api/instances`): all the pane strip
 * and the rail read. */
export interface FlightRow {
  id: string;
  plan_name: string;
  cursor: number;
  total: number;
  state: FlightState;
  step: { id: string; kind: string; title: string; state: StepState; actor: StepActor };
}

// --- Reading a flight --------------------------------------------------------

/** The flight is still in the air (the Board's *In flight* / *Needs you*). */
export function isLive(f: Pick<Flight, "state">): boolean {
  return f.state === "running" || f.state === "waiting_you" || f.state === "paused" || f.state === "halted";
}

/** The flight drives its session's ship ladder right now: running, parked on
 * you, or paused. The pane hides its guided next-step button and its ⏩
 * fast-track toggle meanwhile (a toggle mid-flight would fight the flight);
 * once it is halted, done or cancelled both come back. */
export function flightDrives(f: Pick<Flight, "state"> | null | undefined): boolean {
  return !!f && (f.state === "running" || f.state === "waiting_you" || f.state === "paused");
}

/** "Perch: <stop>" — once: a title that already says "Perch:" (or the old
 * "You:") is read without it, so it never doubles. */
export function perchLabel(title: string | null | undefined): string {
  const t = String(title || "")
    .trim()
    .replace(/^(you|perch):\s*/i, "");
  return t ? "Perch: " + t : "Perch";
}

/** The bell's words for a flight perched on you: "Perched on you: <stop>". */
export function perchedOnYou(stepTitle: string | null | undefined): string {
  const t = String(stepTitle || "")
    .trim()
    .replace(/^(you|perch):\s*/i, "");
  return "Perched on you" + (t ? ": " + t : "");
}

/** A step's chip glyph: done ✓ · current ● · pending ○ · failed ✗ · skipped –. */
export function stepGlyph(state: StepState): string {
  switch (state) {
    case "done":
      return "✓";
    case "running":
    case "waiting_you":
      return "●";
    case "failed":
      return "✗";
    case "skipped":
      return "–";
    default:
      return "○";
  }
}

/** A step's state in a flight. A step the driver has no record of reads from
 * the cursor: behind it is done, at it is running, after it pending. */
export function stepStateOf(f: Pick<Flight, "cursor" | "step_states" | "state">, step: FlightStep, index: number): StepState {
  const rec = f.step_states?.[step.id]?.state;
  if (rec) return rec;
  if (index < f.cursor) return "done";
  if (index === f.cursor && isLive(f)) return f.state === "waiting_you" ? "waiting_you" : f.state === "paused" ? "pending" : "running";
  return "pending";
}

export interface FlightProgress {
  /** Steps finished (done or skipped). */
  done: number;
  total: number;
  /** The step at the cursor, null once the flight has run past its end. */
  current: FlightStep | null;
  /** Its run record (null when the driver has none yet). */
  currentState: StepRunState | null;
  /** Each step's state, in plan order. */
  states: StepState[];
}

export function flightProgress(f: Flight): FlightProgress {
  const states = f.steps.map((s, i) => stepStateOf(f, s, i));
  const current = f.steps[f.cursor] || null;
  return {
    done: states.filter((s) => s === "done" || s === "skipped").length,
    total: f.steps.length,
    current,
    currentState: current ? f.step_states?.[current.id] || null : null,
    states,
  };
}

/** Who acts on a step: the registry's word when we have it, else the kind's
 * own (a `you` stop — a perch — is the only *you* kind; `verify` is a robot that can
 * park on you, which its state says). */
export function stepActor(step: Pick<FlightStep, "kind">, kinds?: readonly StepKindDef[] | null): StepActor {
  const def = kinds?.find((k) => k.kind === step.kind);
  if (def) return def.actor;
  return step.kind === "you" ? "you" : "robot";
}

/** Flights that wait on a person: parked on a *you* step (or a verify step's
 * human items), or halted until someone retries, skips or cancels. A paused
 * flight waits on nobody — whoever paused it resumes it — so it stays under
 * *In flight* and out of the badge. */
export function needsYouFlights(flights: readonly Flight[]): Flight[] {
  return flights.filter((f) => f.state === "waiting_you" || f.state === "halted");
}

/** How long the current step has been current — "4m", "2h" (the app's one
 * relative-time formatter, bare) — or "" when unknown. */
export function currentElapsed(f: Flight, now = Date.now()): string {
  const cur = f.steps[f.cursor];
  const since = cur ? Number(f.step_states?.[cur.id]?.since) || 0 : 0;
  return since > 0 ? relTime(since, { now: now / 1000, suffix: "" }) : "";
}

/** The dots of a strip: one per step, filled for done/current, ✗ for failed,
 * – skipped, hollow pending. From a row (no per-step records) the cursor
 * decides. */
export function stripDots(f: Flight | FlightRow): string {
  if ("steps" in f) {
    return flightProgress(f)
      .states.map((s) => (s === "failed" ? "✗" : s === "skipped" ? "–" : s === "pending" ? "○" : "●"))
      .join("");
  }
  const total = Math.max(0, Number(f.total) || 0);
  let out = "";
  for (let i = 0; i < total; i++) out += i <= f.cursor ? (i === f.cursor && f.step?.state === "failed" ? "✗" : "●") : "○";
  return out;
}

/** The pane strip's line: `Ticket to merge · ●●●○○○ · Reviews settle (4m)`
 * (the strip draws the bird glyph before it). A full flight carries the
 * step's `since`; a session row does not, so its line has no elapsed. */
export function stripLabel(f: Flight | FlightRow, now = Date.now()): string {
  const title = "steps" in f ? f.steps[f.cursor]?.title || "" : f.step?.title || "";
  const elapsed = "steps" in f ? currentElapsed(f, now) : "";
  const tail =
    f.state === "done"
      ? "Landed"
      : f.state === "cancelled"
        ? "Cancelled"
        : (f.state === "paused" ? "Paused · " : "") + title + (elapsed ? " (" + elapsed + ")" : "");
  return [f.plan_name, stripDots(f), tail].filter((x) => x.trim()).join(" · ");
}

/** The row's flight is parked on a *you* step: what the rail, the bell and the
 * pane strip's Done / Skip key off. */
export function rowWaitsOnYou(row: FlightRow | null | undefined): boolean {
  return !!row && row.state === "waiting_you" && row.step?.actor === "you";
}

/** The checklist items of a perch (`config.items`): a list, or the editor's
 * text with one item per line (the server stores a list either way). */
export function youItems(step: Pick<FlightStep, "config"> | null | undefined): string[] {
  const raw = step?.config?.items;
  const list = Array.isArray(raw) ? raw : typeof raw === "string" ? raw.split("\n") : [];
  return list.map((x) => String(x).trim()).filter(Boolean);
}

/** What a *you* step asks (`config.text`), or the parked step's note. */
export function youText(f: Flight): string {
  const cur = f.steps[f.cursor];
  const text = String(cur?.config?.text ?? "").trim();
  return text || (cur ? f.step_states?.[cur.id]?.note || "" : "") || f.reason || "";
}

// --- The badge ---------------------------------------------------------------

export interface PerchBadge {
  /** The number on the top bar's Sky toggle (0 = no badge). */
  count: number;
  /** One of them halted: the badge turns red. */
  failing: boolean;
}

/** The Sky toggle's badge: flights perched on you (or halted until someone
 * retries, skips or cancels). A flight parked on a VERIFY step is one of
 * Verify's due checklists, which Verify's own badge counts, so it is left
 * out here rather than counted twice. */
export function perchBadge(flights: readonly Flight[]): PerchBadge {
  const perched = needsYouFlights(flights).filter((f) => f.state === "halted" || f.steps[f.cursor]?.kind !== "verify");
  return { count: perched.length, failing: perched.some((f) => f.state === "halted") };
}

// --- Waiting to fly ----------------------------------------------------------

export type WaitingKind = "tickets" | "prs" | "issues";

/** The Board's sub-heading and every row's badge for each kind — a row always
 * says whether it is a ticket, a pull request or an issue. */
export const WAITING_KIND_HEAD: Record<WaitingKind, string> = { tickets: "Tickets", prs: "Pull requests", issues: "Issues" };
export const WAITING_KIND_BADGE: Record<WaitingKind, string> = { tickets: "Ticket", prs: "PR", issues: "Issue" };

/** Whether an item of this kind starts by itself (the Board's chip and the
 * badge). Read from the one status call the Auto-start tab reads: tickets go
 * when the engine runs, PR comments and issues on their own switches (a switch
 * that runs on another of your devices still starts them). */
export function waitingAuto(
  kind: WaitingKind,
  status: { available?: boolean; running?: boolean; desired?: boolean; pr_enabled?: boolean; issues_enabled?: boolean } | null | undefined
): boolean {
  if (!status) return false;
  if (kind === "tickets") return !!status.available && !!(status.desired ?? status.running);
  if (kind === "prs") return status.pr_enabled === true;
  return status.issues_enabled === true;
}

/** The built-in flyway a kind's flights use when its source names none. The
 * contract has no issue built-in: an issue is worked like a ticket. */
export function builtinPlanFor(kind: WaitingKind | TriggerKind, plans?: readonly Pick<FlightPlan, "id">[] | null): string {
  const own =
    kind === "prs" || kind === "pr"
      ? "builtin:pr"
      : kind === "issues" || kind === "issue"
        ? "builtin:issue"
        : kind === "tickets" || kind === "ticket"
          ? "builtin:ticket"
          : "builtin:" + kind;
  // A kind's own built-in when the server has one (it may ship builtin:issue),
  // else the ticket plan; with no list at hand, the contract's four.
  if (plans) return plans.some((p) => p.id === own) ? own : "builtin:ticket";
  return own === "builtin:issue" ? "builtin:ticket" : own;
}

// --- The editor --------------------------------------------------------------

/** A select field's options as value/label pairs. */
export function fieldOptions(f: StepKindField): Array<{ value: string; label: string }> {
  return (f.options || []).map((o) =>
    typeof o === "string" ? { value: o, label: o } : { value: String(o.value), label: String(o.label || o.value) }
  );
}

/** A new stop of a kind: its default title and every field's default. The id
 * is local until saved (the server keeps or reassigns it). */
export function newStep(def: StepKindDef, taken: readonly string[]): FlightStep {
  const config: Record<string, unknown> = {};
  for (const f of def.fields) if (f.default !== undefined) config[f.name] = f.default;
  let n = taken.length + 1;
  while (taken.includes("s" + n)) n++;
  return { id: "s" + n, kind: def.kind, title: def.default_title || def.label, config, on_fail: "halt" };
}

/** Move a stop from one index to another (drag and ← →). */
export function moveStep<T>(steps: readonly T[], from: number, to: number): T[] {
  if (from === to || from < 0 || from >= steps.length) return steps.slice();
  const out = steps.slice();
  const [it] = out.splice(from, 1);
  out.splice(Math.max(0, Math.min(out.length, to)), 0, it);
  return out;
}

/** A copy of a stop, placed right after it, under a fresh id. */
export function duplicateStep(steps: readonly FlightStep[], index: number): FlightStep[] {
  const src = steps[index];
  if (!src) return steps.slice();
  const taken = steps.map((s) => s.id);
  let n = steps.length + 1;
  while (taken.includes("s" + n)) n++;
  const copy: FlightStep = { ...src, id: "s" + n, config: { ...src.config } };
  const out = steps.slice();
  out.splice(index + 1, 0, copy);
  return out;
}

/** Flyways compare by what Save would send (the server's `updated` aside). */
export function planDirty(a: FlightPlan | null, b: FlightPlan | null): boolean {
  if (!a || !b) return a !== b;
  const strip = (p: FlightPlan) => JSON.stringify({ ...p, updated: 0 });
  return strip(a) !== strip(b);
}

/** A blank manual flyway for "+ New flyway". */
export function blankPlan(): FlightPlan {
  return {
    id: "",
    name: "New flyway",
    description: "",
    builtin: false,
    trigger: { kind: "manual", schedule: null, paused: false },
    session: { repo_path: "", program: "", model: "", in_place: false },
    steps: [],
    updated: 0,
  };
}

/** A flyway from a line's stops ("Save as flyway" on a live line, a card
 * seeded from one): only what a stop IS — kind, title, config, on_fail —
 * never what a run did to it (step_states live beside the steps; a config key
 * with a leading `_` is the driver's, not the stop's), and re-numbered s1..sN
 * so the server sees a fresh flyway. A manual trigger unless one is given. */
export function planFromSteps(
  name: string,
  steps: readonly FlightStep[],
  trigger?: FlightPlan["trigger"]
): Omit<FlightPlan, "id" | "updated"> {
  return {
    name: name.trim() || "New flyway",
    description: "",
    builtin: false,
    trigger: trigger ? { ...trigger } : { kind: "manual", schedule: null, paused: false },
    session: { repo_path: "", program: "", model: "", in_place: false },
    steps: steps.map((s, i) => {
      const config: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(s.config || {})) {
        if (k.startsWith("_") || v === undefined) continue;
        config[k] = Array.isArray(v) ? v.slice() : v && typeof v === "object" ? JSON.parse(JSON.stringify(v)) : v;
      }
      return { id: "s" + (i + 1), kind: s.kind, title: s.title, config, on_fail: s.on_fail || "halt" };
    }),
  };
}

/** The trigger chips' words. */
export const TRIGGER_LABEL: Record<TriggerKind, string> = {
  manual: "By hand",
  ticket: "Ticket",
  pr: "Pull request",
  issue: "Issue",
  schedule: "Schedule",
};

/** The `on_fail` chips' words. */
export const ON_FAIL_LABEL: Record<OnFail, string> = {
  halt: "Stop and tell me",
  agent: "Hand it to the agent",
  skip: "Skip it",
  you: "Ask me",
};

// --- The old Flights addresses ---------------------------------------------

/** Where an old Flights address lands now that the dialog is retired: the
 * Sky (on a session: `focus`), the Sky with the flyway shelf open (and one
 * flyway's card: `plan`), or one of the flat dialogs with its own deep link. */
export type LegacyFlightsRoute =
  | { to: "sky"; focus: string }
  | { to: "flyways"; plan: string }
  | { to: "intake" | "verify" | "schedule"; target: string | null };

/** A `#flights[/<tab>[/<rest>]]` target (or an `openDialogFor("flights", …)`
 * from an old caller), routed:
 *
 *   ""  sky[/<title>]  board        → the Sky (on that session)
 *   flyways[/<id>]  plans[/<id>]    → the Sky, shelf open (that flyway's card)
 *   sources[/<x>]                   → Intake (x = its tab / source)
 *   checklists[/<branch>]           → Verify (that checklist)
 *   schedule[/<x>]                  → Schedule (x = its own deep link)
 *
 * Anything else is the Sky. */
export function legacyFlightsRoute(target: string | null | undefined): LegacyFlightsRoute {
  const raw = String(target || "").trim();
  const cut = raw.indexOf("/");
  const word = cut < 0 ? raw : raw.slice(0, cut);
  const rest = cut < 0 ? "" : raw.slice(cut + 1);
  if (word === "flyways" || word === "plans") return { to: "flyways", plan: rest };
  if (word === "sources") return { to: "intake", target: rest || null };
  if (word === "checklists") return { to: "verify", target: rest || null };
  if (word === "schedule") return { to: "schedule", target: rest || null };
  return { to: "sky", focus: word === "sky" ? rest : "" };
}

// --- The trigger's schedule, as Schedule's own editor reads it ----------------

/** The days / time / date / cron fields `ScheduleWhen` edits. */
export interface WhenForm {
  days: "daily" | "weekdays" | "once" | "cron";
  at: string;
  date: string;
  cron: string;
  seeded: string;
}

export function whenFormOf(s: FlightSchedule | null | undefined): WhenForm {
  const base: WhenForm = { days: "daily", at: "21:00", date: "", cron: "0 21 * * 1-5", seeded: "" };
  if (!s) return base;
  if ("once" in s && s.once) return { ...base, days: "once", at: s.once.slice(11, 16), date: s.once.slice(0, 10) };
  if ("cron" in s && s.cron) return { ...base, days: "cron", cron: s.cron };
  if ("at" in s && s.at) return { ...base, days: s.days === "weekdays" ? "weekdays" : "daily", at: s.at };
  return base;
}

/** The schedule a form means (null when it doesn't name one yet). */
export function scheduleOfWhen(f: WhenForm): FlightSchedule | null {
  if (f.days === "cron") return f.cron.trim() ? { cron: f.cron.trim() } : null;
  if (f.days === "once") return f.date && f.at ? { once: f.date + "T" + f.at } : null;
  return f.at ? { at: f.at, days: f.days } : null;
}

// --- A source's flyway chips ---------------------------------------------------

/** The chips of a source's flyway choice (and a waiting item's): the empty
 * choice first — "Default · <the kind's built-in>", which the server cuts at
 * the source's Fast-track depth — then every flyway, built-ins first. A stored
 * flyway the list no longer has is kept as its own chip, so a save never drops
 * it silently. */
export function planChoices(
  plans: readonly Pick<FlightPlan, "id" | "name" | "builtin">[],
  kind: WaitingKind,
  value = ""
): Array<{ value: string; label: string; builtin?: boolean }> {
  const builtin = plans.find((p) => p.id === builtinPlanFor(kind, plans));
  const ordered = [...plans.filter((p) => p.builtin), ...plans.filter((p) => !p.builtin)];
  const out: Array<{ value: string; label: string; builtin?: boolean }> = [
    { value: "", label: "Default · " + (builtin?.name || "built-in flyway") },
  ];
  for (const p of ordered) out.push({ value: p.id, label: p.name, builtin: p.builtin });
  if (value && !plans.some((p) => p.id === value)) out.push({ value, label: value + " (not found)" });
  return out;
}

/** The per-source setting's key: `TicketingSource.plan`,
 * `github.repo_settings[repo].plan`, `github.issue_repo_settings[repo].plan`. */
export const PLAN_FIELD = "plan";
