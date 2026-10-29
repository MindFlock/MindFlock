/** The Flights routes, as the UI calls them (contract: docs/flights.md "The
 * HTTP API"). One place for the paths and the "act, refetch, say so" shape, so
 * the Board, the pane strip and the bell can never drift apart on what a
 * button posts.
 *
 *  - Done / Skip / Retry   POST /api/flights/{id}/steps/{sid}/done {note?} · /skip · /retry
 *  - Jump to…              POST /api/flights/{id}/jump {step}
 *  - Pause / Resume / Cancel  POST /api/flights/{id}/pause · /resume · /cancel
 *  - Take off              POST /api/flights/start {plan_id, repo_path?}
 *  - Start (an arrival)    the Intake start routes, with `plan`
 *  - Plans                 GET/POST /api/flights/plans · DELETE /{id} · POST /{id}/reset
 *  - Describe a step       POST /api/flights/describe {text, want}
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
import type { Flight, FlightPlan, FlightStep, StepKindDef } from "./flights";

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

export function controlFlight(flightId: string, verb: "pause" | "resume" | "cancel"): Promise<boolean> {
  const said = verb === "pause" ? "Flight paused" : verb === "resume" ? "Flight resumed" : "Flight cancelled — its session is kept";
  return flightAction(verb[0].toUpperCase() + verb.slice(1), flightPath(flightId, "/" + verb), {}, said);
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

/** What an arrival's Start posts: the Intake start routes with their bodies
 * as today, plus `plan` when one was picked ("" = the source's own plan, which
 * the server resolves — so a source with only a depth behaves as before). */
export type ArrivalTarget =
  | { kind: "tickets"; source: string; id: string }
  | { kind: "prs"; repo: string; number: number }
  | { kind: "issues"; repo: string; number: number };

export function arrivalStartRequest(
  t: ArrivalTarget,
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

/** Start an arrival and open its session as soon as its row exists. Resolves
 * to the session title; a refusal throws the server's sentence (the row that
 * pressed Start shows it). */
export async function startArrival(
  t: ArrivalTarget,
  plan: string,
  extra: Record<string, string> = {},
  fallbackTitle = ""
): Promise<string> {
  const req = arrivalStartRequest(t, plan, extra);
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

// --- Plans -------------------------------------------------------------------

/** Save a plan (no id = create). Throws the server's refusal ({error, field})
 * so the editor can point at the block. */
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

/** Describe a step (or a plan) in a sentence; the server's one-shot model
 * answers with blocks already through the plan validator. */
export async function describeSteps(text: string, want: "step" | "plan" = "step"): Promise<FlightStep[]> {
  const d = await api<{ steps?: FlightStep[] }>("/api/flights/describe", { json: { text, want } });
  return d?.steps || [];
}
