/** Verify's actions — the requests the pill, the pane's verify row, the
 * Behaviors card and a Verify stop send, with the cache patching, refreshes,
 * toasts and error cards that go with each (the Verify twin of
 * lib/sessionActions.ts).
 *
 * VERIFY IS THE SESSION'S OWN AGENT (owner request 2026-11-10): "Verify" on a
 * session (`startSessionVerify`) has its agent write its own actionable steps
 * and work them in its own terminal, telling the person in its reply whatever
 * only a person can do. When there are such steps, a person confirms that
 * verification is complete (`confirmVerified`) — the pill says "Confirm
 * verified" until they do.
 *
 * The dialog components own their UI state (which button is busy, which box is
 * open); these own what is said to the server and what the app shows about it.
 * Where a step of the UI has to happen in the MIDDLE of an action — the inline
 * terminal latching on before the reconcile, a deleted row's scratch UI going
 * before the cache drops it — the caller passes it in as a callback, so the
 * order stays exactly what it was when this lived inline in PlanRow.
 *
 * FAILURES DO NOT GO IN A TOAST. `toast()` is a 1.4-second confirmation strip at
 * the bottom of the screen; the failures this surface produces are a paragraph
 * of git or tmux output whose remedy is its last sentence ("… is already used by
 * worktree at <path>"), and they were being shown in a strip that clipped them
 * and vanished before they could be read — from a MODAL, so the strip was often
 * behind it. Intake already puts failures in the bottom-right cards that stay
 * until dismissed; this surface now uses the same corner for the same reason. */

import { api } from "../api/client";
import type {
  SessionVerifyConfirmed,
  SessionVerifyFixStarted,
  SessionVerifyStarted,
  SessionVerifyStepAnswer,
  TestPlan,
  TestPlanRunStarted,
  TestPlansResponse,
} from "../api/types";
import { planName } from "../components/dialogs/verify";
import { queryClient, refreshInstances, refreshTestPlans } from "../state/queries";
import { useUi } from "../state/store";
import { selectSession } from "./sessionActions";
import { errorPop } from "./errorPop";
import { errMsg } from "./format";
import { toast } from "./toast";

/** `/api/test-plans/{id:path}` — the id is a session title and may contain a
 * slash. encodeURIComponent is still right (and is what instApi does for the
 * same reason): the ASGI server percent-decodes the path before routing, so a
 * `%2F` arrives at the path converter as the slash it was. */
export function planPath(id: string): string {
  return "/api/test-plans/" + encodeURIComponent(id);
}

/** Swap one plan in the cached list without waiting for the 10s poll.
 *
 * Every mutating route answers with the state the server just wrote, and the
 * list is shared with the top bar's badge — so the response is applied to the
 * cache directly and `refreshTestPlans()` follows only as a reconcile. Without
 * this, pressing Pass left the row unchanged for up to ten seconds, which reads
 * as "the click didn't take" and gets clicked again. */
export function patchPlan(id: string, apply: (plan: TestPlan) => TestPlan) {
  // CANCEL THE POLL THAT IS ALREADY OPEN. `useTestPlans` refetches every 10s and
  // query-core resolves a successful fetch with an unconditional `setData` — it
  // has no idea a manual update landed while it was in flight — so an optimistic
  // row could be overwritten by a GET that left before the POST. A follow-up
  // `refreshTestPlans()` would cancel it too (`cancelRefetch: true`), but not
  // every caller refetches, so this does it deliberately. Unawaited on
  // purpose: the cancel is synchronous where it matters, and nothing here
  // needs its promise.
  void queryClient.cancelQueries({ queryKey: ["test-plans"] });
  queryClient.setQueryData<TestPlansResponse>(["test-plans"], (prev) =>
    prev ? { ...prev, plans: prev.plans.map((p) => (p.id === id ? apply(p) : p)) } : prev
  );
}

/** The bare run request — the Behaviors card's Verify when the plan's
 * session is gone. `only` scopes it to those step ids. */
export function requestPlanRun(planId: string, only?: string[]) {
  const json: { steps?: string[] } = only ? { steps: only } : {};
  return api<TestPlanRunStarted>(planPath(planId) + "/run", {
    method: "POST",
    json,
  });
}

/** What the app says once a run has started (pure — the toast's text).
 *
 * IN THE SESSION when the server handed the steps to the plan's own session's
 * agent (`in_session`): that agent walks them as its tasks, in its own
 * terminal, and the person talks to it there — so the sentence names it and
 * says so. Otherwise a separate checker session was started (the plan's own
 * session is gone): "Starting", because nothing has been provisioned yet. */
