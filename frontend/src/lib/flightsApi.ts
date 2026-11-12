/** The Flights routes, as the UI calls them (contract: docs/flights.md "The
 * HTTP API"). One place for the paths and the "act, refetch, say so" shape, so
 * the Board, the pane strip and the bell can never drift apart on what a
 * button posts.
 *
 *  - Done / Skip / Retry   POST /api/flights/{id}/steps/{sid}/done {note?} · /skip · /retry
 *                          (Done on a Verify stop parked on you is 409
 *                          `answer_behaviors`: `stepDone` opens its behaviors)
 *  - Let the agent fix it  POST /api/flights/{id}/steps/{sid}/op/fix (a halted stop)
 *  - Jump to…              POST /api/flights/{id}/jump {step}
 *  - Pause / Resume / Cancel  POST /api/flights/{id}/pause · /resume · /cancel
 *  - Take off              POST /api/flights/start {plan_id, repo_path?, start_at?}
 *                          (`start_at` a stop id, or "resume": where an earlier
 *                          run of this route stopped)
 *  - Check a start         POST /api/flights/check (a start's body; nothing starts)
 *                          — `checkStart`, with the `resume` point
 *  - Fly again             POST /api/flights/{id}/again {at?, title?} (an ended route)
 *  - Fly (waiting to fly)  the Intake start routes, with `plan`
 *  - Flyways               GET/POST /api/flights/plans · DELETE /{id} · POST /{id}/reset
 *  - Describe a stop       POST /api/flights/describe {text, want}
 *  - Tack on a stop        a session with no flight: POST /api/flights/start
 *                          {title, steps, absorb_fasttrack: true}; a live flight:
 *                          POST /api/flights/{id}/steps {step, at?}
 *  - Edit / drop a stop    PATCH · DELETE /api/flights/{id}/steps/{sid} (a stop ahead;
 *                          PATCH also the current one when it failed or waits
 *                          at its halt point — `saveStop` resolves to the refusal)
 *  - Halt here             PATCH /api/flights/{id}/steps/{sid} {halt} (a pending stop or
 *                          the current one)
 *  - Move a stop           POST /api/flights/{id}/steps/{sid}/move {at} (pending only,
 *                          to an index after the current stop)
 *
 * Every start ends in `selectSession(title)` as soon as the row exists — the
 * way lib/templateRun.ts does. */

import { ApiError, api } from "../api/client";
import type { Instance } from "../api/types";
import { queryClient, refreshInstances } from "../state/queries";
import { type BehaviorsTarget, openBehaviors } from "./behaviorsFocus";
import { errorPop } from "./errorPop";
import { errMsg } from "./format";
import { selectSession } from "./sessionActions";
import { toast } from "./toast";
import { windowName } from "./windowName";
import type { Flight, FlightPlan, FlightStartOn, FlightStep, IntakeDefault, IntakeSourceRef, StepKindDef } from "./flights";

const enc = encodeURIComponent;
export const flightPath = (id: string, rest = "") => "/api/flights/" + enc(id) + rest;
export const stepPath = (id: string, step: string, verb: "done" | "skip" | "retry") =>
  flightPath(id, "/steps/" + enc(step) + "/" + verb);

// --- Reads --------------------------------------------------------------------

export async function fetchFlights(all = false): Promise<Flight[]> {
  const d = await api<{ flights?: Flight[] }>("/api/flights" + (all ? "?all=1" : ""));
  return d?.flights || [];
}

export async function fetchFlightPlans(): Promise<FlightPlan[]> {
  const d = await api<{ plans?: FlightPlan[] }>("/api/flights/plans");
  return d?.plans || [];
}

export async function fetchFlightKinds(): Promise<StepKindDef[]> {
  const d = await api<{ kinds?: StepKindDef[] }>("/api/flights/kinds");
  return d?.kinds || [];
}

/** After anything that moves a flight or a plan. */
export function refreshFlights() {
  void queryClient.invalidateQueries({ queryKey: ["flight-plans"] });
  return queryClient.invalidateQueries({ queryKey: ["flights"] });
}

// --- Acting on a flight ---------------------------------------------------------

/** POST, refetch the flights and the rows, and say so; a failure opens the
 * error card (the server's sentence carries the remedy). Resolves true when
 * it went through. */
export async function flightAction(what: string, path: string, body: unknown = {}, done = ""): Promise<boolean> {
  try {
    await api(path, { json: body });
    if (done) toast(done);
    return true;
  } catch (err) {
    errorPop(what + " failed", errMsg(err));
    return false;
  } finally {
    void refreshFlights();
    void refreshInstances();
  }
}

