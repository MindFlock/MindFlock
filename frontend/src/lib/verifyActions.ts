/** Checklist (test-plan) actions behind the Verify dialog — the requests a row,
 * a step, the by-hand writer and the bulk bar send, with the cache patching,
 * refreshes, toasts and error cards that go with each (the Verify twin of
 * lib/sessionActions.ts).
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
  TestPlan,
  TestPlansResponse,
  TestStep,
  TestStepActor,
  TestStepResult,
} from "../api/types";
import { groupPlans, isWaitingOnYou, planName } from "../components/dialogs/verify";
import { queryClient, refreshInstances, refreshTestPlans } from "../state/queries";
import { useUi } from "../state/store";
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
  // row could be overwritten by a GET that left before the POST. Every other
  // mutating path here escapes only by accident: they follow the patch with
  // `refreshTestPlans()`, whose `invalidateQueries` defaults to
  // `cancelRefetch: true` and silently cancels the stale request. Rewrite
  // deliberately does not refetch (the plan is mid-generation), so it was the
  // one that snapped back — to a row offering "Run with an agent" on a plan the
  // model was still writing. Unawaited on purpose: the cancel is synchronous
  // where it matters, and nothing here needs its promise.
  void queryClient.cancelQueries({ queryKey: ["test-plans"] });
  queryClient.setQueryData<TestPlansResponse>(["test-plans"], (prev) =>
    prev ? { ...prev, plans: prev.plans.map((p) => (p.id === id ? apply(p) : p)) } : prev
  );
}

/** The bare run request — the row's Run, the per-step Re-check and the bulk
 * bar's Run all send exactly this. `only` scopes it to those step ids. */
export function requestPlanRun(planId: string, only?: string[]) {
  return api<{ session?: string; plan?: TestPlan }>(planPath(planId) + "/run", {
    method: "POST",
    json: only ? { steps: only } : {},
  });
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
    toast("Starting an agent on " + planName(plan) + " in " + (r?.session || "a new session"));
  } catch (err) {
    errorPop("Couldn't start the run for " + planName(plan), errMsg(err));
    // The optimistic state above never landed on a failure, but the plan may
    // have moved for another reason (it 409s when a run is already going), so
    // take the server's word for where it actually is.
    void refreshTestPlans();
  }
}

/** Ask for a new checklist, optionally saying what the last one got wrong.
 * True when the server accepted it.
 *
 * NO `confirm()`. This used to open a native OS modal from inside an
 * `aria-modal` dialog — a second window, painted by the browser rather than by
 * this app, whose only choices were OK and Cancel — and it was skipped
 * entirely when nothing had been answered yet, so the one press that could
 * lose work was the one that asked, and the common press asked nothing.
 *
 * Worse, it threw away the highest-signal input in the building. The person
 * pressing Rewrite has just READ the weak checklist and knows exactly what
 * should have been checked instead; the old route took no body at all, so a
 * second press re-ran the identical prompt and hoped for a different answer.
 * The composer replaces both: the warning is a line of text next to the box
 * (and only when there is something to lose — see `rewriteWarning`), and the
 * box is where the correction goes. */
