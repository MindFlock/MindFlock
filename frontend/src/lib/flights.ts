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
import { capSourceDepth, sourceRungLabel, type Lane } from "./autopilot";

// --- The contract's shapes ---------------------------------------------------

export type StepActor = "robot" | "you";
export type FlightState = "grounded" | "ready" | "running" | "waiting_you" | "paused" | "halted" | "done" | "cancelled";

/** Round 19: how a chain built on a session takes off — on ▶ Run or the
 * session's next prompt, only on ▶ Run, or at once (never stored as ready). */
export type FlightStartOn = "prompt" | "run" | "now";
export type StepState = "pending" | "running" | "waiting_you" | "done" | "skipped" | "failed";
export type OnFail = "halt" | "agent" | "skip" | "you";
export type TriggerKind = "manual" | "ticket" | "pr" | "issue" | "schedule" | "main_push" | "main_red" | "hook";
/** The triggers that watch a repository's default branch (`trigger.repo`). */
export const MAIN_TRIGGERS: readonly TriggerKind[] = ["main_push", "main_red"];
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
  /** A halt point: the flight stops when it reaches this stop, before doing
   * it (docs/flights.md "Halt points"). The server leaves it out when false. */
  halt?: boolean;
  /** Round 11: consecutive steps sharing `group.id` are ONE stop on the board
   * (a compound box named `group.name`); the driver still flies them one by
   * one. `plan_id` is the saved stop they came from, "" for a group made by
   * hand. Omitted when the step is on its own. */
  group?: StepGroup;
  /** Round 15: a condition (`diff touches frontend/**`, `weekday and files
   * > 3` — docs/flights.md "`when`: a condition on every stop"); a pending
   * stop whose `when` is false is skipped, never run. Omitted when it has
   * none. */
  when?: string;
  /** Round 18: where the Sky draws this stop, chain-local plane px (relative
   * to the chain's origin); omitted = auto-placed. Layout only. */
  pos?: { x: number; y: number };
  /** Round 20: consecutive steps sharing `parallel.id` (`p_<hex>`) are ONE
   * parallel group — they fly side by side and the flight goes on once every
   * one is through (docs/flights-r20-parallel.md). Omitted on a step of its
   * own; only `parallel_safe` kinds carry it. */
  parallel?: StepParallel;
}

export interface StepParallel {
  id: string;
}

/** Round 20: the parallel group of step `i` as `[lo, hi)` (hi exclusive);
 * `[i, i + 1]` for a step of its own. Groups are RUNS of consecutive steps
 * sharing `parallel.id` — the server's rule (a run of one is no group). */
export function parallelSpan(steps: readonly Pick<FlightStep, "parallel">[], i: number): [number, number] {
  const pid = steps[i]?.parallel?.id || "";
  if (!pid) return [i, i + 1];
  let lo = i;
  let hi = i + 1;
  while (lo > 0 && steps[lo - 1]?.parallel?.id === pid) lo--;
  while (hi < steps.length && steps[hi]?.parallel?.id === pid) hi++;
  return [lo, hi];
}

/** Kinds the server never lets run side by side (backend `NOT_PARALLEL`,
 * plus Judge, which types to the agent) — the fallback when the kinds list
 * (its `parallel_safe`) is not at hand. */
export const PARALLEL_UNSAFE: ReadonlySet<string> = new Set(["agent", "ship", "reviews", "verify", "split", "join", "land", "sync", "loop", "judge"]);

/** May a step of `kind` sit in a parallel group? The kinds list's
 * `parallel_safe` when it says, else `PARALLEL_UNSAFE`. */
export function isParallelSafe(kind: string, kinds?: readonly Pick<StepKindDef, "kind" | "parallel_safe">[] | null): boolean {
  const def = kinds?.find((k) => k.kind === kind);
  if (def && typeof def.parallel_safe === "boolean") return def.parallel_safe;
  return !PARALLEL_UNSAFE.has(kind);
}