export function runStartedMessage(plan: TestPlan, r: TestPlanRunStarted | null | undefined): string {
  if (r?.in_session && r.session) {
    return (
      "Handed the steps to " + r.session + "'s agent" +
      (r.queued ? " — it takes them once its current turn ends" : "") +
      ". Talk to it there"
    );
  }
  return "Starting an agent on " + planName(plan) + " in " + (r?.session || "a new session");
}

/** Open the session walking a check on its Agent tab — the terminal where
 * its agent works the steps and the person talks to it. The dialog goes first:
 * a pane opening behind a modal is invisible. */
export function openWalkingSession(title: string) {
  if (!title) return;
  const ui = useUi.getState();
  ui.closeDialog();
  selectSession(title, { noKeyboard: true });
  ui.setLastTab(title, "agent");
}

/** "This branch" in the start form's Where: no environment in the body. */
export const VERIFY_WHERE_BRANCH = "branch";

/** `POST /api/instances/{title}/verify` — the bare request. `notes` is what
 * the person wants it to prove (optional); `environment` is where (a Verify
 * environment's name; "" or "branch" = this branch, left out). The body is
 * `{}` with neither. */
export function requestSessionVerify(title: string, notes = "", environment = "") {
  const n = notes.trim();
  const env = environment.trim();
  const json: { notes?: string; environment?: string } = {};
  if (n) json.notes = n;
  if (env && env !== VERIFY_WHERE_BRANCH) json.environment = env;
  return api<SessionVerifyStarted>("/api/instances/" + encodeURIComponent(title) + "/verify", {
    method: "POST",
    json,
  });
}

/** What the app says once a Verify has been taken (pure — the toast's text).
 * QUEUED: the session's agent is mid-turn, so nothing has started yet — say
 * when it will, never that it is writing its steps. */
export function verifyStartedMessage(title: string, r: Partial<SessionVerifyStarted> | null | undefined): string {
  const session = r?.session || title;
  if (r?.queued) return "Verify starts when " + session + "'s current turn ends";
  return "Verifying " + session + " — its agent writes its steps and works them in its terminal";
}

/** "Verify" on a session: its own agent writes its steps and works them in
 * its terminal. Opens that terminal (its Agent tab) unless `open` is false —
 * the person follows it and talks to it there. Resolves whether it started. */
export async function startSessionVerify(
  title: string,
  opts: { notes?: string; environment?: string; open?: boolean } = {},
): Promise<boolean> {
  try {
    const r = await requestSessionVerify(title, opts.notes || "", opts.environment || "");
    toast(verifyStartedMessage(title, r));
    await refreshInstances();
    void refreshTestPlans();
    if (opts.open !== false) openWalkingSession(title);
    return true;
  } catch (err) {
    errorPop("Couldn't start verifying " + title, errMsg(err));
    return false;
  }
}

/** `POST /api/test-plans/{plan}/confirm` — the bare request. `steps` is the
 * person's answer per step (`{id: "pass" | "fail"}`), sent only when given. */
export function requestVerifyConfirm(
  planId: string,
  verified: boolean,
  note = "",
  steps?: Record<string, SessionVerifyStepAnswer> | null,
) {
  const n = note.trim();
  const json: { verified: boolean; note?: string; steps?: Record<string, SessionVerifyStepAnswer> } = { verified };
  if (n) json.note = n;
  if (steps && Object.keys(steps).length) json.steps = steps;
  return api<SessionVerifyConfirmed>(planPath(planId) + "/confirm", {
    method: "POST",
    json,
  });
}

/** What the app says once a person answered (pure — the toast's text). Not
 * verified: "Sent to <session>'s agent" only when the server says the note
 * reached it (`delivered`); otherwise it is on the check, nothing more. */
export function confirmMessage(verified: boolean, r: Partial<SessionVerifyConfirmed> | null | undefined, session: string): string {
  if (verified) return "Confirmed verified";
  return r?.delivered ? "Sent to " + session + "'s agent" : "Saved on the check";
}

/** "Confirm verified" (true) / "Not verified…" (false, with what the person
 * saw): a person closing a verify that waits on them. `session` names the
 * session in the toast (the plan id when not given). Resolves whether the
 * server took it — a 409 means it wasn't waiting any more (someone else
 * answered), said in the card that stays up. */