export async function regeneratePlan(plan: TestPlan, focus = ""): Promise<boolean> {
  try {
    await api(planPath(plan.id) + "/regenerate", {
      method: "POST",
      json: { focus: focus.trim() },
    });
    // 202 means accepted, not finished — the headless one-shot runs in a
    // background thread for up to three minutes. "generating" is therefore
    // true from this moment, and showing it immediately is what stops a
    // second press while nothing visibly changes.
    //
    // Deliberately NOT followed by an invalidation: a re-read on this tick
    // would race the server's own write and could hand back the pre-request
    // state, flicking the row back to what it was. The truth arrives on its
    // own — `session.test_plan_ready` bridges straight to a refresh
    // (state/queries.ts), and the 10s poll is the backstop.
    // `gen_started` moves with the state, and it has to: the row calls a
    // `generating` plan stalled once its stamp is old enough, so patching the
    // state without the clock would put this plan straight back into "writing
    // stopped part-way" — the row it was just rescued from.
    patchPlan(plan.id, (p) => ({
      ...p,
      state: "generating",
      error: "",
      gen_started: Date.now() / 1000,
    }));
    toast("Rewriting the checklist for " + planName(plan) + " — up to three minutes");
    return true;
  } catch (err) {
    errorPop("Couldn't rewrite " + planName(plan), errMsg(err));
    void refreshTestPlans();
    return false;
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
    toast(r?.session ? "Stopped " + r.session : "Run cancelled");
  } catch (err) {
    errorPop("Couldn't cancel the run", errMsg(err));
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

/** Record (or, with result "", take back) an answer to one step. Resolves when
 * the server has taken it; rejects with its message — the step row reports it. */
export async function recordStepAnswer(
  plan: TestPlan,
  step: TestStep,
  result: TestStepResult,
  note: string
): Promise<void> {
  // "Was this plan asking me for something, and has it stopped?" — asked of
  // the badge's own predicate rather than of the open-step count, so it also
  // covers the plan nobody ever ran: `openHumanSteps` requires a run, so a
  // shipped checklist answered entirely by hand went from asking to answered
  // with the count at 0 on both sides, and the one plan whose completion was
  // most entirely the user's doing was the one that got no acknowledgement.
  const wasAsking = isWaitingOnYou(plan);
  const answer = await api<{ plan?: TestPlan }>(planPath(plan.id) + "/result", {
    json: { step_id: step.id, result, note },
  });
  const next = answer?.plan;
  if (next) patchPlan(plan.id, () => next);
  // THE COMPLETION MOMENT, and the surface had none. You worked down a
  // checklist, gave the last answer, and nothing said so: the row simply
  // restyled itself and — a moment later — left for another group, which
  // reads as the thing you were working on vanishing rather than as finishing
  // it. The toast is the receipt, and it names where the row went so the
  // disappearance is an outcome instead of a glitch.
  if (next && wasAsking && !isWaitingOnYou(next)) {
    // Where it lands, asked of the SAME rule that will file it there, rather
    // than assumed to be "Checked" — a plan whose last answer was a Fail moves
    // to "Steps failed", and a toast that named the wrong group would be one
    // more thing on this surface that does not match what is on screen.
    const dest = groupPlans([next])[0]?.label || "";
    toast(
      "All your steps are answered — " +
        planName(plan) +
        (dest ? " moves to " + dest : ""),
    );
  }
  void refreshTestPlans();
}

/** Change one step's wording or who answers it. */
export async function editPlanStep(
  plan: TestPlan,
  step: TestStep,
  fields: Partial<TestStep>
): Promise<void> {
  try {
    const r = await api<{ plan?: TestPlan }>(
      planPath(plan.id) +
        "/steps/" +
        encodeURIComponent(step.id),
      { method: "PATCH", json: fields }
    );
    if (r?.plan) patchPlan(plan.id, () => r.plan as TestPlan);
    void refreshTestPlans();
  } catch (err) {
    errorPop("Couldn't change that step", errMsg(err));
  }
}

/** Delete a step a person added (a generated one has no such button — see
 * `test_plans.remove_step`). */
export async function removePlanStep(plan: TestPlan, step: TestStep): Promise<void> {
  try {
    const r = await api<{ plan?: TestPlan }>(
      planPath(plan.id) + "/steps/" + encodeURIComponent(step.id),
      { method: "DELETE" }
    );
    if (r?.plan) patchPlan(plan.id, () => r.plan as TestPlan);
    void refreshTestPlans();
  } catch (err) {
    errorPop("Couldn't remove that step", errMsg(err));
  }
}

/** Add a step of your own. Resolves to the server's copy of the plan (or
 * undefined); rejects with its message — the composer keeps its text then. */
export async function addPlanStep(
  plan: TestPlan,
  fields: { text: string; expect: string; actor: TestStepActor }
): Promise<TestPlan | undefined> {
  const r = await api<{ plan?: TestPlan }>(planPath(plan.id) + "/steps", {
    method: "POST",
    json: fields,
  });
  return r?.plan;
}

/** Ask for exactly one checklist, by session title (the by-hand writer).
 *
 * 202 means a model call is now running for up to three minutes. Say so: the
 * plan appears immediately as "writing the plan", and a user who was not told
 * would read that quiet row as the button having failed. */
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
        ? branch + " already has a checklist — it's in Verify"
        : "Asked the agent for a checklist for " + branch + " — it'll appear in Verify (up to three minutes)"
    );
    refreshTestPlans();
    return true;
  } catch (err) {
    errorPop("Couldn't write a checklist", errMsg(err));
    return false;
  }
}

export async function writePlanFor(target: string): Promise<void> {
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
            ? target + "'s branch already has a checklist — it's under " + r.plan
            : target + " already has a checklist") + " — it's in Verify"
        // "up to three minutes", matching the row this press creates and the
        // server's own budget. It used to promise "a minute" 200px above a row
        // saying three, which makes the faster of the two promises a broken one.
        : "Asked the agent for a checklist for " + target + " — it'll appear in Verify (up to three minutes)"
    );
    refreshTestPlans();
  } catch (err) {
    errorPop("Couldn't write a checklist", errMsg(err));
  }
}
