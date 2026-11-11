/** "Run PR review" (owner request 2026-11-11): run the team's saved PR review
 * route — CodeRabbit plus the team-rules agent review, whatever that route's
 * stops are — on an open PR, even while the session's agent is still there.
 *
 * One server action, `POST /api/pr-review` (api/types.ts PrReviewResult):
 *  - a session with no route STARTS the review route;
 *  - a session already on a route gets the review stops INSERTED right after
 *    its current stop;
 *  - a bare PR link finds, or CREATES, the session for the PR's branch first.
 *
 * Which route is "the review route" is Settings → Workspace → PR route
 * (`flights.pr_review_plan`, the route every PR flies — also the ones picked
 * up automatically; unset = the built-in "Pull request" route, "off" = none,
 * which is the 400 below); the request may name another with `plan`.
 *
 * Called from the session ⋯ menus (components/sidebar/prReviewMenu.ts) and
 * Intake → GitHub PRs (components/intake/PrReviewAction.tsx). An older server
 * has no such route: its bare 404 hides the action for the rest of the page
 * life (`prReviewSupported`) instead of failing every click. */

import { api, ApiError } from "../api/client";
import type { PrReviewRequestBody, PrReviewResult, PrReviewStop } from "../api/types";
import { refreshInstances } from "../state/queries";
import { useUi } from "../state/store";
import { errorPop } from "./errorPop";
import { fetchFlights, refreshFlights } from "./flightsApi";
import type { Flight } from "./flights";
import { errMsg } from "./format";
import { openSettings } from "./openSettings";
import { selectSession } from "./sessionActions";
import { toast } from "./toast";

export interface PrReviewRequest {
  /** The session to review (its PR). */
  session?: string;
  /** An open PR's link — alone, the server finds or opens its session. */
  prUrl?: string;
  /** A saved route's id or name; absent = the one chosen in Settings. */
  plan?: string;
}

/** Where the review route is chosen, in the words the 400 sends people to. */
export const PR_REVIEW_SETTING = "Settings → Workspace → PR route";

/** The JSON the server takes: only the fields that were given. */
export function prReviewBody(req: PrReviewRequest): PrReviewRequestBody {
  const body: PrReviewRequestBody = {};
  if (req.session) body.session = req.session;
  if (req.prUrl) body.pr_url = req.prUrl;
  if (req.plan) body.plan = req.plan;
  return body;
}

/** `POST /api/pr-review` — the bare request. */
export function runPrReview(req: PrReviewRequest): Promise<PrReviewResult> {
  return api<PrReviewResult>("/api/pr-review", { method: "POST", json: prReviewBody(req) });
}

// --- An older server --------------------------------------------------------

let unsupported = false;
const listeners = new Set<() => void>();

/** False once a server answered with no such route (it predates the action). */
export function prReviewSupported(): boolean {
  return !unsupported;
}

/** Re-render on a change of `prReviewSupported()` (the Intake rows hide). */
export function onPrReviewSupport(fn: () => void): () => void {
  listeners.add(fn);
  return () => void listeners.delete(fn);
}

/** Tests: back to "the server has it". */
export function resetPrReviewSupport(): void {
  unsupported = false;
}

/** The server has no `/api/pr-review` at all: FastAPI's own 404 for an unknown
 * path is `{"detail": "Not Found"}`, while the action's own 404 (no such
 * session or PR) says why in `error`. */
export function isMissingRoute(err: unknown): boolean {
  if (!(err instanceof ApiError) || err.status !== 404) return false;
  const b = err.body as { error?: unknown } | null;
  return !(b && typeof b === "object" && typeof b.error === "string" && b.error);
}

// --- Words ------------------------------------------------------------------

/** The title of the stop the review stops were put after: the one just before
 * the first of them on that route. "" when the route isn't known here. */
export function stopBefore(flights: readonly Flight[] | null | undefined, flightId: string, stops: readonly PrReviewStop[]): string {
  const f = (flights || []).find((x) => x.id === flightId);
  if (!f || !stops.length) return "";
  const at = f.steps.findIndex((s) => s.id === stops[0].id);
  return at > 0 ? f.steps[at - 1].title || "" : "";
}

/** What a started review says, per mode. `after` names the current stop of an
 * insert ("" = it isn't known, said as "the current stop"). */
export function prReviewMessage(r: Pick<PrReviewResult, "mode" | "session">, after = ""): string {
  const who = r.session || "the session";
  if (r.mode === "inserted") return "Review steps added to " + who + "'s route after " + (after || "the current stop");
  if (r.mode === "created") return "Opened " + who + " for the PR and started its review";
  return "PR review started on " + who;
}

/** A failure in plain words, the remedy last. */
export function prReviewErrorText(err: unknown): string {
  if (isMissingRoute(err)) return "This MindFlock server can't run PR reviews yet — update it, then try again.";
  if (err instanceof ApiError) {
    const said = (err.body as { error?: unknown } | null)?.error;
    const why = typeof said === "string" && said ? said : "";
    if (err.status === 400) return (why ? why.replace(/\.?$/, ". ") : "PR routes are off. ") + "Choose a review route in " + PR_REVIEW_SETTING + ".";
    if (err.status === 409) return why || "The session is paused or held — resume it, then run the review again.";
    if (err.status === 404) return why || "MindFlock couldn't find that session or PR — it may have just closed.";
  }
  return errMsg(err);
}

// --- The action -------------------------------------------------------------

/** Bring the session forward on its Agent tab, where the review stops run.
 * The dialog (Intake) goes first: a pane opening behind a modal is invisible. */
export function openReviewedSession(title: string): void {
  if (!title) return;
  const ui = useUi.getState();
  ui.closeDialog();
  selectSession(title, { noKeyboard: true });
  ui.setLastTab(title, "agent");
}

/** Run the review route and say what happened: a toast and the session's
 * Agent tab on success (unless `open` is false), an error card otherwise —
 * with a button to the setting when no route is chosen. Resolves the result,
 * or null when it didn't start. */
export async function startPrReview(req: PrReviewRequest, opts: { open?: boolean } = {}): Promise<PrReviewResult | null> {
  try {
    const r = await runPrReview(req);
    let after = "";
    if (r.mode === "inserted") {
      try {
        after = stopBefore(await fetchFlights(), r.flight, r.stops || []);
      } catch {
        // The words fall back to "the current stop"; the review is on.
      }
    }
    toast(prReviewMessage(r, after));
    void refreshFlights();
    void refreshInstances();
    if (opts.open !== false) openReviewedSession(r.session);
    return r;
  } catch (err) {
    if (isMissingRoute(err)) {
      unsupported = true;
      for (const fn of listeners) fn();
    }
    const where = req.session || "this PR";
    const toSettings =
      err instanceof ApiError && err.status === 400
        ? [{ label: "Choose a route", primary: true, run: () => openSettings("workspace") }]
        : undefined;
    errorPop("Couldn't run the PR review on " + where, prReviewErrorText(err), toSettings);
    return null;
  }
}