/** A Verify stop parked on you moves on when its behaviors are answered,
 * not on Done (docs/behaviors.md §5): the server refuses it with 409
 * `answer_behaviors`. */
export function isAnswerBehaviors(err: unknown): boolean {
  if (!(err instanceof ApiError) || err.status !== 409) return false;
  const b = err.body as { error?: unknown } | null;
  return !!b && typeof b === "object" && b.error === "answer_behaviors";
}

/** Where a flight's behaviors live: its session's (or branch's) card on the
 * route, else the next behaviors that need you. */
export function flightBehaviorsTarget(flightId: string): BehaviorsTarget {
  const flights = queryClient.getQueryData<Flight[] | null>(["flights"]) || [];
  const fl = flights.find((f) => f.id === flightId);
  if (fl?.session) return { title: fl.session, ...(fl.branch ? { branch: fl.branch } : {}) };
  if (fl?.branch) return { branch: fl.branch };
  return { next: true };
}

/** "Behaviors" on a Verify stop parked on you: its card, in the stop's place. */
export function answerBehaviors(flightId: string): void {
  openBehaviors(flightBehaviorsTarget(flightId));
}

/** Done on a stop parked on you. A Verify stop refuses it (409
 * `answer_behaviors`): that is not an error — its behaviors open instead. */
export async function stepDone(flightId: string, stepId: string, note = ""): Promise<boolean> {
  try {
    await api(stepPath(flightId, stepId, "done"), { json: note ? { note } : {} });
    return true;
  } catch (err) {
    if (isAnswerBehaviors(err)) {
      toast("Answer it on its Verify card (or Skip it) — the route goes on once it has an answer");
      answerBehaviors(flightId);
    } else errorPop("Done failed", errMsg(err));
    return false;
  } finally {
    void refreshFlights();
    void refreshInstances();
  }
}

/** Let the agent fix it, on a halted stop: the failure goes to the session's
 * agent and the stop runs again once its turn ends. */
export function stepFix(flightId: string, stepId: string): Promise<boolean> {
  return flightAction(
    "Let the agent fix it",
    flightPath(flightId, "/steps/" + enc(stepId) + "/op/fix"),
    {},
    "Handed to the agent — the stop runs again after its fix"
  );
}

export function stepSkip(flightId: string, stepId: string): Promise<boolean> {
  return flightAction("Skip", stepPath(flightId, stepId, "skip"));
}

export function stepRetry(flightId: string, stepId: string): Promise<boolean> {
  return flightAction("Retry", stepPath(flightId, stepId, "retry"));
}

export function jumpFlight(flightId: string, stepId: string, title = ""): Promise<boolean> {
  return flightAction("Jump", flightPath(flightId, "/jump"), { step: stepId }, title ? "Jumped to " + title : "");
}

export function controlFlight(flightId: string, verb: "pause" | "resume" | "cancel" | "run"): Promise<boolean> {
  const said =
    verb === "pause"
      ? "Route paused — Carry on picks it up again"
      : verb === "resume"
        ? "Route carrying on"
        : verb === "run"
          ? "Route started"
          : "Route stopped — its session is kept";
  const what = verb === "pause" ? "Pause" : verb === "resume" ? "Carry on" : verb === "run" ? "Run" : "Stop the route";
  return flightAction(what, flightPath(flightId, "/" + verb), {}, said);
}

/** Round 19: ▶ Run on a ready chain — `POST /api/flights/{id}/run`. */
export function runFlight(flightId: string): Promise<boolean> {
  return controlFlight(flightId, "run");
}

/** Round 19: how a ready chain starts — `PATCH /api/flights/{id} {start_on}`
 * ("now" runs it at once; anything but a ready chain is 409 not_ready). */
export async function setStartOn(flightId: string, startOn: FlightStartOn): Promise<boolean> {
  try {
    await api(flightPath(flightId), { method: "PATCH", json: { start_on: startOn } });
    return true;
  } catch (err) {
    errorPop("Changing how it starts failed", errMsg(err));
    return false;
  } finally {
    void refreshFlights();
    void refreshInstances();
  }
}

/** ⏸ Add halt point / Remove halt point on a stop of a live flight: `{halt}`
 * alone, which the server takes on a pending stop or the current one. */
export async function setHalt(flightId: string, stepId: string, halt: boolean): Promise<boolean> {
  try {
    await api(flightPath(flightId, "/steps/" + enc(stepId)), { method: "PATCH", json: { halt } });
    toast(halt ? "Halt point set — the route pauses before this stop" : "Halt point taken off");
    return true;
  } catch (err) {
    errorPop(halt ? "Adding the halt point failed" : "Removing the halt point failed", errMsg(err));
    return false;
  } finally {
    void refreshFlights();
  }
}

