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
import type { SessionVerifyStarted, TestPlan, TestPlanRunStarted, TestPlansResponse, TestRunWalk } from "../api/types";
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

/** The bare run request — the row's Run, the per-step Re-check and the bulk
 * bar's Run all send exactly this. `only` scopes it to those step ids.
 * `walk: "all"` asks the walker to attempt the person's steps too (it answers
 * one only with evidence, else "needs you"); absent = the server's default,
 * "agent" — so the ordinary call's body is unchanged. */
export function requestPlanRun(planId: string, only?: string[], walk?: TestRunWalk) {
  const json: { steps?: string[]; walk?: TestRunWalk } = only ? { steps: only } : {};
  if (walk === "all") json.walk = walk;
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
export function runStartedMessage(plan: TestPlan, r: TestPlanRunStarted | null | undefined, walk?: TestRunWalk): string {
  const all = walk === "all";
  if (r?.in_session && r.session) {
    return (
      "Handed the steps to " + r.session + "'s agent" +
      (r.queued ? " — it takes them once its current turn ends" : "") +
      (all ? ". It walks every behavior, yours too" : "") +
      ". Talk to it there"
    );
  }
  return (
    "Starting an agent on " + planName(plan) + " in " + (r?.session || "a new session") +
    (all ? " — it walks every behavior, yours too" : "")
  );
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

/** `POST /api/instances/{title}/verify` — the bare request. `notes` is what
 * the person wants checked (optional); the body is `{}` without it. */
export function requestSessionVerify(title: string, notes = "") {
  const n = notes.trim();
  return api<SessionVerifyStarted>("/api/instances/" + encodeURIComponent(title) + "/verify", {
    method: "POST",
    json: n ? { notes: n } : {},
  });
}

/** "Verify" on a session: its own agent writes its steps and works them in
 * its terminal. Opens that terminal (its Agent tab) unless `open` is false —
 * the person follows it and talks to it there. Resolves whether it started. */
export async function startSessionVerify(title: string, opts: { notes?: string; open?: boolean } = {}): Promise<boolean> {
  try {
    await requestSessionVerify(title, opts.notes || "");
    toast("Verifying " + title + " — its agent writes its steps and works them in its terminal");
    await refreshInstances();
    void refreshTestPlans();
    if (opts.open !== false) openWalkingSession(title);
    return true;
  } catch (err) {
    errorPop("Couldn't start verifying " + title, errMsg(err));
    return false;
  }
}

/** `POST /api/test-plans/{plan}/confirm` — the bare request. */
export function requestVerifyConfirm(planId: string, verified: boolean, note = "") {
  const n = note.trim();
  return api<{ ok: boolean; plan: string }>(planPath(planId) + "/confirm", {
    method: "POST",
    json: n ? { verified, note: n } : { verified },
  });
}

/** "Confirm verified" (true) / "Not verified…" (false, with what the person
 * saw): a person closing a verify that had steps only a person could do.
 * Resolves whether the server took it — a 409 means it wasn't waiting any
 * more (someone else answered), said in the card that stays up. */
export async function confirmVerified(planId: string, verified: boolean, note = ""): Promise<boolean> {
  try {
    await requestVerifyConfirm(planId, verified, note);
    toast(verified ? "Confirmed verified" : "Marked not verified — the agent gets your note");
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
  opts: { only?: string[]; walk?: TestRunWalk; onStarted?(): void } = {}
): Promise<void> {
  try {
    const r = await requestPlanRun(plan.id, opts.only, opts.walk);
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
    toast(runStartedMessage(plan, r, opts.walk));
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
    const r = await api<{ session?: string; plan?: TestPlan }>(
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
    toast(r?.session ? "Stopped " + r.session : "Check cancelled");
  } catch (err) {
    errorPop("Couldn't cancel the check", errMsg(err));
    void refreshTestPlans();
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

/** "Ask the agent for a checklist" for a BRANCH no session has been on
 * (New checklist → Another branch…). Same answers as `writePlanFor`; resolves
 * whether the server took it, so the form can clear. */
export async function writePlanForBranch(repo: VerifyRepo, branch: string): Promise<boolean> {
  try {
    const r = await api<{ plan: string; existing: boolean }>("/api/test-plans/branch", {
      json: { repo: repo.root, branch },
    });
    toast(
      r.existing
        ? branch + " already has behaviors — they're on the Behaviors board in the Sky"
        : "Asked the agent to write the behaviors for " + branch + " — they'll appear on the Behaviors board (up to three minutes)"
    );
    refreshTestPlans();
    return true;
  } catch (err) {
    errorPop("Couldn't write the behaviors", errMsg(err));
    return false;
  }
}

/** "Ask the agent for a checklist" for a session. Resolves whether the server
 * took it, so a "No checklist yet" banner stays up (to try again) when not. */
export async function writePlanFor(target: string): Promise<boolean> {
  try {
    const r = await api<{ plan: string; existing: boolean }>(
      "/api/instances/" + encodeURIComponent(target) + "/test-plan",
      { method: "POST" }
    );
    // `r.plan` names the checklist that already exists, which is not always
    // this session: a branch open in two windows has ONE checklist between
    // them, filed under whichever window got there first. Saying "<target>
    // already has a checklist" would send the user looking for a row that is
    // there under another name.
    toast(
      r.existing
        ? (r.plan && r.plan !== target
            ? target + "'s branch already has behaviors — they're under " + r.plan
            : target + " already has behaviors") + " — they're on its route in the Sky"
        // "up to three minutes", matching the row this press creates and the
        // server's own budget. It used to promise "a minute" 200px above a row
        // saying three, which makes the faster of the two promises a broken one.
        : "Asked the agent to write the behaviors for " + target + " — they'll appear on its route in the Sky (up to three minutes)"
    );
    refreshTestPlans();
    return true;
  } catch (err) {
    errorPop("Couldn't write the behaviors", errMsg(err));
    return false;
  }
}

/** The Behaviors board's "Check another branch…" (docs/behaviors.md §3): ask
 * the agent for behaviors for a branch no session has been on, or (`empty`)
 * start an empty set to write by hand. Resolves to the plan id ("" when the
 * server refused — the reason is popped). Behaviors words, unlike
 * `writePlanForBranch` (the dialog's, kept until it retires). */
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