/** Every parallel group of `steps` (two members or more), in order. */
export function parallelGroups(steps: readonly Pick<FlightStep, "parallel">[]): { id: string; lo: number; hi: number }[] {
  const out: { id: string; lo: number; hi: number }[] = [];
  for (let i = 0; i < steps.length; ) {
    const [lo, hi] = parallelSpan(steps, i);
    if (hi - lo > 1) out.push({ id: steps[i].parallel!.id, lo, hi });
    i = hi;
  }
  return out;
}

export interface StepGroup {
  id: string;
  name: string;
  plan_id: string;
}

export interface FlightTrigger {
  kind: TriggerKind;
  schedule: FlightSchedule | null;
  paused: boolean;
  /** Round 15: the repository a `main_push` / `main_red` trigger watches
   * (an absolute path; the server refuses a main_* trigger without one). */
  repo?: string;
  /** No re-fire inside this many minutes (server default 60). */
  quiet_minutes?: number;
  /** A `hook` trigger's token (32 hex), minted by the server on save: the
   * URL is `<origin>/api/hooks/flyway/<token>` (`hookUrl`). */
  token?: string;
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
  event:
    | "started" | "done" | "failed" | "skipped" | "you" | "halted" | "paused" | "resumed" | "jumped"
    | "added" | "edited" | "removed" | "moved" | "halted_at" | "attached" | "looped" | "took_off";
  note: string;
  /** Round 19: on `took_off` — what took a ready chain off. */
  by?: "run" | "prompt";
}

export interface Flight {
  /** Round 18: started by another chain's Handoff stop. */
  triggered_by?: { flight: string; step: string; session: string };
  /** A FREE chain (2026-11-02): run with no session, in the folder
   * `repo_root` — like an n8n workflow, nothing is started for it. When a run
   * ends it goes back on the ground (`last_run` says how it went). */
  free?: boolean;
  last_run?: { state: "done" | "cancelled"; reason: string; at: number };
  /** A grounded chain that EDITS an intake source's default stops (the Sky's
   * intake lane, 2026-11-02): never run; Save as default writes its steps as
   * that source's own saved stop (`PUT /api/flights/intake-default`). */
  edits_default?: IntakeSourceRef;
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
  /** Round 19: how it takes off ("now" for everything started at once). */
  start_on?: FlightStartOn;
  /** Round 19: when it became `ready` (0 when it never was). */
  ready_since?: number;
}

export type FieldType = "text" | "textarea" | "int" | "bool" | "select" | "duration";