// --- Starting --------------------------------------------------------------------

/** Select a session as soon as its row exists: refetch the rows until it is
 * there (a start answers before the row is listed now and then), then focus
 * it. Gives up quietly after a few seconds — the row arrives with the next
 * poll and the rail shows it. */
export async function openWhenReady(title: string, tries = 8, waitMs = 750): Promise<boolean> {
  if (!title) return false;
  for (let i = 0; i < tries; i++) {
    await refreshInstances();
    const rows = queryClient.getQueryData<Instance[]>(["instances"]) || [];
    if (rows.some((r) => r.title === title)) {
      selectSession(title);
      return true;
    }
    await new Promise((r) => setTimeout(r, waitMs));
  }
  return false;
}

export interface StartBody {
  plan_id: string;
  /** Round 19: "prompt" / "run" = built, waiting (ready); absent = take off now. */
  start_on?: FlightStartOn;
  title?: string;
  subject?: { kind: string; source: string; id: string };
  repo_path?: string;
  prompt?: string;
  start_at?: string;
}

/** Take off, resolving to the flight or the server's refusal with the field
 * it names — for a form that shows it under that field (no card). */
export async function takeOffResult(body: StartBody): Promise<SaveResult & { flight: Flight | null }> {
  try {
    const d = await api<{ flight?: Flight }>("/api/flights/start", { json: body });
    const flight = d?.flight || null;
    void refreshFlights();
    if (flight?.session) {
      toast(startedText(flight));
      void openWhenReady(flight.session);
    }
    return { ...SAVED, flight };
  } catch (err) {
    return { ...refusalOf(err), flight: null };
  }
}

/** Take off: a manual flight of a plan (the server creates its session from
 * the plan's session defaults, or attaches to `title`). Resolves to the
 * flight, null on a refusal (the error card says why). */
export async function takeOff(body: StartBody): Promise<Flight | null> {
  const r = await takeOffResult(body);
  if (!r.ok) errorPop("Starting the route failed", r.detail);
  return r.flight;
}

/** Fly a saved flyway on an existing session (a line on the Sky). */
export function startFlightOn(title: string, planId: string, startOn?: FlightStartOn): Promise<Flight | null> {
  return takeOff({ plan_id: planId, title, ...(startOn ? { start_on: startOn } : {}) });
}

/** The toast after a start: "Started: <route>", and where when it starts
 * past its first stop ("Started: Ship it at Push"). */
export function startedText(flight: Pick<Flight, "plan_name" | "cursor" | "steps">): string {
  const at = flight.cursor > 0 ? flight.steps[flight.cursor]?.title : "";
  return "Started: " + flight.plan_name + (at ? " at " + at : "");
}

/** A start's body that starts at `startAt` (a stop id or "resume"): it flies
 * now — a route built to wait for ▶ Run always starts at its first stop, so
 * `start_on` goes. "" = the body as it is (the normal start). */
export function startBodyAt<B extends { start_on?: FlightStartOn; start_at?: string }>(body: B, startAt: string): B {
  if (!startAt) return body;
  const out: B = { ...body, start_at: startAt };
  delete out.start_on;
  return out;
}

/** Fly a saved route on an existing session, starting at stop `startAt`
 * ("" = the normal start, built to wait for ▶ Run when `startOn` says so). */
export function startFlightAt(title: string, planId: string, startAt: string, startOn?: FlightStartOn): Promise<Flight | null> {
  return takeOff(startBodyAt({ plan_id: planId, title, ...(startOn ? { start_on: startOn } : {}) } as StartBody, startAt));
}

// --- Checking a start, flying an ended route again ---------------------------------

/** Where an earlier, unfinished run of this route (on this session, branch
 * or ticket) stopped: the stop of THIS route it carries on at. */
export interface ResumePoint {
  stop: string;
  index: number;
  title: string;
  from_flight: string;
  session: string;
  state: "cancelled" | "halted" | "running" | "waiting_you" | "paused";
  reason: string;
  /** When it ended or last moved (epoch seconds). */
  at: number;
  /** The stop is an Agent stop whose prompt already went out: carrying on
   * waits for that turn, nothing is sent again. */
  in_progress?: boolean;
}

/** One thing a check found: `{field, detail, error?, session?}`. */
export interface StartProblem {
  field: string;
  detail: string;
  error?: string;
  session?: string;
}

/** `POST /api/flights/check`'s answer: the start as it would go. */
export interface StartCheck {
  ok: boolean;
  steps: FlightStep[];
  start: { id: string; index: number; title: string; kind?: string } | null;
  skipped: Array<{ id: string; index: number; title: string; reason: string }>;
  problems: StartProblem[];
  warnings: StartProblem[];
  in_flight?: { id: string; name: string; state: string } | null;
  /** null = no earlier unfinished run of this route here. */
  resume?: ResumePoint | null;
}

