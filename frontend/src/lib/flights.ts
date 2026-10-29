/** Flights — one route from arrival to merge (docs/flights.md).
 *
 * A PLAN is a row of blocks (steps); a FLIGHT is one run of a plan bound to
 * one session; a STEP is a robot step the server carries out, or a *you* step
 * that parks the flight until somebody presses Done. The types below mirror
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

/** One entry of `GET /api/flights/kinds` — what the palette and the block
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

/** "You: <step>" — once: a title that already says "You:" is left as it is. */
export function youStepLabel(title: string | null | undefined): string {
  const t = String(title || "").trim();
  if (!t) return "You";
  return /^you:/i.test(t) ? t : "You: " + t;
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
 * own (a `you` block is the only *you* kind; `verify` is a robot that can
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

/** The pane strip's line: `✈ Ticket to merge · ●●●○○○ · Reviews settle (4m)`.
 * A full flight carries the step's `since`; a session row does not, so its
 * line has no elapsed. */
export function stripLabel(f: Flight | FlightRow, now = Date.now()): string {
  const title = "steps" in f ? f.steps[f.cursor]?.title || "" : f.step?.title || "";
  const elapsed = "steps" in f ? currentElapsed(f, now) : "";
  const tail =
    f.state === "done"
      ? "Landed"
      : f.state === "cancelled"
        ? "Cancelled"
        : (f.state === "paused" ? "Paused · " : "") + title + (elapsed ? " (" + elapsed + ")" : "");
  return ["✈ " + f.plan_name, stripDots(f), tail].filter((x) => x.trim()).join(" · ");
}

/** The row's flight is parked on a *you* step: what the rail, the bell and the
 * pane strip's Done / Skip key off. */
export function rowWaitsOnYou(row: FlightRow | null | undefined): boolean {
  return !!row && row.state === "waiting_you" && row.step?.actor === "you";
}

/** The checklist items of a *you* step (`config.items`). */
export function youItems(step: FlightStep | null | undefined): string[] {
  const raw = step?.config?.items;
  return Array.isArray(raw) ? raw.map((x) => String(x)).filter((x) => x.trim()) : [];
}

/** What a *you* step asks (`config.text`), or the parked step's note. */
export function youText(f: Flight): string {
  const cur = f.steps[f.cursor];
  const text = String(cur?.config?.text ?? "").trim();
  return text || (cur ? f.step_states?.[cur.id]?.note || "" : "") || f.reason || "";
}

// --- The badge ---------------------------------------------------------------

export interface FlightBadge {
  /** The number on the top-bar button (0 = no badge). */
  count: number;
  /** Flights waiting on you. */
  you: number;
  /** Verify's due checklists (failing ones included). */
  verify: number;
  /** Arrivals that will not start on their own. */
  arrivals: number;
  /** Any of it failed: the badge turns red. */
  failing: boolean;
}

/** What waits on you, the top bar's one number: flights parked on you, plus
 * Verify's due checklists, plus arrivals no switch will start. A flight parked
 * on a VERIFY step is already one of Verify's due checklists, so it is counted
 * there and not twice. */
export function flightBadgeCount(
  flights: readonly Flight[],
  dueVerify: number,
  failingVerify: number,
  arrivals: readonly { auto: boolean }[]
): FlightBadge {
  const you = needsYouFlights(flights).filter((f) => f.steps[f.cursor]?.kind !== "verify").length;
  const verify = Math.max(0, dueVerify || 0);
  const manual = arrivals.filter((a) => !a.auto).length;
  const halted = flights.some((f) => f.state === "halted");
  return {
    count: you + verify + manual,
    you,
    verify,
    arrivals: manual,
    failing: (failingVerify || 0) > 0 || halted,
  };
}

/** The tooltip's three lines under the button's name. */
export function badgeLines(b: FlightBadge): string[] {
  const n = (k: number, one: string, many: string) => k + " " + (k === 1 ? one : many);
  return [
    n(b.you, "flight waits on you", "flights wait on you"),
    n(b.verify, "checklist to check", "checklists to check"),
    n(b.arrivals, "arrival to start by hand", "arrivals to start by hand"),
  ];
}

// --- Arrivals ----------------------------------------------------------------

export type ArrivalKind = "tickets" | "prs" | "issues";

/** Whether an arrival of this kind starts by itself (the Board's chip and the
 * badge). Read from the one status call the Auto-start tab reads: tickets go
 * when the engine runs, PR comments and issues on their own switches (a switch
 * that runs on another of your devices still starts them). */
export function arrivalAuto(
  kind: ArrivalKind,
  status: { available?: boolean; running?: boolean; desired?: boolean; pr_enabled?: boolean; issues_enabled?: boolean } | null | undefined
): boolean {
  if (!status) return false;
  if (kind === "tickets") return !!status.available && !!(status.desired ?? status.running);
  if (kind === "prs") return status.pr_enabled === true;
  return status.issues_enabled === true;
}

/** The built-in plan a kind's flights use when its source names none. The
 * contract has no issue built-in: an issue is worked like a ticket. */
export function builtinPlanFor(kind: ArrivalKind | TriggerKind, plans?: readonly Pick<FlightPlan, "id">[] | null): string {
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

/** A new block of a kind: its default title and every field's default. The id
 * is local until saved (the server keeps or reassigns it). */
export function newStep(def: StepKindDef, taken: readonly string[]): FlightStep {
  const config: Record<string, unknown> = {};
  for (const f of def.fields) if (f.default !== undefined) config[f.name] = f.default;
  let n = taken.length + 1;
  while (taken.includes("s" + n)) n++;
  return { id: "s" + n, kind: def.kind, title: def.default_title || def.label, config, on_fail: "halt" };
}

/** Move a block from one index to another (drag and ← →). */
export function moveStep<T>(steps: readonly T[], from: number, to: number): T[] {
  if (from === to || from < 0 || from >= steps.length) return steps.slice();
  const out = steps.slice();
  const [it] = out.splice(from, 1);
  out.splice(Math.max(0, Math.min(out.length, to)), 0, it);
  return out;
}

/** A copy of a block, placed right after it, under a fresh id. */
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

/** Plans compare by what Save would send (the server's `updated` aside). */
export function planDirty(a: FlightPlan | null, b: FlightPlan | null): boolean {
  if (!a || !b) return a !== b;
  const strip = (p: FlightPlan) => JSON.stringify({ ...p, updated: 0 });
  return strip(a) !== strip(b);
}

/** A blank manual plan for "+ New plan". */
export function blankPlan(): FlightPlan {
  return {
    id: "",
    name: "New plan",
    description: "",
    builtin: false,
    trigger: { kind: "manual", schedule: null, paused: false },
    session: { repo_path: "", program: "", model: "", in_place: false },
    steps: [],
    updated: 0,
  };
}

/** The trigger picker's words. */
export const TRIGGER_LABEL: Record<TriggerKind, string> = {
  manual: "Manual",
  ticket: "Ticket",
  pr: "Pull request",
  issue: "Issue",
  schedule: "Schedule",
};

/** The `on_fail` picker's words. */
export const ON_FAIL_LABEL: Record<OnFail, string> = {
  halt: "Stop and tell me",
  agent: "Hand it to the agent",
  skip: "Skip it",
  you: "Ask me",
};

// --- The dialog's address ----------------------------------------------------

export type FlightsTab = "board" | "plans" | "sources" | "schedule" | "checklists";
export const FLIGHTS_TABS: readonly FlightsTab[] = ["board", "plans", "sources", "schedule", "checklists"];
export const FLIGHTS_TAB_LABEL: Record<FlightsTab, string> = {
  board: "Board",
  plans: "Plans",
  sources: "Sources",
  schedule: "Schedule",
  checklists: "Checklists",
};

/** A Flights dialog target: `<tab>` or `<tab>/<rest>` — rest is the embedded
 * surface's own deep link (an Intake tab, a branch for Verify, Schedule's
 * "new" / "schedule:<name>", a plan id). Unknown or empty = the Board. */
export function parseFlightsTarget(target: string | null | undefined): { tab: FlightsTab; rest: string } {
  const raw = String(target || "").trim();
  const cut = raw.indexOf("/");
  const head = (cut < 0 ? raw : raw.slice(0, cut)) as FlightsTab;
  const rest = cut < 0 ? "" : raw.slice(cut + 1);
  return FLIGHTS_TABS.includes(head) ? { tab: head, rest } : { tab: "board", rest: "" };
}

/** The legacy dialog names and the tab of Flights each one became, with its
 * old target carried in the new one: `intake` + "tickets:jira" →
 * "sources/tickets:jira"; `verify` + a branch → "checklists/<branch>";
 * `schedule` / `templates` + "new" → "schedule/new". Intake with no target is
 * the Board (Alt+I's old landing was the work that came in). */
export function flightsTargetFor(name: "intake" | "verify" | "schedule" | "templates", target: string | null | undefined): string {
  const t = String(target || "");
  if (name === "intake") return t ? "sources/" + t : "board";
  if (name === "verify") return t ? "checklists/" + t : "checklists";
  return t ? "schedule/" + t : "schedule";
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