export async function confirmVerified(
  planId: string,
  verified: boolean,
  note = "",
  opts: { steps?: Record<string, SessionVerifyStepAnswer> | null; session?: string } = {},
): Promise<boolean> {
  try {
    const r = await requestVerifyConfirm(planId, verified, note, opts.steps);
    toast(confirmMessage(verified, r, opts.session || planId));
    await refreshInstances();
    void refreshTestPlans();
    return true;
  } catch (err) {
    errorPop(verified ? "Couldn't confirm verified" : "Couldn't mark it not verified", errMsg(err));
    void refreshInstances();
    return false;
  }
}

/** The bare delete request (the row's Delete and the bulk bar's). Deleting the
 * plan closes its run session too — see the route. */
export function requestPlanDelete(planId: string) {
  return api(planPath(planId), { method: "DELETE" });
}

/** Start a run, optionally scoped to one step (the per-step Re-check).
 *
 * One function for both because they are the same request — a whole-plan run
 * is just this with no filter — so the optimistic patch, the sidebar refresh
 * and the error handling cannot drift between the two. `onStarted` runs once
 * the server has taken it, before the reconcile: the row latches its inline
 * terminal there. */
export async function startChecklistRun(
  plan: TestPlan,
  opts: { only?: string[]; onStarted?(): void } = {}
): Promise<void> {
  try {
    const r = await requestPlanRun(plan.id, opts.only);
    // The server already has a provisioning row for the verify session: pull
    // it now rather than leaving the grid unable to attach a pane to a
    // session it has never heard of, which is what Watch needs a moment later.
    await refreshInstances();
    // THE SERVER'S OWN ROW when it sends one. Hand-synthesising `{state,
    // run_session}` left out the run record `start_run` had just opened, so
    // for the ten seconds until the poll the row said an agent was checking
    // with no "last checked" line and no give-up clock behind it.
    const started = r?.plan;
    patchPlan(plan.id, (p) =>
      started || { ...p, state: "running", run_session: r?.session || "" },
    );
    // Latched, not left to follow `running`: the terminal has to still be
    // there when the run ENDS, which is the moment whatever it found arrives.
    opts.onStarted?.();
    await refreshTestPlans();
    // The row itself changes — the sentence becomes "An agent is checking the
    // steps it can" and the button becomes Watch — which is the feedback that
    // was missing. It deliberately does NOT teleport you into the session any
    // more: triaging three due plans lost the list on the first one, and a
    // pane opening behind a modal is invisible, so the button read as broken.
    // Watch is that trip, kept as one deliberate press.
    // "Starting", not "is checking": at this instant the route has answered
    // 202 and nothing has been provisioned yet — the worktree, the branch and
    // tmux all happen afterwards, and when they fail the app's only statement
    // about this run must not have been a claim that it was under way.
    // `session.create_failed` retracts it (see bridgeTestPlanEvents).
    toast(runStartedMessage(plan, r));
    // IN THE SESSION: the steps are that agent's tasks now, in its own
    // terminal — open it there, which is the whole point of walking it in
    // place (unlike a checker, which Watch reaches on purpose; see above).
    if (r?.in_session && r.session) openWalkingSession(r.session);
  } catch (err) {
    errorPop("Couldn't start the check for " + planName(plan), errMsg(err));
    // The optimistic state above never landed on a failure, but the plan may
    // have moved for another reason (it 409s when a run is already going), so
    // take the server's word for where it actually is.
    void refreshTestPlans();
  }
}

/** Stop the run without recording a verdict.
 *
 * The counterpart to Run, and needed for the same reason Run is worth
 * watching: it is minutes of a real agent on a real branch, so "not that
 * commit" / "it is stuck" has to have an answer that isn't waiting out the
 * server's two-hour give-up clock. The session is closed, not deleted — see
 * the endpoint — so whatever it found is still there to read. */
export async function cancelChecklistRun(plan: TestPlan): Promise<void> {
  try {
    const r = await api<VerifyCancelled>(
      planPath(plan.id) + "/cancel",
      { method: "POST" }
    );
    if (r?.plan) patchPlan(plan.id, () => r.plan as TestPlan);
    // The session this pane was watching is ending, so take the window with
    // it rather than leaving a "Verifying" head over a dead socket. App.tsx
    // reaps orphans as a backstop; closing it here is what makes the press
    // feel like it did something.
    if (plan.run_session) useUi.getState().closeVerifyPane(plan.run_session);
    // The verify session has just left the grid; the sidebar must not keep
    // showing it as live work.
    await refreshInstances();
    await refreshTestPlans();
    toast(cancelMessage(plan.id, r));
  } catch (err) {
    errorPop("Couldn't stop verifying", errMsg(err));
    void refreshTestPlans();
  }
}