/** Ask the server how a start would go (nothing starts). null when it can't
 * say (an older server, the network) — the caller starts as it always did. */
export async function checkStart(body: StartBody): Promise<StartCheck | null> {
  try {
    const d = await api<StartCheck>("/api/flights/check", { json: body });
    return d && typeof d === "object" ? d : null;
  } catch {
    return null;
  }
}

/** `POST /api/flights/{id}/again`'s answer. */
export interface AgainResult {
  flight: Flight;
  from: string;
  session: string;
  how: "same" | "chosen" | "branch" | "reopened" | "restored";
}

/** The body of a Fly again: `at` a stop id ("" = where it stopped — the
 * first stop for a route that finished), `title` another session. */
export function againBody(at = "", title = ""): { at?: string; title?: string } {
  return { ...(at ? { at } : {}), ...(title ? { title } : {}) };
}

/** The toast after Fly again, by how the server found its session. `stop` is
 * where the new run starts, `session` its session's shown name. */
export function againText(how: AgainResult["how"], stop: string, session: string): string {
  const from = stop ? " from " + stop : "";
  if (how === "branch") return "Flying again on " + session + ", which holds its branch";
  if (how === "reopened") return "Reopened " + session + " and flying again" + from;
  if (how === "restored") return "Reopened " + session + ": its route carried on as it was";
  if (how === "chosen") return "Flying again on " + session + from;
  return "Flying again" + from;
}

/** Fly an ended route again (`at` / `title` as `againBody`): on success the
 * toast says how and its session opens; a refusal opens the error card with
 * the server's sentence. Resolves to the answer, null on a refusal. */
export async function flyAgain(flightId: string, at = "", title = ""): Promise<AgainResult | null> {
  try {
    const d = await api<AgainResult>(flightPath(flightId, "/again"), { json: againBody(at, title) });
    const f = d?.flight;
    const stop = f ? f.steps[f.cursor]?.title || "" : "";
    toast(againText(d.how, stop, windowName(d.session)));
    if (d.session) void openWhenReady(d.session);
    return d;
  } catch (err) {
    errorPop("Flying it again failed", refusalOf(err).detail);
    return null;
  } finally {
    void refreshFlights();
    void refreshInstances();
  }
}

/** What a waiting item's Fly posts: the Intake start routes with their bodies
 * as today, plus `plan` when one was picked ("" = the source's own plan, which
 * the server resolves — so a source with only a depth behaves as before). */
export type WaitingTarget =
  | { kind: "tickets"; source: string; id: string }
  | { kind: "prs"; repo: string; number: number }
  | { kind: "issues"; repo: string; number: number };

export function flyRequest(
  t: WaitingTarget,
  plan: string,
  extra: Record<string, string> = {}
): { path: string; body: Record<string, unknown> } {
  // The row's own picks (agent / depth / effort / repo), only when made.
  const picks: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(extra)) if (v) picks[k] = v;
  if (plan) picks.plan = plan;
  if (t.kind === "tickets") return { path: "/api/tickets/start", body: { source: t.source, id: t.id, ...picks } };
  if (t.kind === "prs") return { path: "/api/github/prs/review", body: { repo: t.repo, number: t.number, ...picks } };
  return { path: "/api/github/issues/start", body: { repo: t.repo, number: t.number, ...picks } };
}

/** Fly a waiting item and open its session as soon as its row exists.
 * Resolves to the session title; a refusal throws the server's sentence (the
 * row that pressed Fly shows it). */
export async function flyWaiting(
  t: WaitingTarget,
  plan: string,
  extra: Record<string, string> = {},
  fallbackTitle = ""
): Promise<string> {
  const req = flyRequest(t, plan, extra);
  try {
    const r = await api<{ title?: string }>(req.path, { json: req.body });
    const title = r?.title || fallbackTitle;
    void refreshFlights();
    if (title) void openWhenReady(title);
    return title;
  } finally {
    void refreshInstances();
  }
}

// --- Flyways -----------------------------------------------------------------

/** Save a flyway (no id = create). Throws the server's refusal ({error, field})
 * so the editor can point at the stop — `refusalOf(err)` reads it, or call
 * `savePlanResult`. */
export async function savePlan(plan: FlightPlan): Promise<FlightPlan> {
  const body: Partial<FlightPlan> = { ...plan };
  if (!body.id) delete body.id;
  const d = await api<{ plan: FlightPlan }>("/api/flights/plans", { json: body });
  void refreshFlights();
  return d.plan;
}