export interface StepKindField {
  name: string;
  type: FieldType;
  label: string;
  /** select only. The contract leaves the item shape open: a plain value, or
   * a value with its own label — `fieldOptions` reads both. */
  options?: Array<string | { value: string; label?: string }>;
  default?: unknown;
  placeholder?: string;
  /** Round 16: a select whose options the UI fills from the line —
   * `"stops_before"`: every stop before this one (`stopsBefore`); the
   * server's `options` stay empty. */
  dynamic?: "stops_before" | "chains" | "sessions";
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
  /** What the stop touches outside the worktree (docs/flights.md "Round 15"):
   * `remote` (the shared remote), `irreversible` (Ctrl+Z cannot undo it),
   * `spends` (money outside the agent's own usage). Older servers omit it. */
  risk?: string[];
  /** The palette's grouping: work | ship | outside | people. Older servers omit it. */
  family?: string;
  /** What a new step of this kind does on failure when the plan says nothing. */
  default_on_fail?: string;
  /** Round 20: may a step of this kind sit in a parallel group (it neither
   * types to the agent nor uses the worktree's lanes)? Older servers omit it
   * — treat that as false. */
  parallel_safe?: boolean;
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

/** Round 19: a flight a session's line draws — one in the air (`isLive`), or
 * one built and waiting for ▶ Run or the session's next prompt (`ready`). */
export function isShown(f: Pick<Flight, "state">): boolean {
  return isLive(f) || f.state === "ready";
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
    .replace(/^(you|perch|wait for me):\s*/i, "");
  return t ? "Wait for me: " + t : "Wait for me";
}

/** The bell's words for a flight perched on you: "Perched on you: <stop>". */
export function perchedOnYou(stepTitle: string | null | undefined): string {
  const t = String(stepTitle || "")
    .trim()
    .replace(/^(you|perch|wait for me):\s*/i, "");
  return "Needs you" + (t ? ": " + t : "");
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
      ? "Finished"
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

/** The built-in saved stop a kind's flights use when its source names none. The
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

/** One choice of a `dynamic: "stops_before"` select: a stop's id and title,
 * `group` the name of the group it is a sub-step of (the select lists a
 * group's sub-steps under its name). */
export interface StopChoice {
  value: string;
  label: string;
  group?: string;
}

/** A Loop's "Repeat from" choices (docs/flights.md "Round 16"): `""` first —
 * "the first stop", or "the first stop of <group>" when `step` is a sub-step
 * of a group — then every stop of `steps` before `step`, in flight order,
 * by title. A step not on the line yet (a new one) sees the whole line. */
export function stopsBefore(steps: readonly FlightStep[] | null | undefined, step: Pick<FlightStep, "id" | "group">): StopChoice[] {
  const list = steps || [];
  const at = list.findIndex((s) => s.id === step.id);
  const before = at >= 0 ? list.slice(0, at) : list.slice();
  const g = step.group?.name;
  const out: StopChoice[] = [{ value: "", label: g ? "the first stop of " + g : "the first stop" }];
  for (const s of before) {
    const c: StopChoice = { value: s.id, label: (s.title || "").trim() || s.kind };
    if (s.group?.name) c.group = s.group.name;
    out.push(c);
  }
  return out;
}

/** A new stop of a kind: its default title and every field's default. The id
 * is local until saved (the server keeps or reassigns it). */
/** What a stop chip on the sky's shelf carries while dragged (its
 * `dataTransfer`, the stop kind, e.g. "ship"): dropped on a line it is tacked
 * on there — at the gap it lands in, else at the end. */
export const STOP_MIME = "application/x-mindflock-stop";

export function newStep(def: StepKindDef, taken: readonly string[]): FlightStep {
  const config: Record<string, unknown> = {};
  for (const f of def.fields) if (f.default !== undefined) config[f.name] = f.default;
  let n = taken.length + 1;
  while (taken.includes("s" + n)) n++;
  return { id: "s" + n, kind: def.kind, title: def.default_title || def.label, config, on_fail: defaultOnFail(def) };
}

/** What a stop of this kind does on failure when it says nothing: the kind's
 * `default_on_fail` (round 15), "halt" from an older server or an unknown word. */
export function defaultOnFail(def: Pick<StepKindDef, "default_on_fail"> | null | undefined): OnFail {
  const v = def?.default_on_fail;
  return v && Object.prototype.hasOwnProperty.call(ON_FAIL_LABEL, v) ? (v as OnFail) : "halt";
}

/** Move a stop from one index to another (drag and ← →). */
export function moveStep<T>(steps: readonly T[], from: number, to: number): T[] {
  if (from === to || from < 0 || from >= steps.length) return steps.slice();
  const out = steps.slice();
  const [it] = out.splice(from, 1);
  out.splice(Math.max(0, Math.min(out.length, to)), 0, it);
  return out;
}

/** The stop at `index` made a halt point, or no longer one (the flyway
 * editor's ⏸ Halt here / Carry on past here; saved with the flyway). */
export function toggleHalt(steps: readonly FlightStep[], index: number): FlightStep[] {
  const src = steps[index];
  if (!src) return steps.slice();
  const out = steps.slice();
  const { halt, ...rest } = src;
  out[index] = halt ? rest : { ...rest, halt: true };
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

/** A blank manual saved stop (the shelf's "New stops"). */
export function blankPlan(): FlightPlan {
  return {
    id: "",
    name: "New chain",
    description: "",
    builtin: false,
    trigger: { kind: "manual", schedule: null, paused: false },
    session: { repo_path: "", program: "", model: "", in_place: false },
    steps: [],
    updated: 0,
  };
}

/** A flyway from a line's stops ("Save as flyway" on a live line, a card
 * seeded from one): only what a stop IS — kind, title, config, on_fail, when —
 * never what a run did to it (step_states live beside the steps; a config key
 * with a leading `_` is the driver's, not the stop's), and re-numbered s1..sN
 * so the server sees a fresh flyway. A manual trigger unless one is given. */
export function planFromSteps(
  name: string,
  steps: readonly FlightStep[],
  trigger?: FlightPlan["trigger"]
): Omit<FlightPlan, "id" | "updated"> {
  return {
    name: name.trim() || "New chain",
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
      const out: FlightStep = { id: "s" + (i + 1), kind: s.kind, title: s.title, config, on_fail: s.on_fail || "halt" };
      if (s.when?.trim()) out.when = s.when.trim();
      return out;
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
  main_push: "When main moves",
  main_red: "When main is red",
  hook: "When a hook fires",
};

/** The triggers that start their own session (the rest attach to an intake's). */
export function triggerMakesSession(kind: TriggerKind): boolean {
  return kind === "manual" || kind === "schedule" || kind === "hook" || MAIN_TRIGGERS.includes(kind);
}

/** The address an outside system POSTs to fire a `hook` trigger: "" until
 * the plan is saved (the server mints the token then). `origin` defaults to
 * this page's. */
export function hookUrl(trigger: Pick<FlightTrigger, "token">, origin?: string): string {
  const token = trigger.token || "";
  if (!token) return "";
  const base = origin ?? (typeof window !== "undefined" && window.location ? window.location.origin : "");
  return base + "/api/hooks/flyway/" + token;
}

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
    { value: "", label: "Default · " + (builtin?.name || "built-in stops") },
  ];
  for (const p of ordered) out.push({ value: p.id, label: p.name, builtin: p.builtin });
  if (value && !plans.some((p) => p.id === value)) out.push({ value, label: value + " (not found)" });
  return out;
}

// --- A source's "Goes as far as" ---------------------------------------------
// ONE control per Intake source (components/flights/PlanSelect.tsx): the rungs
// (Stop after the agent · Commit · Push · Open PR) or one of its saved stops.
// The server's chain (server._intake_plan) is unchanged: a source's own saved
// stop, when it still exists, wins; else the built-in ship ladder cut at its
// depth (flights._ladder_plan: the agent, then commit → push → pr). A rung
// pick writes the depth AND clears the plan, so a depth can no longer sit
// ignored beside a plan.

/** The ship ladder a depth means, in the words a ship step's title uses. */
const SHIP_WORD: Record<string, string> = { commit: "Commit", push: "Push", pr: "Open PR", merge: "Merge" };

/** A ship lane in the words a ship step's title uses ("Open PR", "Merge"). */
export function shipWord(lane: string): string {
  return SHIP_WORD[lane] || lane;
}
const LADDER: readonly Lane[] = ["commit", "push", "pr", "merge"];

/** A row of steps in words, for one line: "Agent → Commit → Push → Open PR".
 * The agent step reads "Agent"; every other step by its title. */
export function stopsSummaryWords(steps: readonly Pick<FlightStep, "kind" | "title" | "config">[] | null | undefined): string[] {
  return (steps || []).map((s) =>
    s.kind === "agent" ? "Agent" : String(s.title || "").trim() || (s.kind === "ship" ? SHIP_WORD[String(s.config?.lane || "")] || "" : "") || s.kind
  );
}

/** Where a source's items go, as every surface names it (the Intake card,
 * the Sky lane's chip). */
export interface SourceReach {
  /** The control's chip that is on: a rung ("leave", "commit", "push", "pr")
   * or "plan:<id>" for a saved stop. */
  choice: string;
  /** THE words: "Open PR", "Payments stops", "Stop after the agent". */
  words: string;
  /** A saved stop decides (not the rungs). */
  own: boolean;
  /** The depth as the server reads it (lib/autopilot capSourceDepth). */
  rung: Lane;
  /** A saved stop the source names that no longer exists: the server goes by
   * the rung instead. "" when none. */
  missing: string;
  /** The stops it resolves to, in words (`stopsSummaryWords`); [] when the
   * saved stop's steps aren't known here. */
  steps: string[];
  /** The plan id a new item flies (what GET /api/flights/intake-default
   * answers as `plan_id`): the saved stop, `builtin:<kind>@<depth>`, or "". */
  planId: string;
}

/** The built-in a source's ladder is cut from (flights._BUILTIN_FOR_KIND). */
const LADDER_BASE: Record<WaitingKind, string> = { tickets: "builtin:ticket", issues: "builtin:ticket", prs: "builtin:pr" };

/** Where a source with this `plan` and `depth` sends its items (the server's
 * chain, minus a per-start pick). `plans` undefined/null = not known here:
 * a named plan is then trusted to exist. */
export function sourceReach(
  kind: WaitingKind,
  src: { plan?: string | null; depth?: string | null },
  plans: ReadonlyArray<Pick<FlightPlan, "id" | "name"> & { steps?: FlightStep[] }> | null | undefined
): SourceReach {
  const rung = capSourceDepth(src.depth);
  const plan = String(src.plan || "").trim();
  const found = plan && plans ? plans.find((p) => p.id === plan) : undefined;
  if (plan && (found || !plans))
    return { choice: "plan:" + plan, words: found?.name || plan, own: true, rung, missing: "", steps: found ? stopsSummaryWords(found.steps) : [], planId: plan };
  const ship = rung === "leave" ? [] : LADDER.slice(0, LADDER.indexOf(rung) + 1);
  return {
    choice: rung,
    words: sourceRungLabel(rung),
    own: false,
    rung,
    missing: plan,
    steps: ["Agent", ...ship.map((l) => SHIP_WORD[l])],
    planId: rung === "leave" ? "" : LADDER_BASE[kind] + "@" + rung,
  };
}

/** The saved stops a source can pick, after the rungs: your own saved stops
 * (by name), then our built-ins made for this kind of item ("Ticket to merge"
 * — the full built-in, with its reviews, checks and your OK before merging). */
export function sourcePlanOptions(
  plans: readonly Pick<FlightPlan, "id" | "name" | "builtin" | "trigger">[] | null | undefined,
  kind: WaitingKind
): Array<{ value: string; label: string; builtin: boolean }> {
  const triggers: Record<WaitingKind, string[]> = { tickets: ["ticket"], prs: ["pr"], issues: ["issue", "ticket"] };
  const mine = (plans || []).filter((p) => !p.builtin);
  const ours = (plans || []).filter((p) => p.builtin && triggers[kind].includes(String(p.trigger?.kind || "")));
  return [...mine, ...ours].map((p) => ({ value: p.id, label: p.name, builtin: !!p.builtin }));
}

/** The per-source setting's key: `TicketingSource.plan`,
 * `github.repo_settings[repo].plan`, `github.issue_repo_settings[repo].plan`. */
export const PLAN_FIELD = "plan";

/** The server gives a worker with no flight of its own a branch-only `flight`
 * dto (`id` "", `step` null, only `branch` set) so the diagram can find where
 * it fans out from. Everything else in the app keys on "has a flight", so the
 * rows are normalised once, where they enter: such a dto becomes
 * `flight: null` + `flight_branch`. Real flights pass through untouched. */
export function normalizeFlightRows<T extends { flight?: (FlightRow & { branch?: { parent: string; step: string } | null }) | null; flight_branch?: { parent: string; step: string } | null }>(rows: T[]): T[] {
  return rows.map((r) => {
    const f = r.flight;
    if (!f || f.id) return r;
    return { ...r, flight: null, flight_branch: f.branch && f.branch.step ? { parent: f.branch.parent, step: f.branch.step } : null };
  });
}

/** An intake source a default belongs to: a ticketing source (its id), or a
 * GitHub repo's PRs / issues (the repo). The intake lane's block `kind`/`id`. */
export interface IntakeSourceRef {
  kind: "tickets" | "prs" | "issues";
  target: string;
}

/** A source's default stops as the server resolves them
 * (`GET /api/flights/intake-default`): its own saved stop (`own`) or the
 * built-in, cut at its Fast-track depth — the steps new items really fly. */
export interface IntakeDefault extends IntakeSourceRef {
  /** What the lane calls the source ("Tickets · Shortcut", "PRs · o/r"). */
  label: string;
  plan_id: string;
  name: string;
  own: boolean;
  steps: FlightStep[];
}