/** `POST /api/test-plans/{id}/cancel` → `verify_stopped: true` from a newer
 * server when it stopped the session's own agent verifying. */
interface VerifyCancelled {
  session?: string;
  plan?: TestPlan;
  verify_stopped?: boolean;
}

/** What the app says once a verify was stopped (pure). The SESSION keeps
 * running — "Stopped my-session" read as if the session itself had been
 * stopped. Only a separate session the plan's run opened (its own session
 * gone) is actually closed, and that is said by name. */
export function cancelMessage(planId: string, r: VerifyCancelled | null | undefined): string {
  const session = r?.session || "";
  if (r?.verify_stopped || !session || session === planId)
    return "Stopped verifying " + (session || planId) + " — the session keeps running";
  return "Stopped verifying " + planId + " — closed " + session;
}

/** "Stop verifying" on the pane's verify row and the ⋯ menu: the session's
 * own agent stops working its steps; the session itself keeps running. */
export async function stopVerifying(planId: string, session = ""): Promise<boolean> {
  try {
    const r = await api<VerifyCancelled>(planPath(planId) + "/cancel", { method: "POST" });
    if (r?.plan) patchPlan(planId, () => r.plan as TestPlan);
    toast(cancelMessage(planId, { ...r, session: r?.session || session }));
    await refreshInstances();
    void refreshTestPlans();
    return true;
  } catch (err) {
    errorPop("Couldn't stop verifying " + (session || planId), errMsg(err));
    void refreshInstances();
    return false;
  }
}

/** "Dismiss" on a failed or blocked verify row: they stay until a person
 * says they've seen it (`POST /api/test-plans/{id}/dismiss`). */
export async function dismissVerify(planId: string, session = ""): Promise<boolean> {
  try {
    await api<{ ok: boolean }>(planPath(planId) + "/dismiss", { method: "POST" });
    await refreshInstances();
    return true;
  } catch (err) {
    errorPop("Couldn't dismiss the verify on " + (session || planId), errMsg(err));
    return false;
  }
}

/** What the app says once "Let its agent fix it" was taken (pure).
 * IN THE SESSION (`in_session`): the failed steps and their evidence went to
 * the session's own agent, and Verify runs again after its turn. An older
 * server ignores `in_session` and opens a NEW session — said as such, so the
 * person knows where the fix is happening. */
export function fixMessage(session: string, r: SessionVerifyFixStarted | null | undefined): string {
  if (r?.in_session) {
    const who = r.session || session;
    return (
      "Sent what failed to " + who + "'s agent" +
      (r.queued ? " — it starts once its current turn ends" : "") +
      "; Verify runs again after it fixes it"
    );
  }
  return (
    "Opened a new session, " + (r?.session || "a new session") + ", to fix what failed" +
    (r?.reclaimed ? " (cleared the last one's empty workspace)" : "")
  );
}

/** "Let its agent fix it" on a failed verify: hand the failed steps and
 * their evidence to the session's OWN agent, which fixes them and then
 * verifies again (`{in_session: true}` on the fix route). Resolves whether
 * the server took it. */
export async function fixInSession(planId: string, session = ""): Promise<boolean> {
  try {
    const r = await api<SessionVerifyFixStarted>(planPath(planId) + "/fix", {
      method: "POST",
      json: { in_session: true },
    });
    toast(fixMessage(session || planId, r));
    await refreshInstances();
    void refreshTestPlans();
    if (r?.in_session) openWalkingSession(r.session || session || planId);
    return true;
  } catch (err) {
    errorPop("Fix didn't start", errMsg(err));
    return false;
  }
}

/** Open a session to fix what the check found.
 *
 * The press this surface was missing. A red checklist is the most valuable
 * thing the feature produces — it shipped, it does not do what it was for,
 * and somebody wrote down exactly how — and the only thing you could do with
 * one was read it. An ORDINARY session, never the verify one: a verify run's
 * whole posture is "report, never fix", and reusing it to make a change would
 * dismantle the property that makes its report evidence.
 *
 * Deliberately not the primary button on a failed row — the primary stays
 * "See what failed", because reading the evidence before spending a session on
 * it is the right order, and a step can simply be wrong. */