/** `savePlan` that resolves instead of throwing: the saved plan, or the
 * server's refusal with the field it names (`steps[2].config.timeout_minutes`). */
export async function savePlanResult(plan: FlightPlan): Promise<SaveResult & { plan: FlightPlan | null }> {
  try {
    return { ...SAVED, plan: await savePlan(plan) };
  } catch (err) {
    return { ...refusalOf(err), plan: null };
  }
}

export async function deletePlan(id: string): Promise<void> {
  await api("/api/flights/plans/" + enc(id), { method: "DELETE" });
  void refreshFlights();
}

export async function resetPlan(id: string): Promise<FlightPlan> {
  const d = await api<{ plan: FlightPlan }>("/api/flights/plans/" + enc(id) + "/reset", { method: "POST" });
  void refreshFlights();
  return d.plan;
}

/** Describe a stop (or a whole flyway) in a sentence; the server's one-shot
 * model answers with stops already through the flyway validator. */
export async function describeSteps(text: string, want: "step" | "plan" = "step"): Promise<FlightStep[]> {
  const d = await api<{ steps?: FlightStep[] }>("/api/flights/describe", { json: { text, want } });
  return d?.steps || [];
}


// --- Tacking stops onto a session's line ------------------------------------------

/** The step a stop posts: its kind, title, config, on_fail and (a halt
 * point's) halt — the id is the server's to give. */
export function wireStep(step: FlightStep): Omit<FlightStep, "id"> {
  const out: Omit<FlightStep, "id"> = { kind: step.kind, title: step.title, config: step.config, on_fail: step.on_fail };
  if (step.halt) out.halt = true;
  if (step.when) out.when = step.when;
  // Round 18: where the Sky draws it (a stop dropped at a spot keeps it).
  if (step.pos) out.pos = step.pos;
  // Round 20: its parallel group rides along (a copied row keeps its forks).
  if (step.parallel) out.parallel = step.parallel;
  return out;
}

/** The `+` at the end of a session's line, on a session with no flight yet: an
 * ad-hoc flight of these stops (no saved flyway), which takes over an armed
 * fast-track — its remaining rungs become the leading ship stops — so the
 * ladder's boxes turn into the flight's without a jump. */
export async function tackOnNew(title: string, steps: FlightStep[], startOn?: FlightStartOn): Promise<Flight | null> {
  try {
    const d = await api<{ flight?: Flight }>("/api/flights/start", {
      json: { title, steps: steps.map(wireStep), absorb_fasttrack: true, ...(startOn ? { start_on: startOn } : {}) },
    });
    return d?.flight || null;
  } catch (err) {
    errorPop("Adding the stops failed", errMsg(err));
    return null;
  } finally {
    void refreshFlights();
    void refreshInstances();
  }
}

/** Tack one stop onto a live flight: appended, or at `at` (only after its
 * current stop — the server refuses anything earlier). */
export function tackOn(flightId: string, step: FlightStep, at?: number, opts: TackOnOpts = {}): Promise<boolean> {
  return flightAction("Adding the stop", flightPath(flightId, "/steps"), {
    step: wireStep(step),
    ...(at === undefined ? {} : { at }),
    ...(opts.parallel_with ? { parallel_with: opts.parallel_with } : {}),
  });
}

/** Round 20: how a stop is tacked on — `parallel_with` (a step id) puts it in
 * that step's parallel group, making one when it has none. */
export interface TackOnOpts {
  parallel_with?: string;
}

/** Round 20: a stop that flies side by side with `withSid` (after the group's
 * last member; `at` within the group). 400 `not_parallel_safe` names a kind
 * that can't (the error card says "use a Split for agents"). */
export function tackOnParallel(flightId: string, step: FlightStep, withSid: string, at?: number): Promise<boolean> {
  return tackOn(flightId, step, at, { parallel_with: withSid });
}

async function patchParallel(flightId: string, stepId: string, body: { parallel: null } | { parallel_with: string }): Promise<boolean> {
  try {
    await api(flightPath(flightId, "/steps/" + enc(stepId)), { method: "PATCH", json: body });
    return true;
  } catch (err) {
    errorPop("Saving the stop failed", errMsg(err));
    return false;
  } finally {
    void refreshFlights();
  }
}

/** Round 20: a stop still ahead leaves its parallel group (it then flies
 * right after the group). */
export function leaveParallel(flightId: string, stepId: string): Promise<boolean> {
  return patchParallel(flightId, stepId, { parallel: null });
}

/** Round 20: a stop still ahead joins `withSid`'s parallel group (making one),
 * moving to after its last member — the Sky's Shift-drop. */
export function joinParallel(flightId: string, stepId: string, withSid: string): Promise<boolean> {
  return patchParallel(flightId, stepId, { parallel_with: withSid });
}

