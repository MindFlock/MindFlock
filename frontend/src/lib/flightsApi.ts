/** The Flights routes, as the UI calls them (contract: docs/flights.md "The
 * HTTP API"). One place for the paths and the "act, refetch, say so" shape, so
 * the Board, the pane strip and the bell can never drift apart on what a
 * button posts.
 *
 *  - Done / Skip / Retry   POST /api/flights/{id}/steps/{sid}/done {note?} · /skip · /retry
 *  - Jump to…              POST /api/flights/{id}/jump {step}
 *  - Pause / Resume / Cancel  POST /api/flights/{id}/pause · /resume · /cancel
 *  - Take off              POST /api/flights/start {plan_id, repo_path?}
 *  - Fly (waiting to fly)  the Intake start routes, with `plan`
 *  - Flyways               GET/POST /api/flights/plans · DELETE /{id} · POST /{id}/reset
 *  - Describe a stop       POST /api/flights/describe {text, want}
 *  - Tack on a stop        a session with no flight: POST /api/flights/start
 *                          {title, steps, absorb_fasttrack: true}; a live flight:
 *                          POST /api/flights/{id}/steps {step, at?}
 *  - Edit / drop a stop    PATCH · DELETE /api/flights/{id}/steps/{sid} (pending only)
 *  - Halt here             PATCH /api/flights/{id}/steps/{sid} {halt} (a pending stop or
 *                          the current one)
 *  - Move a stop           POST /api/flights/{id}/steps/{sid}/move {at} (pending only,
 *                          to an index after the current stop)
 *
 * Every start ends in `selectSession(title)` as soon as the row exists — the
 * way lib/templateRun.ts does. */

import { api } from "../api/client";
import type { Instance } from "../api/types";
import { queryClient, refreshInstances } from "../state/queries";
import { errorPop } from "./errorPop";
import { errMsg } from "./format";
import { selectSession } from "./sessionActions";
import { toast } from "./toast";
import type { Flight, FlightPlan, FlightStartOn, FlightStep, StepKindDef } from "./flights";

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

export function stepDone(flightId: string, stepId: string, note = ""): Promise<boolean> {
  return flightAction("Done", stepPath(flightId, stepId, "done"), note ? { note } : {});
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
      ? "Flight halted — Carry on picks it up again"
      : verb === "resume"
        ? "Flight carrying on"
        : verb === "run"
          ? "Chain started"
          : "Flight cancelled — its session is kept";
  const what = verb === "pause" ? "Halt now" : verb === "resume" ? "Carry on" : verb === "run" ? "Run" : "Cancel";
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

/** ⏸ Halt here / Carry on past here on a stop of a live flight: `{halt}`
 * alone, which the server takes on a pending stop or the current one. */
export async function setHalt(flightId: string, stepId: string, halt: boolean): Promise<boolean> {
  try {
    await api(flightPath(flightId, "/steps/" + enc(stepId)), { method: "PATCH", json: { halt } });
    toast(halt ? "Halt point set — the flight stops before this stop" : "Halt point taken off");
    return true;
  } catch (err) {
    errorPop(halt ? "Halt here failed" : "Carry on past here failed", errMsg(err));
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

/** Take off: a manual flight of a plan (the server creates its session from
 * the plan's session defaults, or attaches to `title`). Resolves to the
 * flight, null on a refusal (the error card says why). */
export async function takeOff(body: StartBody): Promise<Flight | null> {
  try {
    const d = await api<{ flight?: Flight }>("/api/flights/start", { json: body });
    const flight = d?.flight || null;
    void refreshFlights();
    if (flight?.session) {
      toast("Took off: " + flight.plan_name);
      void openWhenReady(flight.session);
    }
    return flight;
  } catch (err) {
    errorPop("Take off failed", errMsg(err));
    return null;
  }
}

/** Fly a saved flyway on an existing session (a line on the Sky). */
export function startFlightOn(title: string, planId: string, startOn?: FlightStartOn): Promise<Flight | null> {
  return takeOff({ plan_id: planId, title, ...(startOn ? { start_on: startOn } : {}) });
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
 * so the editor can point at the stop. */
export async function savePlan(plan: FlightPlan): Promise<FlightPlan> {
  const body: Partial<FlightPlan> = { ...plan };
  if (!body.id) delete body.id;
  const d = await api<{ plan: FlightPlan }>("/api/flights/plans", { json: body });
  void refreshFlights();
  return d.plan;
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
    errorPop("Tack on failed", errMsg(err));
    return null;
  } finally {
    void refreshFlights();
    void refreshInstances();
  }
}

/** Tack one stop onto a live flight: appended, or at `at` (only after its
 * current stop — the server refuses anything earlier). */
export function tackOn(flightId: string, step: FlightStep, at?: number, opts: TackOnOpts = {}): Promise<boolean> {
  return flightAction("Tack on", flightPath(flightId, "/steps"), {
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

/** Edit a pending stop of a live flight in place (title / config / on_fail). */
export async function patchStop(flightId: string, stepId: string, patch: Partial<Pick<FlightStep, "title" | "config" | "on_fail" | "halt" | "when">>): Promise<boolean> {
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
  return flightAction("Tack on", flightPath(flightId, "/steps"), { plan_id: planId, ...(at === undefined ? {} : { at }) });
}

/** Several stops tacked on at once, grouped under `groupName` when given. */
export function tackOnSteps(flightId: string, steps: FlightStep[], at?: number, groupName?: string): Promise<boolean> {
  return flightAction("Tack on", flightPath(flightId, "/steps"), {
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
export async function startGrounded(body: { plan_id?: string; steps?: FlightStep[]; name?: string }): Promise<Flight | null> {
  try {
    const d = await api<{ flight?: Flight }>("/api/flights/start", {
      json: {
        grounded: true,
        ...(body.plan_id ? { plan_id: body.plan_id } : {}),
        ...(body.steps ? { steps: body.steps.map(wireStep) } : {}),
        ...(body.name ? { name: body.name } : {}),
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
  body: { title: string; repo_path?: string; program?: string; model?: string; in_place?: boolean; prompt?: string; start_on?: FlightStartOn }
): Promise<Flight | null> {
  try {
    const d = await api<{ flight?: Flight }>(flightPath(flightId, "/attach"), { json: body });
    return d?.flight || null;
  } catch (err) {
    errorPop("Flying the stops failed", errMsg(err));
    return null;
  } finally {
    void refreshFlights();
    void refreshInstances();
  }
}

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
    errorPop("Splitting the chain failed", errMsg(err));
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