export async function fixPlanFailures(plan: TestPlan, only?: string[]): Promise<void> {
  try {
    const r = await api<{ session?: string; reclaimed?: boolean }>(
      planPath(plan.id) + "/fix",
      { method: "POST", json: only ? { steps: only } : {} },
    );
    await refreshInstances();
    // The cleanup is said out loud when there was one. A workspace being
    // reclaimed under you is the kind of thing that looks like a bug later if
    // nothing ever mentioned it — and it is also the answer to "why did this
    // press work when the last one didn't?".
    toast(
      "Opened " +
        (r?.session || "a session") +
        " to fix what failed" +
        (r?.reclaimed ? " (cleared the last one's empty workspace)" : ""),
    );
  } catch (err) {
    errorPop("Fix didn't start", errMsg(err));
  }
}

/** "It's out — check it now", skipping the rest of the deploy wait.
 *
 * The delay is a good guess and never a fact: a pipeline that usually takes
 * fifteen minutes sometimes takes three, and somebody who just watched it land
 * should not have to wait out a timer that has already been proved wrong. */
export async function markPlanDeployed(plan: TestPlan): Promise<void> {
  try {
    const r = await api<{ plan?: TestPlan }>(planPath(plan.id) + "/deployed", {
      method: "POST",
    });
    if (r?.plan) patchPlan(plan.id, () => r.plan as TestPlan);
    await refreshTestPlans();
    toast(planName(plan) + " is ready to check");
  } catch (err) {
    errorPop("Couldn't release " + planName(plan) + " for checking", errMsg(err));
    void refreshTestPlans();
  }
}

/** Delete a checklist. `watchSession` is the session its log lives in (its
 * window goes too); `onGone` runs once the server has taken it, before the
 * cache drops the row — where the row forgets its scratch UI. */
export async function removePlan(
  plan: TestPlan,
  opts: { watchSession?: string; onGone?(): void } = {}
): Promise<void> {
  try {
    await requestPlanDelete(plan.id);
    // Deleting the plan closes its run session too (see the route), so the
    // watch window goes with it.
    if (opts.watchSession) useUi.getState().closeVerifyPane(opts.watchSession);
    opts.onGone?.();
    queryClient.setQueryData<TestPlansResponse>(["test-plans"], (prev) =>
      prev ? { ...prev, plans: prev.plans.filter((p) => p.id !== plan.id) } : prev
    );
    await refreshTestPlans();
  } catch (err) {
    errorPop("Couldn't delete " + planName(plan), errMsg(err));
    void refreshTestPlans();
  }
}

/** A repo Verify can write a checklist in by branch (GET /api/test-plans/repos). */
export interface VerifyRepo {
  name: string;
  root: string;
}

export async function fetchVerifyRepos(): Promise<VerifyRepo[]> {
  const r = await api<{ repos?: VerifyRepo[] }>("/api/test-plans/repos");
  return Array.isArray(r?.repos) ? r.repos : [];
}

/** The Behaviors board's "Check another branch…" (docs/behaviors.md §3): ask
 * the agent for behaviors for a branch no session has been on, or (`empty`)
 * start an empty set to write by hand. Resolves to the plan id ("" when the
 * server refused — the reason is popped). */
export async function askBehaviorsForBranch(repo: VerifyRepo, branch: string, opts: { empty?: boolean } = {}): Promise<string> {
  try {
    const r = await api<{ plan: string; existing: boolean }>("/api/test-plans/branch", {
      json: opts.empty ? { repo: repo.root, branch, empty: true } : { repo: repo.root, branch },
    });
    toast(
      r.existing
        ? branch + " already has behaviors — they're on the Behaviors board"
        : opts.empty
          ? "Started empty behaviors for " + branch + " — add what it should do"
          : "Asked the agent to write behaviors for " + branch + " — up to three minutes",
    );
    void refreshTestPlans();
    return r.plan || "";
  } catch (err) {
    errorPop("Couldn't start behaviors for " + branch, errMsg(err));
    return "";
  }
}

/** "Start empty behaviors for a session…": an empty set (origin "manual", no
 * model call) on a session's branch, open or closed. Resolves to the plan id
 * — new, or the one already covering the branch — or "" when refused. */
export async function startEmptyBehaviors(session: string): Promise<string> {
  try {
    const r = await api<{ plan: string; existing: boolean }>(
      "/api/instances/" + encodeURIComponent(session) + "/test-plan",
      { method: "POST", json: { empty: true } },
    );
    await refreshTestPlans();
    toast(
      r.existing
        ? session + " already has behaviors — add yours to them"
        : "Started empty behaviors for " + session + " — add what it should do",
    );
    return r.plan || "";
  } catch (err) {
    errorPop("Couldn't start behaviors for " + session, errMsg(err));
    return "";
  }
}