/** Every stop of a flyway, appended to a live flight in order. */
export async function tackOnAll(flightId: string, steps: FlightStep[]): Promise<boolean> {
  for (const s of steps) if (!(await tackOn(flightId, s))) return false;
  return true;
}

/** How a save went: `ok`, else the server's refusal — `detail` its sentence
 * (verbatim: it says what is allowed), `field` the path it names
 * (`steps[2].config.timeout_minutes`, `step.title`; "" when it names none)
 * and `error` its code. A form shows `detail` under the field `field` ends
 * with (StopDialog `fieldNameOf`). */
export interface SaveResult {
  ok: boolean;
  field: string;
  detail: string;
  error: string;
}

export const SAVED: SaveResult = { ok: true, field: "", detail: "", error: "" };

/** A thrown request error as a refusal: the server's `{error, detail, field}`
 * (any of them may be missing — an older server, a network failure). */
export function refusalOf(err: unknown): SaveResult {
  const b = err instanceof ApiError && err.body && typeof err.body === "object" ? (err.body as Record<string, unknown>) : {};
  const str = (v: unknown) => (typeof v === "string" ? v : "");
  return { ok: false, field: str(b.field), detail: str(b.detail) || errMsg(err), error: str(b.error) };
}

/** Whether a value is a `SaveResult` (a dialog's `onSave` may resolve to
 * one, or to nothing from an older caller). */
export function isSaveResult(v: unknown): v is SaveResult {
  return !!v && typeof v === "object" && typeof (v as SaveResult).ok === "boolean" && "field" in v && "detail" in v;
}

type StopPatchBody = Partial<Pick<FlightStep, "title" | "config" | "on_fail" | "halt" | "when">>;

/** Edit a stop of a live flight in place (title / config / on_fail / when /
 * halt) — one still ahead, or (flow-polish) the CURRENT one while the route
 * is paused at its halt point or the stop failed: the server resets it, so
 * Retry / Carry on runs the new settings. No card on a refusal — the dialog
 * shows it under the field; the flights are refetched before it resolves so
 * the dialog reads the saved stop. */
export async function saveStop(flightId: string, stepId: string, patch: StopPatchBody): Promise<SaveResult> {
  try {
    await api(flightPath(flightId, "/steps/" + enc(stepId)), { method: "PATCH", json: patch });
    return SAVED;
  } catch (err) {
    return refusalOf(err);
  } finally {
    await refreshFlights();
  }
}

/** `saveStop` for a caller with no form to point at: a refusal opens the
 * error card. Resolves true when it went through. */
export async function patchStop(flightId: string, stepId: string, patch: StopPatchBody): Promise<boolean> {
  try {
    await api(flightPath(flightId, "/steps/" + enc(stepId)), { method: "PATCH", json: patch });
    return true;
  } catch (err) {
    errorPop("Saving the stop failed", errMsg(err));
    return false;
  } finally {
    void refreshFlights();
  }
}

/** Take a pending stop off a live flight. */
export async function dropStop(flightId: string, stepId: string): Promise<boolean> {
  try {
    await api(flightPath(flightId, "/steps/" + enc(stepId)), { method: "DELETE" });
    return true;
  } catch (err) {
    errorPop("Removing the stop failed", errMsg(err));
    return false;
  } finally {
    void refreshFlights();
    void refreshInstances();
  }
}

/** Move a pending stop of a live flight to index `at` of the final list (its
 * drag along the line, ⇤ / ⇥). The server refuses (409 `in_progress`) a stop,
 * or a place, that is not after the current stop; the error card says so and
 * the line stays as it was. */
export function moveStop(flightId: string, stepId: string, at: number): Promise<boolean> {
  return flightAction("Moving the stop", flightPath(flightId, "/steps/" + enc(stepId) + "/move"), { at });
}

// --- Round 11: groups, grounded flights (docs/flights.md "Round 11") --------

/** A saved stop tacked onto a live (or grounded) flight as ONE group: the
 * server copies its steps under a fresh group id named after it. */
export function tackOnPlan(flightId: string, planId: string, at?: number): Promise<boolean> {
  return flightAction("Adding the stop", flightPath(flightId, "/steps"), { plan_id: planId, ...(at === undefined ? {} : { at }) });
}

/** Several stops tacked on at once, grouped under `groupName` when given. */
export function tackOnSteps(flightId: string, steps: FlightStep[], at?: number, groupName?: string): Promise<boolean> {
  return flightAction("Adding the stop", flightPath(flightId, "/steps"), {
    // Their ids ride along: the server gives them new ones and re-aims a Loop
    // among them that repeats from one of them.
    steps: steps.map((s) => ({ ...wireStep(s), id: s.id })),
    ...(at === undefined ? {} : { at }),
    ...(groupName ? { group: { name: groupName } } : {}),
  });
}

/** The named steps (consecutive, each able to change) become one group. */
export function groupStops(flightId: string, stepIds: string[], name: string): Promise<boolean> {
  return flightAction("Grouping", flightPath(flightId, "/group"), { steps: stepIds, name });
}

/** Every step of the group loses it. */
export function ungroupStops(flightId: string, groupId: string): Promise<boolean> {
  return flightAction("Ungrouping", flightPath(flightId, "/ungroup"), { group: groupId });
}

/** Rename a step's group (every step of it), or `null` to take this one step
 * out of it. */
export function patchStopGroup(flightId: string, stepId: string, group: { name: string } | null): Promise<boolean> {
  return flightAction("Saving the stop", flightPath(flightId, "/steps/" + enc(stepId)), { group });
}

/** Take a whole group off a flight (each step by the remove rules). */
export async function dropGroup(flightId: string, stepId: string): Promise<boolean> {
  try {
    await api(flightPath(flightId, "/steps/" + enc(stepId) + "?group=1"), { method: "DELETE" });
    return true;
  } catch (err) {
    errorPop("Removing the stop failed", errMsg(err));
    return false;
  } finally {
    void refreshFlights();
    void refreshInstances();
  }
}

/** Stops on the board with no session yet (`state: "grounded"`): from a saved
 * stop (one group) or from loose steps (grouped under `name` when set). */
export async function startGrounded(body: { plan_id?: string; steps?: FlightStep[]; name?: string; edits_default?: IntakeSourceRef }): Promise<Flight | null> {
  try {
    const d = await api<{ flight?: Flight }>("/api/flights/start", {
      json: {
        grounded: true,
        ...(body.plan_id ? { plan_id: body.plan_id } : {}),
        ...(body.steps ? { steps: body.steps.map(wireStep) } : {}),
        ...(body.name ? { name: body.name } : {}),
        ...(body.edits_default ? { edits_default: body.edits_default } : {}),
      },
    });
    return d?.flight || null;
  } catch (err) {
    errorPop("Adding the stops failed", errMsg(err));
    return null;
  } finally {
    void refreshFlights();
  }
}

/** Bind a grounded flight to a live session (`title`) and take off, or make
 * the session first (`title: ""` + `repo_path`…) and then attach. */
export async function attachGrounded(
  flightId: string,
  body: { title: string; folder?: string; repo_path?: string; program?: string; model?: string; in_place?: boolean; prompt?: string; start_on?: FlightStartOn }
): Promise<Flight | null> {
  try {
    const d = await api<{ flight?: Flight }>(flightPath(flightId, "/attach"), { json: body });
    return d?.flight || null;
  } catch (err) {
    errorPop("Running the stops failed", errMsg(err));
    return null;
  } finally {
    void refreshFlights();
    void refreshInstances();
  }
}

/** "No session — the default folder" for `attachGrounded` (`{title:
 * DEFAULT_SESSION}`): a chain that needs no session runs there on its own
 * (nothing is started); one that needs a session gets a new one there.
 * `{folder}` names another folder the same way. */
export const DEFAULT_SESSION = "@default";

/** Delete a flight that is not in the air (a grounded one, or a finished one). */
export async function deleteFlight(flightId: string): Promise<boolean> {
  try {
    await api(flightPath(flightId), { method: "DELETE" });
    return true;
  } catch (err) {
    errorPop("Removing the stops failed", errMsg(err));
    return false;
  } finally {
    void refreshFlights();
  }
}

/** Rename a flight — its line's title (a grounded one's especially):
 * `PATCH /api/flights/{id} {plan_name}`. */
export async function renameFlight(flightId: string, name: string): Promise<boolean> {
  try {
    await api(flightPath(flightId), { method: "PATCH", json: { plan_name: name } });
    return true;
  } catch (err) {
    errorPop("Renaming failed", errMsg(err));
    return false;
  } finally {
    void refreshFlights();
  }
}

/** Round 18: free positions of a flight's stops (chain-local; null clears) —
 * `POST /api/flights/{id}/layout`. Layout only: any step, flown too. */
export async function saveStopPositions(flightId: string, positions: Record<string, { x: number; y: number } | null>): Promise<boolean> {
  try {
    await api(flightPath(flightId, "/layout"), { json: { positions } });
    return true;
  } catch (err) {
    errorPop("Saving where the stops sit failed", errMsg(err));
    return false;
  } finally {
    void refreshFlights();
  }
}

/** Round 18: stop `stepId` and every stop after it leave into a new
 * stops-only chain (its first stop at `pos`) — `POST …/steps/{sid}/split_off`. */
export async function splitOffChain(flightId: string, stepId: string, pos?: { x: number; y: number }): Promise<Flight | null> {
  try {
    const d = await api<{ chain?: Flight }>(flightPath(flightId, "/steps/" + enc(stepId) + "/split_off"), { json: pos ? { pos } : {} });
    return d?.chain || null;
  } catch (err) {
    errorPop("Splitting the route failed", errMsg(err));
    return null;
  } finally {
    void refreshFlights();
  }
}

/** Round 18: a node dragged on the Sky — its spot queued and sent with the
 * flight's other moves a moment later, as ONE `POST …/layout` per flight
 * (a drag that lands, then another, costs one call). `onDone` hears whether
 * that batch was saved. */
const posQueue = new Map<string, { positions: Record<string, { x: number; y: number } | null>; timer: ReturnType<typeof setTimeout>; done: Array<(ok: boolean) => void> }>();
export const POS_BATCH_MS = 250;
export function queueStopPos(flightId: string, stepId: string, pos: { x: number; y: number } | null, onDone?: (ok: boolean) => void): void {
  let q = posQueue.get(flightId);
  if (q) clearTimeout(q.timer);
  else {
    q = { positions: {}, timer: 0 as unknown as ReturnType<typeof setTimeout>, done: [] };
    posQueue.set(flightId, q);
  }
  q.positions[stepId] = pos ? { x: Math.round(pos.x), y: Math.round(pos.y) } : null;
  if (onDone) q.done.push(onDone);
  const cur = q;
  cur.timer = setTimeout(() => {
    posQueue.delete(flightId);
    void saveStopPositions(flightId, cur.positions).then((ok) => cur.done.forEach((f) => f(ok)));
  }, POS_BATCH_MS);
}

/** Send every queued spot now (tests; a page about to go). */
export function flushStopPos(): void {
  for (const [id, q] of [...posQueue]) {
    clearTimeout(q.timer);
    posQueue.delete(id);
    void saveStopPositions(id, q.positions).then((ok) => q.done.forEach((f) => f(ok)));
  }
}

// --- An intake source's default stops (2026-11-02) ------------------------------
// Our built-in stops are every source's default; the owner edits one source's
// in the Sky with the blocks (the lane's stops chip, or a ticket line's ⋯ "Use
// these stops for every ticket from …"). Editing ONE ticket's line never
// changes the default — only these calls do.

const defaultQuery = (ref: IntakeSourceRef) => "kind=" + encodeURIComponent(ref.kind) + "&target=" + encodeURIComponent(ref.target);

/** The stops a source's new items fly now (its own saved stop, else the
 * built-in at its depth); null when the server can't say (logged as a pop). */
export async function getIntakeDefault(ref: IntakeSourceRef): Promise<IntakeDefault | null> {
  try {
    return await api<IntakeDefault>("/api/flights/intake-default?" + defaultQuery(ref));
  } catch (err) {
    errorPop("Couldn't read the default stops", errMsg(err));
    return null;
  }
}

/** The intake source a session line's flight came from (its subject), or
 * null — a hand-made flight, or a source since removed. Quiet on failure. */
export async function intakeSourceOfFlight(flightId: string): Promise<IntakeDefault | null> {
  try {
    return await api<IntakeDefault>("/api/flights/intake-default/for-flight/" + encodeURIComponent(flightId));
  } catch {
    return null;
  }
}

/** Set a source's default stops. One of: `steps` (saved as the source's own
 * saved stop — its existing own one is replaced when no other source shares
 * it), `from_flight` (that flight's steps, its source from its subject), or
 * `plan_id` (an existing saved stop; "" = back to our built-in). Returns the
 * default as it now stands, with `previous_plan_id` for an Undo. */
export async function setIntakeDefault(
  body: (IntakeSourceRef & ({ steps: FlightStep[]; name?: string } | { plan_id: string })) | { from_flight: string }
): Promise<(IntakeDefault & { previous_plan_id: string }) | null> {
  try {
    const json = "steps" in body ? { ...body, steps: body.steps.map(wireStep) } : body;
    const d = await api<IntakeDefault & { previous_plan_id: string }>("/api/flights/intake-default", { method: "PUT", json });
    return d;
  } catch (err) {
    errorPop("Saving the default stops failed", errMsg(err));
    return null;
  } finally {
    void refreshFlights();
    // The lane reads each source's plan off the settings doc; Intake off these.
    void queryClient.invalidateQueries({ queryKey: ["settings"] });
    void queryClient.invalidateQueries({ queryKey: ["ticketing-sources"] });
  }
}
