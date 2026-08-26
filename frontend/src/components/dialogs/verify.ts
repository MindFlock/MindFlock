/** The decision rules behind the Verify surface: what a plan's run says, and
 * which plans are actually asking something of you.
 *
 * Verify exists because the pipeline's last honest checkpoint is "the PR
 * merged". Merged is not verified — nobody has opened the thing and looked at
 * it. So every session that lands on the live branch gets a generated test
 * plan, the agent runs the steps it can run from a shell, and whatever needs a
 * pair of eyes is handed back as a short list. This module holds the rules that
 * decide which of those states a plan is in.
 *
 * It is kept pure and separate from the dialog for the same reason
 * ``intake/queue.ts`` is: the top-bar badge, the first group's heading, every
 * row's sentence and every row's button are four renderings of one question
 * ("what has nobody checked?"), and they must never be able to disagree. A
 * count computed in the TopBar and a filter computed in the dialog drift the
 * first time either is edited; one exported rule, unit-tested against plain
 * fixtures, cannot.
 *
 * The dependency runs one way and must keep running one way:
 *
 *     isWaitingOnYou  →  dueCount (the badge)
 *                     →  planGroup → groupPlans (the headings)
 *                     →  planStatus (the sentence, the tone, the button)
 *
 * Deriving the badge from whatever the list happened to render would invert it,
 * and the badge is the thing that has to be true when the dialog is shut.
 *
 * Two judgement calls worth knowing before reading the code:
 *
 * - The verdict is RECOMPUTED from the run's step results rather than read off
 *   the ``verdict`` the backend stamped on the run. The stamp is a snapshot of
 *   the moment the agent finished; a human then confirms the steps the agent
 *   left blocked, one at a time, through ``POST /api/test-plans/{id}/result``.
 *   Trusting the stamp would leave a fully-confirmed plan reading "partial"
 *   forever, and would show a red "fail" next to steps that have since passed.
 * - "No result yet" and an AGENT's "blocked" are the same thing to every rule
 *   here. The run prompt tells the agent to mark human-only steps ``blocked``
 *   with a reason, but an agent that dies halfway, or a plan whose steps were
 *   regenerated after the run, leaves them simply absent. Both mean "unchecked",
 *   and treating an absent result as a pass is the one answer that would make
 *   the whole surface a lie.
 * - A PERSON's "blocked" — the button says "Can't check" — is not that. It is
 *   somebody saying "I looked, I could not get to it, here is why", which is an
 *   answer even though it is not an outcome. :func:`isYourAnswer` is the one
 *   rule that knows the difference, and it is deliberately confined to the
 *   question "is this still asking something of you?". See it for why the
 *   surface was previously impossible to clear honestly.
 */

import type {
  Instance,
  TestPlan,
  TestRun,
  TestStep,
  TestStepResult,
  TestStepResultEntry,
} from "../../api/types";
// The list dialogs' shared matcher (Recently closed, Workspaces on disk): every
// whitespace-separated token has to appear somewhere in the row, so "sitecheck
// grafana" narrows without anybody having to remember the separator. Verify is
// the third list to grow a filter and it must not be the one that behaves
// differently.
import { matchesTokens } from "../../lib/rowSearch";

/** Whether a step result is a settled answer.
 *
 * Only "pass" and "fail" are settled. "blocked" is the agent saying "a person
 * has to do this one", and "" is the store's not-yet placeholder — neither is
 * an outcome, and both are what the Verify surface exists to chase down. */
function isFinal(result: TestStepResult | undefined): boolean {
  return result === "pass" || result === "fail";
}

/** Whether a PERSON has answered this step — the rule that lets the surface be
 * cleared without lying.
 *
 * "Blocked" is two different sentences depending on who said it, and until this
 * existed the surface only understood one of them:
 *
 * - THE AGENT's blocked means "not mine — hand it to a person". Correctly
 *   unchecked, and every other rule in this file still reads it that way.
 * - YOUR blocked — the button says "Can't check" — means "I looked, I could not
 *   get to it, and here is why". Staging was down; you have no account on the
 *   thing; the customer never called back. That is an answer. It is not an
 *   OUTCOME, which is a different question and one :func:`verdictOf` still
 *   answers honestly.
 *
 * WHY IT MATTERS MORE THAN IT LOOKS. The legend teaches "Can't check" as the
 * honest answer, and the whole feature rests on people giving honest answers.
 * But a blocked step kept the plan in :func:`isWaitingOnYou` forever: the row
 * stayed in the badge's group, the top bar kept its number, and no amount of
 * truthful clicking made either move. The only two exits were to press Pass on
 * something nobody had checked, or to delete the checklist and its history. A
 * surface whose entire premise is "nobody claims to have observed what they
 * could not observe" was cornering its users into precisely that claim.
 *
 * Deliberately NOT used by `verdictOf`, `failCount` or `unansweredCount`. A
 * can't-check is still not a pass, so nothing here can make the surface say the
 * thing works — :func:`planStatus` states the two counts side by side instead of
 * averaging them into one cheerful sentence. */
export function isYourAnswer(result: TestStepResult | undefined, by = ""): boolean {
  return isFinal(result) || (result === "blocked" && by === "human");
}

/** :func:`isYourAnswer` for a stored entry, with "no entry at all" meaning no. */
function answeredByYou(entry: TestStepResultEntry | undefined): boolean {
  return isYourAnswer(entry?.result, entry?.by);
}

/** An agent tried this step and HANDED IT BACK: `blocked`, recorded by the run.
 *
 * The other half of `isYourAnswer`'s rule, named. A person's "Can't check" and
 * an agent's "blocked" are the same wire value and opposite events — one closes
 * the step, the other reassigns it — so every rule that touches `blocked` has
 * to say which one it means, and saying it by repeating `by === "human"` at
 * each site is how the two drifted apart in the first place. */
export function handedBack(entry: TestStepResultEntry | null | undefined): boolean {
  return entry?.result === "blocked" && entry?.by !== "human";
}

/** WHOSE STEP THIS IS NOW — which is not always whose it was written to be.
 *
 * `step.actor` is what the model GUESSED when it wrote the checklist: "an agent
 * can settle this from a shell". A verify run is where that guess meets the
 * world, and when the agent comes back with `blocked` it is saying the guess
 * was wrong — the step needs a person. From that moment the step is yours, and
 * every count on the surface has to agree, because the person reading is the
 * one who now owes it.
 *
 * It used not to. A run that handed back six of its own steps left the row
 * saying "2 steps need your eyes" over a body whose tally, roll-up and every
 * one of the eight marks said eight — `stepCheck` had this rule and
 * `openHumanSteps` did not, so the loudest sentence on the card was the one
 * number that was wrong. One rule, both callers.
 *
 * DERIVED, never stored. The blocker is usually the world and not the step —
 * "the commit isn't deployed where I could observe it" is the commonest reason
 * an agent hands one back — so a re-run after the deploy should have its go at
 * it again. Rewriting `actor` on the plan would make the first agent's bad
 * afternoon permanent and quietly shrink every future run to the steps that
 * happened to work once. */
export function stepIsYours(plan: TestPlan, step: TestStep): boolean {
  return step.actor === "human" || handedBack(stepResult(plan, step.id));
}

/** Whether a run record has anything in it at all.
 *
 * A phantom run is one nobody has actually made. ``record_result`` opens a run
 * to have somewhere to put the first answer, so answering one step and then
 * clicking that answer OFF again — which the step buttons allow, on purpose —
 * leaves a run behind whose every result is ``""``. Nothing was checked, but
 * the record existed, so the plan wore a "partial" chip, claimed a "last run",
 * and sat in "Awaiting your confirmation" over work that had been withdrawn.
 * Partial means "run, and something is unanswered"; this is "not run".
 *
 * A run with a SESSION is never a phantom even with no results: that is an
 * agent working the plan right now, and the empty results are simply the ones
 * it has not written yet. */
function isPhantomRun(run: TestRun): boolean {
  if (run.session) return false;
  const results = run.results || {};
  return !Object.keys(results).some((id) => !!results[id]?.result);
}

/** The run whose results the surface should show.
 *
 * ``finish_run`` appends, and the store caps the list at MAX_RUNS newest-first,
 * so the last element is normally the one. It is chosen by timestamp anyway:
 * a hand-edited ``test_plans.json`` or a re-run recorded out of order would
 * otherwise silently show a stale verdict, and ties fall back to append order
 * (``>=``) so runs written in the same second still resolve to the later one.
 *
 * Phantom runs are skipped entirely (see :func:`isPhantomRun`), which is what
 * makes every rule below agree that an un-answered plan has not been run. Note
 * that a later answer lands back in that same record — ``record_result`` writes
 * into the newest run — so the plan reappears the moment there is anything real
 * to report. */
export function latestRun(plan: TestPlan): TestRun | null {
  const runs = plan.runs || [];
  let best: TestRun | null = null;
  for (const run of runs) {
    if (!run || isPhantomRun(run)) continue;
    if (!best || (run.at || 0) >= (best.at || 0)) best = run;
  }
  return best;
}

/** What the latest run recorded for one step, or null if it recorded nothing.
 *
 * An entry with ``result: ""`` is returned rather than nulled out: the store
 * writes that placeholder when a run touched a step without settling it, and
 * the note attached to it ("needs a real browser") is exactly what the row
 * wants to render. Callers asking "is this done?" ask about ``result``, not
 * about the presence of the entry. */
export function stepResult(
  plan: TestPlan,
  stepId: string,
): TestStepResultEntry | null {
  const run = latestRun(plan);
  if (!run) return null;
  return run.results?.[stepId] ?? null;
}

/** The plan's overall answer, from the latest run's results.
 *
 * "none" is its own value rather than a flavour of "partial" because the two
 * mean opposite things to a reader: "none" is "this has never been run", which
 * is a normal state for a plan that only just went live, while "partial" is
 * "it was run and something is still unanswered", which is a to-do.
 *
 * The step list, not the results map, is the authority for what had to be
 * checked. A run that reported five results for a plan that has eight steps has
 * left three unchecked, and reading only the map would call that a pass. When
 * the plan has no steps at all (a regeneration that emptied them, say) the map
 * is all there is to go on, and a plan with neither is "partial" — nothing was
 * verified, so claiming a pass would be inventing one. */
export function verdictOf(plan: TestPlan): "pass" | "fail" | "partial" | "none" {
  const run = latestRun(plan);
  if (!run) return "none";
  const results = run.results || {};
  const ids = answerableIds(plan, run);
  if (!ids.length) return "partial";
  let unsettled = false;
  for (const id of ids) {
    const result = results[id]?.result;
    if (result === "fail") return "fail";
    if (!isFinal(result)) unsettled = true;
  }
  return unsettled ? "partial" : "pass";
}

/** The step ids one run had to answer.
 *
 * The step LIST is the authority, not the results map: a run that reported five
 * results for an eight-step plan left three unchecked, and reading only the map
 * would call that a pass. The map is the fallback for the one case where the
 * list is gone — a regeneration that emptied the steps out from under a recorded
 * run — because it is then the only evidence there is.
 *
 * Shared by verdictOf, failCount and unansweredCount so the three cannot
 * disagree about what "every step" meant. */
function answerableIds(plan: TestPlan, run: TestRun): string[] {
  const steps = plan.steps || [];
  return steps.length ? steps.map((s) => s.id) : Object.keys(run.results || {});
}

/** The steps only a person can settle, that nobody has settled yet.
 *
 * This is the rule that turns an agent's run into a human to-do. The generator
 * tags a step ``human`` only for things a shell genuinely cannot judge — how a
 * screen looks, a real browser, an external service — and the run prompt tells
 * the agent to leave exactly those blocked. So a run that finished with human
 * steps still unsettled has not failed and has not passed: it has handed you a
 * short list.
 *
 * It requires a run. A plan that has never been run is waiting on the agent (or
 * on going live), not on you, and calling that "waiting on you" would put work
 * in your lap that nobody has started.
 *
 * A step you marked "Can't check" is NOT in here — see :func:`isYourAnswer`.
 * You answered it; the plan stops asking. Whether anything was actually
 * verified is `verdictOf`'s question, and it still says no.
 *
 * Returns the STEPS rather than a count because the row needs both: the count
 * goes on the button ("Answer 2 steps") and the first of them is what the button
 * scrolls to. */
export function openHumanSteps(plan: TestPlan): TestStep[] {
  const run = latestRun(plan);
  if (!run) return [];
  const results = run.results || {};
  return (plan.steps || []).filter(
    (step) => stepIsYours(plan, step) && !answeredByYou(results[step.id]),
  );
}

/** Whether this plan is waiting on a person — see :func:`openHumanSteps`.
 *
 * One implementation, two names: this predicate is pinned by tests and read by
 * the grouping, and the list behind it is read by the row. Re-deriving either
 * from the plan separately is how they drift. */
export function needsConfirmation(plan: TestPlan): boolean {
  return openHumanSteps(plan).length > 0;
}

/** Whether every step of the plan's newest run has an answer.
 *
 * The mirror of the server's ``_all_settled``, and it exists here for the same
 * reason it exists there: it is what a rewrite of a SHIPPED plan resolves to
 * when the model answers — ``done`` if everything was settled, ``due`` if not —
 * so the grouping can file the plan where it is headed instead of dropping it
 * a rung for the minutes the rewrite takes. Same settled rule as the server's
 * (:func:`isYourAnswer`): a person's "Can't check" is an answer, an agent's
 * blocked is a handover. A plan with no steps or no real run has settled
 * nothing. */
function allSettled(plan: TestPlan): boolean {
  const run = latestRun(plan);
  const steps = plan.steps || [];
  if (!run || !steps.length) return false;
  const results = run.results || {};
  return steps.every((s) => answeredByYou(results[s.id]));
}

/** How many steps the latest run recorded as an outright failure.
 *
 * Zero before anything has run — an unchecked step is not a failed one, and the
 * whole surface rests on never confusing the two. Uses the same id authority
 * verdictOf does, so ``failCount(plan) > 0`` and ``verdictOf(plan) === "fail"``
 * cannot answer differently. */
export function failCount(plan: TestPlan): number {
  const run = latestRun(plan);
  if (!run) return 0;
  const results = run.results || {};
  return answerableIds(plan, run).filter((id) => results[id]?.result === "fail").length;
}

/** How many steps the latest run left without an answer.
 *
 * "blocked" and "" count the same, for the reason at the top of this file: one
 * is the agent saying a person has to do this, the other is a run that never
 * reached the step, and treating either as a pass would make the surface a lie.
 * Zero before anything has run. */
export function unansweredCount(plan: TestPlan): number {
  const run = latestRun(plan);
  if (!run) return 0;
  const results = run.results || {};
  return answerableIds(plan, run).filter((id) => !isFinal(results[id]?.result)).length;
}

/** How many steps a PERSON marked "Can't check" — see :func:`isYourAnswer`.
 *
 * The other half of `unansweredCount`, and it exists because the two must be
 * said separately rather than summed. Both are steps with no outcome, so both
 * are in that count; but one is "nobody got to this" and the other is
 * "somebody went and looked and could not". Reporting them as one number is how
 * the surface ends up telling a person who answered every step of their
 * checklist that three of them "never got an answer" — the exact insult that
 * makes people stop giving the honest answer.
 *
 * Always ≤ `unansweredCount`, by construction: a blocked result is never final.
 * Zero before anything has run. */
export function cantCheckCount(plan: TestPlan): number {
  const run = latestRun(plan);
  if (!run) return 0;
  const results = run.results || {};
  return answerableIds(plan, run).filter((id) => {
    const entry = results[id];
    return entry?.result === "blocked" && entry.by === "human";
  }).length;
}

/** Whether this plan is asking something of the person reading.
 *
 * THE predicate of the surface. The top-bar badge, the "Not checked yet" group
 * and every row's tone are three renderings of this one answer, and they agree
 * because they all call this rather than each re-deriving it — the previous
 * arrangement printed one number in four places and the four never matched.
 *
 * It is named for the reader's TURN and labelled for the FACT, and the gap
 * between those two is deliberate: "shipped, and nobody has finished checking
 * it" is what every member has in common, while only some of them literally
 * want the reader's hands right now. The heading says the fact; the row's own
 * sentence says whose turn it is.
 *
 * Everything that is not literally asking for a person's attention is kept out,
 * because a badge that over-counts stops being read within a day, and once it is
 * wallpaper the feature is dead:
 *
 * - ``generating`` / ``generated`` — the plan isn't yours yet. One is still
 *   being written; the other is waiting for the branch to reach the live
 *   branch. Nothing to check until it ships. The EXCEPTION is a rewrite of a
 *   plan that has ALREADY shipped (``generating`` with ``live_at`` set): the
 *   work is live and unchecked the whole time the model is writing, and the
 *   server puts it straight back into ``due`` when the answer lands — dropping
 *   it from the badge for those minutes would read as the work un-shipping,
 *   which is a lie the reader can see through. Same argument as ``running``
 *   below.
 * - ``done`` with nothing unconfirmed — that is the point of the feature.
 * - ``failed`` — the generator couldn't produce a plan. That is a defect to
 *   look at in the list, not a shipped thing awaiting verification, and a badge
 *   that counted it would nag about something no amount of checking clears.
 *
 * What is left is ``due`` (live, unrun), ``running`` (live, the agent is mid-run
 * — still nothing checked, and a badge that dropped to zero for the minutes a
 * run takes and then popped back up would read as a bug), and any plan whose
 * human steps are still unsettled regardless of state, which is the ``done``
 * plan that came back with a list for you.
 *
 * A recorded FAIL is deliberately NOT in here. A fail is an answer, not an ask —
 * somebody looked and it did not work — so it gets a loud red group of its own
 * second from the top rather than a number on the top bar that no amount of
 * checking would ever clear. */
export function isWaitingOnYou(plan: TestPlan): boolean {
  return (
    plan.state === "due" ||
    plan.state === "running" ||
    // A shipped plan being rewritten. The rewrite resolves to ``due`` unless
    // every step was already settled (the server's ``_generate_inner`` mirrors
    // this exact expression), so the badge holds steady across it.
    (plan.state === "generating" && !!plan.live_at && !allSettled(plan)) ||
    needsConfirmation(plan)
  );
}

/** What the top-bar badge shows: how many plans are asking something of you.
 *
 * Each plan counts at most once — a due plan whose earlier run left human steps
 * open matches both halves of :func:`isWaitingOnYou`, and counting it twice
 * would make the badge exceed the number of rows the dialog can show. */
export function dueCount(plans: TestPlan[]): number {
  return (plans || []).filter(isWaitingOnYou).length;
}

/** Whether the one branch in the header is the whole story.
 *
 * The header names a live branch, and that branch is the FLOCK-WIDE default:
 * `GET /api/test-plans` resolves it with no repo in hand, so it deliberately
 * skips the per-repo link. Stating it as a fact about everything would
 * contradict the repo card two folds below ("Acme/App · live: staging") and the
 * chips on the rows, which is what the "+ per-repo" admission exists to
 * prevent — and why it must be true whenever a different answer exists
 * anywhere on the surface.
 *
 * BOTH SOURCES, UNIONED, because either can hold an answer the other has never
 * heard of:
 *
 * - The PLANS: a plan carries the branch it was actually stamped with, and a
 *   plan outlives its repo's membership in the list — a plan written before
 *   somebody removed the repo still waits on the branch it recorded.
 * - The CONFIGURED repos: writing a plan costs a model call and only happens on
 *   a push, so a repo added and given `staging` this minute has NO plans yet.
 *   That is the normal state right after configuring one, it can last
 *   indefinitely, and it is exactly when the header would otherwise state
 *   `main` as fact six lines above a card reading `staging`.
 *
 * Only blocks for repos on `trackedRepos` count. An override for a repo that is
 * not tracked does nothing at all (the backend reads tracking off the list
 * alone), and the dialog keeps such a block on purpose so a re-added repo comes
 * back with the branch the user typed — so counting it here would put a
 * "+ per-repo" on a header describing repos that have all been removed.
 * Compared case-insensitively for the same reason the backend does it: GitHub
 * slugs are case-preserving but not case-sensitive. */
export function liveBranchOverridden(
  liveBranch: string,
  plans: TestPlan[],
  trackedRepos?: string[],
  blocks?: Record<string, { live_branch?: string } | undefined>,
): boolean {
  if (
    (plans || []).some(
      (plan) => plan.effective_live_branch && plan.effective_live_branch !== liveBranch,
    )
  )
    return true;
  const tracked = new Set(
    (trackedRepos || []).map((slug) => String(slug || "").trim().toLowerCase()),
  );
  return Object.entries(blocks || {}).some(([slug, block]) => {
    const own = String(block?.live_branch || "").trim();
    return (
      !!own && own !== liveBranch && tracked.has(String(slug || "").trim().toLowerCase())
    );
  });
}

/** The sessions a plan can be written for right now, in list order.
 *
 * A session qualifies when it has a branch (there is no diff to read a plan out
 * of before that — `Instance.branch` is "" until the session starts) and does
 * not already have one. A session that already carries a plan is not an error
 * to explain, it is simply not a candidate: its plan is in the list below,
 * which is a better answer than a disabled row with a tooltip.
 *
 * Exported rather than left inside the picker because the EMPTY STATE has to
 * ask the same question. "Pick a session above and press Write plan" is the
 * first-run instruction, and the picker it points at renders nothing when this
 * list is empty — which is precisely the fresh install where that paragraph is
 * read. One rule, so the instruction cannot describe furniture that is not on
 * the screen. */
/** The one thing this needs off a closed session: its name and its branch. */
export interface ClosedTarget {
  title: string;
  branch?: string;
}

/** Sessions a checklist could be written for — OPEN OR RECENTLY CLOSED.
 *
 * WHY CLOSED ONES BELONG HERE. Every other part of this feature is built on a
 * checklist outliving its session: the plan stores the main repo rather than the
 * worktree precisely because the worktree is reclaimed, and a plan can be
 * rewritten and run months after the work merged. Creation was the one half that
 * still demanded a live window — so "write me a checklist for that" stopped
 * being possible at exactly the moment people reach for it, which is after the
 * work is done and the window has been put away.
 *
 * Open sessions come first: they are the ones on screen, and the closed list is
 * fifty entries deep. A title is offered once even if it appears in both (a
 * session reopened under the same name), because the thing being named is the
 * session, not the window.
 */
export function planTargets(
  instances: Instance[],
  plans: TestPlan[],
  closed: ClosedTarget[] = [],
): string[] {
  const havePlans = new Set((plans || []).map((plan) => plan.id));
  const out: string[] = [];
  const seen = new Set<string>();
  const take = (title: string, branch: string | undefined) => {
    if (!title || !branch || havePlans.has(title) || seen.has(title)) return;
    seen.add(title);
    out.push(title);
  };
  for (const inst of instances || []) take(inst.title, inst.branch);
  for (const entry of closed || []) take(entry.title, entry.branch);
  return out;
}

/** Which of those titles is a closed session — the picker labels them, because
 * "no window will open" is worth knowing before you press the button. */
export function closedTargets(
  instances: Instance[],
  closed: ClosedTarget[] = [],
): Set<string> {
  const live = new Set((instances || []).map((i) => i.title));
  return new Set(
    (closed || []).map((e) => e.title).filter((t) => t && !live.has(t)),
  );
}

/** Why :func:`planTargets` came back empty — one sentence, or "" when it didn't.
 *
 * Three unlike situations used to share one sentence ("Start a session first"),
 * which is simply false for the user with five sessions who is being told to
 * start a sixth. The three are genuinely different problems with different next
 * moves, and the whole cost of telling them apart is this function.
 *
 * Lives here rather than in the picker because the answer is a fact about the
 * data, and because a rule with three branches is worth pinning with a test. */
export function noTargetsReason(
  instances: Instance[],
  plans: TestPlan[],
  closed: ClosedTarget[] = [],
): string {
  if (planTargets(instances, plans, closed).length) return "";
  const list = [...(instances || []), ...(closed || [])];
  if (!list.length)
    return "Start a session first — a checklist is written from a branch's diff.";
  if (!list.some((inst) => inst.branch))
    return "No session has a branch yet — a checklist is written from a branch's diff.";
  return "Every session with a branch already has a checklist — they're in the list below.";
}

/** The groups, in the order the dialog draws them.
 *
 * Fixed order, most-urgent first, so the list reads top-down as a work queue
 * rather than as a database dump. Labels are plain English about what the
 * reader has to do, not the state names: "generated" tells you nothing, whereas
 * "Not live yet" says why it isn't your problem yet.
 *
 * The first group is the badge. Its membership predicate IS
 * :func:`isWaitingOnYou`, which is what ``dueCount`` filters by, so the number
 * on the top bar and the number on that heading are the same array length by
 * construction. They used to be four separate numbers ("Due now",
 * "Awaiting your confirmation", the Due tab's count, the badge) that were never
 * equal to each other, which taught the reader that the badge was approximate.
 * The distinction the old split carried survives where it costs nothing: the
 * row's own sentence and button say whether nobody has started this yet or the
 * agent has left you a short list.
 *
 * TWO KEYS ONE LETTER APART, on purpose. ``failed`` is the plan the generator
 * could not write; ``fail`` is the shipped thing that flunked its own checks —
 * opposite ends of the feature. ``failed`` keeps its historical spelling
 * because ``mf_verify_groups`` persists these keys as collapse exceptions and
 * renaming one silently resets everybody's saved state. */
const GROUPS: Array<{ key: string; label: string }> = [
  // "Not checked yet", not "Waiting on you": the group holds three unlike
  // situations — nobody has pressed Run, an agent is running it right now, and
  // some steps genuinely need your eyes — and only the third is literally
  // waiting on the reader. A heading that named the person while the row under
  // it said "an agent is checking the steps it can" was a contradiction the
  // reader had to resolve, and the natural resolution is "this count is
  // approximate", which is how a badge dies. What is true of every member is
  // that it shipped and nobody has finished checking it, so that is the name.
  { key: "due", label: "Not checked yet" },
  // "Steps failed", not "Failed the check": in a repo this reads next to a PR's
  // CI checks, and a heading with that shape sent people to GitHub to look for
  // a red tick. This is about steps in a checklist, and it says so.
  { key: "fail", label: "Steps failed" },
  { key: "generated", label: "Not shipped yet" },
  { key: "done", label: "Checked" },
  // "Couldn't be written", not "No plan": every row in this group IS a plan, so
  // the old heading contradicted its own contents. The defect is that the model
  // could not produce steps for it.
  { key: "failed", label: "Couldn't be written" },
];

/** Which group one plan belongs to.
 *
 * Asking something of you wins over everything else, because that is the pile
 * you came to pick up. A recorded failure comes next: ``finish_run`` writes
 * ``state = "done"`` unconditionally, so without this a shipped thing that
 * flunked its own plan filed under "Checked" — the single most valuable output
 * of the feature, hidden in the pile of successes.
 *
 * ``running`` files with the due ones because it is a due plan with an agent
 * currently working it, and ``generating`` files with ``generated`` because both
 * are pre-live — dropping either from the grouping would make plans vanish from
 * the list exactly while they were at their most interesting. The one exception
 * is a rewrite of a plan that has already SHIPPED: filing that under "Not
 * shipped yet" for the minutes the model takes told the reader their live work
 * had somehow un-shipped, so it stays where the server will put it back —
 * ``due`` via :func:`isWaitingOnYou` while anything is unsettled, "Checked"
 * when every step already had an answer. The trailing fallback covers a state
 * written by a newer server than this bundle: unknown is shown somewhere
 * harmless rather than silently dropped. */
export function planGroup(plan: TestPlan): string {
  if (isWaitingOnYou(plan)) return "due";
  if (plan.state === "failed") return "failed";
  if (verdictOf(plan) === "fail") return "fail";
  if (plan.state === "done") return "done";
  // Reached in ``generating`` only when the plan shipped AND everything was
  // settled (anything unsettled is caught by isWaitingOnYou above), i.e. a
  // fully-answered checklist being rewritten: it was under "Checked" a second
  // ago and it lands back there when the rewrite resolves.
  if (plan.state === "generating" && plan.live_at) return "done";
  return "generated";
}

/** Does this checklist match the dialog's filter?
 *
 * Everything the row SHOWS is searchable, plus the two things it does not: the
 * repo path (which is how you find "everything from sitecheck") and the STEP
 * TEXT. Steps matter because a checklist is remembered by what it asks — "the
 * one about the Grafana collage board" — long after its ticket number has
 * stopped meaning anything, and they are the only place those words appear.
 *
 * The group's own label is matched too, so "failed" and "not shipped" narrow
 * the list to the heading a reader can already see.
 */
export function planMatches(plan: TestPlan, tokens: string[]): boolean {
  if (!tokens.length) return true;
  const fields: Array<string | null | undefined> = [
    plan.title,
    plan.id,
    plan.summary,
    plan.branch,
    plan.sha,
    plan.repo_root,
    plan.live_branch,
    GROUPS.find((g) => g.key === planGroup(plan))?.label,
  ];
  for (const step of plan.steps || []) {
    fields.push(step.text, step.expect, step.actor === "human" ? "you" : "agent");
  }
  return matchesTokens(fields, tokens);
}

/** Whether "Run with an agent" would actually do something for this plan.
 *
 * The bulk bar asks it of every selected row, because the alternative is firing
 * a request per row and reading back a wall of 409s: the run route refuses a
 * checklist that is still generating, one an agent is already working, and one
 * with no agent step in it (an agent may not settle a human step, so the
 * session would provision a workspace and hand the whole list straight back).
 */
export function canRunNow(plan: TestPlan): boolean {
  const action = planStatus(plan).action;
  return action === "run" || action === "rerun";
}

/** Group plans for the list, dropping groups that would render empty.
 *
 * Input order is preserved inside each group — ``GET /api/test-plans`` returns
 * newest ``generated_at`` first, and re-sorting here would fight whatever the
 * caller chose. The ONE exception is "Checked", which is stably partitioned so
 * the plans that never got a full answer come first: with real failures lifted
 * into their own group, the residue in there is the run that stopped early, and
 * it would otherwise be buried under every success the flock has ever recorded.
 * A stable partition keeps the caller's order inside each half.
 *
 * Empty groups are omitted rather than rendered as headers with nothing under
 * them: the common steady state is one group with one item in it, and four empty
 * headings around it would bury it. */
export function groupPlans(
  plans: TestPlan[],
): Array<{ key: string; label: string; detail?: string; plans: TestPlan[] }> {
  const buckets = new Map<string, TestPlan[]>();
  for (const plan of plans || []) {
    if (!plan) continue;
    const key = planGroup(plan);
    const bucket = buckets.get(key);
    if (bucket) bucket.push(plan);
    else buckets.set(key, [plan]);
  }
  return GROUPS.filter((g) => (buckets.get(g.key) || []).length > 0).map((g) => {
    const found = buckets.get(g.key) || [];
    // WHAT THE WHOLE PILE ADDS UP TO, on the heading. A collapsed group is a
    // number and a noun — "Not checked yet · 7" — which says how many
    // checklists are in there and nothing about how much is being asked of you:
    // seven plans might be two steps or forty, and one of them might be red.
    // The two groups that are an ASK say what the ask is, from the same tally
    // the rows and the roll-ups use.
    if (g.key === "due" || g.key === "fail") {
      const sum = found.reduce(
        (acc, p) => {
          const t = checkTally(p);
          return { yours: acc.yours + t.yours, failed: acc.failed + t.failed };
        },
        { yours: 0, failed: 0 },
      );
      const bits: string[] = [];
      if (sum.failed) bits.push(sum.failed + (sum.failed === 1 ? " step failed" : " steps failed"));
      if (sum.yours) bits.push(sum.yours + (sum.yours === 1 ? " step needs" : " steps need") + " you");
      return {
        key: g.key,
        label: g.label,
        detail: bits.length ? bits.join(" · ") : undefined,
        plans: found,
      };
    }
    if (g.key !== "done") return { key: g.key, label: g.label, plans: found };
    const short = found.filter((p) => unansweredCount(p) > 0);
    // The two reasons a step has no outcome, counted apart and said apart — see
    // :func:`cantCheckCount`. A plan whose only gap is a step you went and
    // looked at is NOT one that "never got an answer", and filing it under that
    // wording is the badge's old lie moved up one line to the heading, where it
    // would contradict the row's own sentence directly underneath.
    const blank = short.filter((p) => unansweredCount(p) - cantCheckCount(p) > 0).length;
    const cant = short.length - blank;
    const bits: string[] = [];
    // The noun, spelled out: the two headings above this one now carry a count
    // of STEPS, and "3 never got an answer" in the same grammar reads as three
    // steps when it means three checklists.
    if (blank)
      bits.push(
        blank + (blank === 1 ? " checklist" : " checklists") + " never got an answer",
      );
    if (cant) bits.push(cant + " you couldn't check");
    return {
      key: g.key,
      label: g.label,
      // Said on the heading so a collapsed "Checked · 11" cannot hide the fact
      // that three of those eleven were never actually finished.
      detail: bits.length ? bits.join(" · ") : undefined,
      plans: [...short, ...found.filter((p) => unansweredCount(p) === 0)],
    };
  });
}

/** What one row says and what its button does.
 *
 * ``tone`` paints it, ``line`` is the whole status, ``action`` is the single
 * primary button. Everything else a plan could say lives in the ⋯ menu. */
export type PlanTone = "you" | "bad" | "warn" | "busy" | "ok" | "wait" | "broken";

/** The primary button's job. ``none`` renders no button at all — a plan that is
 * still being written, or one that has not shipped yet, is genuinely not asking
 * for a press, and a disabled button would be furniture explaining itself. */
export type PlanAction =
  | "none"
  | "run"
  | "rerun"
  | "answer"
  | "watch"
  | "rewrite";

export interface PlanStatus {
  /** The group this plan files under — from :func:`planGroup`, never recomputed
   * here, so the heading, the sentence and the button are three renderings of
   * one decision and cannot contradict each other. */
  group: string;
  tone: PlanTone;
  /** One plain-English sentence: the ONLY status on the row. */
  line: string;
  action: PlanAction;
  /** The primary button's label; "" when `action` is "none". */
  actionLabel: string;
}

/** How long a plan may say "writing…" before the row admits something is wrong.
 *
 * The mirror of ``test_plans.GENERATE_STALE_S`` (``TIMEOUT_GENERATE`` + slack),
 * and it must not be SHORTER than the server's: the server is the half that
 * actually recovers the plan, so a row that cried stalled first would offer a
 * rewrite for a generation still comfortably inside its own budget. Equal is
 * right — the moment the server would call it abandoned is the moment the user
 * should stop being told to wait. */
export const GENERATION_STALE_S = 300;

/** Whether a plan is stuck part-written rather than being written.
 *
 * WHY THE CLIENT ASKS THIS AT ALL, when the server has its own watchdog: closing
 * the app mid-generation kills the thread writing the plan, and the row it left
 * behind said "Writing the plan from the diff — up to three minutes" with no
 * button under it, forever. The server picks such plans back up on its next
 * minute, but "up to three minutes" is already a promise the row broke, and the
 * one state with no action offered is the worst place to leave somebody who has
 * been staring at it since yesterday. So the row tells the truth and hands back
 * the rewrite button; pressing it is the same call the server was about to make.
 *
 * A plan with no `gen_started` (written before the server stamped one — i.e. one
 * that is stuck right now) reads as stalled, which is exactly what it is. */
export function isGenerationStalled(plan: TestPlan, now = Date.now() / 1000): boolean {
  if (!plan || plan.state !== "generating") return false;
  return now - (plan.gen_started || 0) >= GENERATION_STALE_S;
}

/** An epoch-seconds stamp as a rough age ("4m ago").
 *
 * The dialog has `ageOf`/`agoOf` built on Intake's formatter; this is the same
 * rounding for the one sentence that is decided in here rather than rendered
 * there, so the row and the status line cannot disagree about how old something
 * is. Deliberately coarse — the reader wants "a few minutes", not a stopwatch. */
/** " (4m so far)" for a run that is under way, or "" when there is nothing
 * worth saying.
 *
 * Empty for the first minute — "just now" says nothing the press did not — and
 * empty again past a day, where the number is far more likely to be a bad stamp
 * than a fact and "(20690d so far)" makes the row look broken rather than
 * informative. Everything in between is the difference between waiting and
 * going to look: one sentence used to stand for both "you pressed this three
 * seconds ago" and "this has been wedged for an hour and fifty minutes". */
export function runElapsed(run: TestRun | null, now = Date.now() / 1000): string {
  const at = Number(run?.at || 0);
  if (!at) return "";
  const mins = Math.round((now - at) / 60);
  if (mins < 1 || mins > 60 * 24) return "";
  return " (" + agoText(at, now).replace(" ago", "") + " so far)";
}

export function agoText(epochSeconds: number, now = Date.now() / 1000): string {
  const mins = Math.max(0, Math.round((now - (epochSeconds || 0)) / 60));
  if (mins < 1) return "just now";
  if (mins < 60) return mins + "m ago";
  const h = Math.round(mins / 60);
  return h < 48 ? h + "h ago" : Math.round(h / 24) + "d ago";
}

/** The whole vocabulary of the surface, in one ordered decision.
 *
 * This replaces four chips (state, verdict, "vs branch", step count) and four
 * body paragraphs with one sentence, and it is ordered rather than switched: a
 * plan can be several of these at once, and the order is what makes "the agent
 * left you two steps" outrank "eleven steps passed". Reading the two topmost
 * conditions first also means the loud, honest answers — it failed, it is
 * waiting on you — can never be shadowed by a cheerful one below them.
 *
 * `liveBranch` names the branch in the pre-live sentence and is only ever
 * cosmetic; a plan with no branch resolved still gets a true sentence. */
/** The first sentence of a recorded failure, for the row that has to fit it.
 *
 * Every writer of `plan.error` stores a SELF-DESCRIBING first sentence — "The
 * verify session couldn't start.", "Rewriting the checklist failed." — followed
 * by the raw detail that makes it fixable (a git line, a tmux line, a timeout).
 * The body renders the whole thing; a row can carry the first sentence and
 * nothing else, and taking it verbatim is what stops this from becoming a
 * mapping table that has to be updated every time a new failure can be
 * recorded. An error with no sentence boundary at all (hand-written, or stored
 * by an older build) is shown whole if it is short enough to be a sentence and
 * dropped if it is not — half a git error is noise on a list row, and the plan
 * is one click away from the whole of it. */
export const HEADLINE_MAX = 120;

export function errorHeadline(plan: TestPlan): string {
  const raw = String(plan?.error || "").trim();
  if (!raw) return "";
  // Any terminator, not just ". " — a message that says "e.g." mid-sentence, or
  // ends its first clause with a question mark, was being cut at the wrong
  // place or not found at all.
  const stop = raw.search(/[.!?](\s|$)/);
  let first = stop >= 0 ? raw.slice(0, stop + 1) : raw;
  // CLAMPED, NEVER DROPPED. Returning "" for a long sentence meant the row said
  // nothing at all — and the two sentences the release path writes ("The verify
  // run was given up on…", "The verify session's agent window is gone…") are
  // exactly the ones that overflowed, so the commonest failures were the silent
  // ones. The stopline wraps, so a full sentence fits; past that, cut at a word
  // boundary and say so.
  if (first.length > HEADLINE_MAX) {
    const cut = first.slice(0, HEADLINE_MAX);
    const space = cut.lastIndexOf(" ");
    first = (space > 40 ? cut.slice(0, space) : cut).replace(/[,;:]$/, "") + "…";
  }
  // Sentence-cased and terminated, because half the writers are not sentences:
  // every `TestPlanError` that reaches `_fail` is a lowercase clause with no
  // full stop ("every step the model wrote was a placeholder"), and it is
  // appended to a sentence that has just ended.
  const cased = first.charAt(0).toUpperCase() + first.slice(1);
  return /[.!?…]$/.test(cased) ? cased : cased + ".";
}

export function planStatus(plan: TestPlan, liveBranch = ""): PlanStatus {
  const status = planStatusOf(plan, liveBranch);
  // A RUN THAT DIED IS PART OF WHERE THIS PLAN HAS GOT TO, and until now the
  // row was the one place that never said so: `fail_run` puts the plan back to
  // `due`, so the sentence reverted to "Shipped — nobody has checked it yet"
  // and the only trace of the failure was inside the expanded body. Press Run,
  // come back to the list later, and it reads exactly like a plan you never
  // pressed Run on.
  //
  // Appended, not substituted: whose turn it is stays the first thing the row
  // says. Skipped where the sentence is ALREADY about the failure (the two
  // `failed` branches, an empty checklist) and while a rewrite is in flight,
  // where the stored error belongs to the attempt this one is replacing.
  const headline = errorHeadline(plan);
  if (
    !headline ||
    plan.state === "failed" ||
    plan.state === "generating" ||
    !(plan.steps || []).length
  )
    return status;
  return { ...status, line: status.line + " " + headline };
}

function planStatusOf(plan: TestPlan, liveBranch = ""): PlanStatus {
  const group = planGroup(plan);
  const steps = plan.steps || [];
  const run = latestRun(plan);
  // Whether an agent could settle anything here at all. Asked once, because
  // EVERY branch that offers a run has to respect it: the run route refuses a
  // checklist with no agent steps (it would provision a worktree and minutes of
  // a billed session for an agent that is forbidden to answer a single step),
  // so a button offering one is a button that errors.
  const agentCan = steps.filter((s) => s.actor !== "human").length;
  /** "Run (again)", or the way back into the list when there is nothing to run.
   *
   * `ran` distinguishes "an agent has had a go at this" from "the only run on
   * record is you answering steps by hand" — offering to run it *again* when no
   * agent ever ran it is a small lie about what has already happened here. */
  const again = (ran = true): [PlanAction, string] =>
    agentCan
      ? ["rerun", ran ? "Run again with an agent" : "Run with an agent"]
      : // No agent step to re-run, so there is nothing to run and the button is
        // just the way back into the list. It used to read "Check again", which
        // is the same phrase the can't-check branch below uses for an actual
        // re-check — one label, two meanings, six lines apart.
        ["answer", "Open the steps"];
  const at = (tone: PlanTone, line: string, action: PlanAction, actionLabel = "") =>
    ({ group, tone, line, action, actionLabel }) as PlanStatus;

  // Still being written. Nothing to say about the code yet, and nothing to
  // press — the row changes on its own when the model answers.
  if (plan.state === "generating") {
    // …unless it has been "about to answer" for longer than the answer can
    // possibly take, which means the process writing it is gone (the app was
    // closed mid-write). Say so and give the button back.
    // States the OBSERVATION, not a cause. It used to assert "the app closed
    // while the model was answering", which is one of several things that
    // produce this row — a restart, a crash, a laptop that slept, or a plan
    // written before the server stamped `gen_started` at all (which this
    // function's own docstring concedes reads as stalled). Naming the wrong
    // cause sends somebody to check the wrong thing.
    if (isGenerationStalled(plan))
      return at(
        "broken",
        "Writing this checklist stopped part-way — nothing has been written for five minutes.",
        "rewrite",
        "Rewrite the checklist",
      );
    return at("wait", "Writing the checklist from the diff — up to three minutes.", "none");
  }

  // The generator failed AND there is nothing to fall back on. A defect in the
  // plumbing, not an answer about the code, so the only useful button is "ask
  // again".
  //
  // The `!steps.length` guard is the whole subtlety. This test used to come
  // first and unconditionally, so a rewrite that timed out on a perfectly good
  // eight-step checklist printed "the model couldn't write a checklist for this"
  // directly above the eight steps and your recorded answers — while the heading
  // above it still filed the plan under due. The server no longer parks a plan
  // with steps in `failed` at all (see `test_plans._fail`), but a plan stored by
  // an older build can still be in exactly that state, and the row has to read
  // truthfully about it rather than depend on the fix upstream.
  if (plan.state === "failed" && !steps.length)
    return at(
      "broken",
      "The model couldn't write a checklist for this.",
      "rewrite",
      "Rewrite the checklist",
    );
  if (plan.state === "failed")
    return at(
      "warn",
      "Rewriting this failed — these are the steps from before, and they still " +
        "work.",
      "rewrite",
      "Try rewriting again",
    );

  // A plan that came back empty looks identical to one still being written, so
  // it has to say which it is.
  if (!steps.length)
    return at(
      "broken",
      "No steps — the checklist came back empty.",
      "rewrite",
      "Rewrite the checklist",
    );

  // An agent is on it right now. The one thing worth doing is watching — but
  // the sentence carries YOUR count too, and recomputes as you answer.
  //
  // The sentence, not the button. It is tempting to promote "Answer N" here,
  // since your steps are already answerable in the expanded body — but
  // `latestRun` returns the IN-FLIGHT run (a run with a session is never a
  // phantom), so every human step is "open" from the instant the run starts.
  // Promoting it would mean the busy state never renders at all: the row would
  // skip from Run straight to "3 steps are yours" and the only feedback that an
  // agent is working — and the only way to reach its session — would be gone.
  if (plan.state === "running") {
    const mine = openHumanSteps(plan).length;
    // ...unless there is no session to watch. `running` with no `run_session` is
    // a run whose session went away — `prune` clears the title when the session
    // dies, and the state can lag a tick behind it. Offering Watch there was the
    // one genuinely dead control on the surface: `watch()` opens nothing when
    // the title is blank, and a disabled primary button is painted exactly like
    // a live one, so the press produced no pane, no toast and no change. If
    // nothing is running, say so and offer the thing that would start it.
    if (!plan.run_session)
      return at(
        "warn",
        "The agent that was checking this is gone — nothing is running now.",
        ...again(false),
      );
    // HOW LONG IT HAS BEEN GOING. One sentence stood for everything from "you
    // pressed this three seconds ago" to "this has been wedged for an hour and
    // fifty minutes and is about to be given up on", which is the difference
    // between waiting and going to look. Coarse on purpose (`agoText`'s
    // rounding, shared with the row's own ages) and omitted for the first
    // minute, where "just now" says nothing the press did not.
    const been = runElapsed(run);
    return at(
      "busy",
      mine
        ? "An agent is checking the rest" +
            been +
            " — " +
            mine +
            (mine === 1 ? " step needs" : " steps need") +
            " your eyes."
        : "An agent is checking the steps it can" + been + ".",
      "watch",
      "Watch",
    );
  }

  if (!run) {
    // Shipped, and nobody has looked. The reason the surface exists.
    if (plan.live_at || plan.state === "due") {
      // ...and an agent cannot help with a checklist that is entirely yours, so
      // this is the one place Run is replaced rather than relabelled. Not rare,
      // either — `parse_plan` defaults an unrecognised actor to "human", so a
      // model that omits the key produces exactly this checklist.
      if (!agentCan)
        return at(
          "you",
          steps.length === 1
            ? "Shipped — its one step is yours to check."
            : "Shipped — all " + steps.length + " steps are yours to check.",
          "answer",
          "Answer " + steps.length + (steps.length === 1 ? " step" : " steps"),
        );
      // The shape of the job, in the sentence rather than in a tooltip. "Run"
      // reads as "run the tests" — cheap, local, over in seconds — and it is
      // minutes of a real agent in a real worktree. Saying what it will and
      // will not cover turns the press from a guess into a decision.
      return at(
        "you",
        "Shipped — nobody has checked it yet. " +
          // No "the rest are yours" when there is no rest: a checklist an agent
          // can work end to end is the good case, and inventing a share for the
          // reader would send them looking for steps that do not exist.
          (agentCan === steps.length
            ? "An agent can check all " +
              steps.length +
              (steps.length === 1 ? " step." : " steps.")
            : "An agent can check " +
              agentCan +
              " of " +
              steps.length +
              "; the rest are yours."),
        "run",
        "Run with an agent",
      );
    }
    // MERGED, AND WAITING FOR THE DEPLOY. Its own sentence because it is its
    // own state and the reader's question is different: "has it merged yet?"
    // is answered, and what is left is a pipeline. Without this the row is
    // indistinguishable from one whose branch nobody has merged, so the minutes
    // between landing a PR and being able to check it read as the feature
    // having lost the work.
    if (plan.merged_at)
      return at(
        "wait",
        "Merged " + agoText(plan.merged_at) + " — waiting for it to deploy.",
        "none",
      );

    // ...unless the wait CANNOT END. A plan watching a branch origin does not
    // have will never come due, and the sentence below — "it turns up here to
    // check when it ships" — is then a promise the surface cannot keep. This is
    // the one waiting state that is an ask, because it is the user's to fix.
    if (plan.live_problem)
      return at("warn", plan.live_problem, "none");

    // Written, but not out there yet. Explicitly NOT an ask: it turns up on its
    // own when the commit lands, and a button here would invite the one-way
    // door (see the ⋯ menu's "Check it early").
    //
    // It says what ENDS the wait rather than "nothing to do", which read as a
    // dead end to the one person guaranteed to see this row: whoever just
    // pressed "Write a checklist" and is looking at the result.
    return at(
      "wait",
      "Waiting for " +
        (plan.branch || "this branch") +
        " to reach " +
        (plan.live_branch || liveBranch || "the live branch") +
        " — it turns up here to check when it ships.",
      "none",
    );
  }

  // The agent ran and handed back the steps only a person can settle. This is
  // the surface's actual call to action, so it outranks every answer below.
  // "Needs your eyes", not "your call". "Your call" is a phrase people use to
  // mean "up to you whether you bother", which is the one reading this line
  // cannot afford — it is the single call to action the whole feature exists
  // for. The button is a verb plus a counted noun ("Answer 2 steps") because a
  // bare numeral after a verb parses as an ordinal: "Answer 1" reads as
  // "answer number one".
  const open = openHumanSteps(plan);
  if (open.length)
    return at(
      "you",
      open.length === 1
        ? "1 step needs your eyes — an agent can't judge it."
        : open.length + " steps need your eyes — an agent can't judge them.",
      "answer",
      "Answer " + open.length + (open.length === 1 ? " step" : " steps"),
    );

  // The most valuable output of the whole feature: it shipped and it is broken.
  // The button opens the evidence rather than spending minutes of an agent to
  // reprint an answer already on disk — and, less obviously, "Run again" here
  // was destructive: the new run is `latestRun` the moment it starts, so one
  // press replaced the failure you were reading with an empty in-flight run.
  // Re-running after a fix is still one item away, in the ⋯.
  const failed = failCount(plan);
  if (failed)
    return at(
      "bad",
      failed === 1
        ? "1 step didn't do what was expected."
        : failed + " steps didn't do what was expected.",
      "answer",
      "See what failed",
    );

  // Everything is answered, and some of those answers were "I couldn't get to
  // it". Its own branch because the two sentences below are both wrong about
  // it: nothing "never got an answer" (you gave one) and nothing "works"
  // (nobody observed it). Tone is `warn` and never `ok` — a can't-check is a
  // known unknown, and the tone is the loudest claim on the row.
  const missing = unansweredCount(plan);
  const cant = cantCheckCount(plan);
  if (missing && missing === cant) {
    const seen = (plan.steps || []).length - cant;
    return at(
      "warn",
      seen
        ? seen + (seen === 1 ? " step passed · " : " steps passed · ") +
          cant + " you couldn't check."
        : "Nothing could be checked — " + cant +
          (cant === 1 ? " step you couldn't get to." : " steps you couldn't get to."),
      "answer",
      "Check again",
    );
  }

  // Whether an AGENT has actually had a go at this, as opposed to the only run
  // on record being you answering steps by hand — which is a real state
  // (`record_result` opens a session-less run for the first answer) and used to
  // be described as "the run stopped early", about a run that never started.
  const ranByAgent = !!(run.session || run.by === "agent");

  if (missing) {
    // The two kinds of gap, counted apart and said apart — the same split the
    // "Checked" heading makes. Summing them let the row tell somebody who had
    // answered every step of theirs that those steps "never got an answer".
    const blank = missing - cant;
    const gaps = [
      blank ? blank + (blank === 1 ? " step" : " steps") + " with no answer" : "",
      cant ? cant + " you couldn't check" : "",
    ]
      .filter(Boolean)
      .join(" · ");
    return at(
      "warn",
      (plan.state === "done"
        ? "Checked — "
        : ranByAgent
          ? "The run stopped early — "
          : "Part-answered — ") +
        gaps +
        ".",
      ...again(ranByAgent),
    );
  }

  // FINISHED. The primary button used to be "Run again with an agent" — the row
  // saying the job is done while its one button invited minutes of a billed
  // session redoing it, which is the most expensive control on the surface
  // offered at the moment it is least useful. Running it again is still one
  // item away in the ⋯, where re-doing settled work belongs.
  return at("ok", "Every step has an answer — it works.", "answer", "See the answers");
}

/** A GitHub-checks-style tally of one plan's steps.
 *
 * WHY A TALLY AND NOT JUST THE STATUS SENTENCE. `planStatus` is deliberately ONE
 * ordered decision producing ONE sentence, so the row can never contradict
 * itself — but that means it can only ever say the loudest true thing. Opening a
 * twelve-step checklist, the reader's next question is not "what is the loudest
 * thing" but "how much of this is settled, and how" — the question a checks
 * panel answers at a glance and a paragraph of prose does not. So the sentence
 * stays exactly as it is and the tally sits above the steps, computed from the
 * same run `planStatus` read, so the two are two renderings of one state.
 */
export interface CheckTally {
  passed: number;
  failed: number;
  /** Steps waiting on a person — a human step nobody has settled, or one the
   * agent explicitly handed back. */
  yours: number;
  /** You answered "can't check". A known unknown, never folded into passed. */
  cant: number;
  /** Nobody has said anything about these at all. */
  pending: number;
  total: number;
}

export function checkTally(plan: TestPlan): CheckTally {
  const steps = plan?.steps || [];
  const t: CheckTally = {
    passed: 0,
    failed: 0,
    yours: 0,
    cant: 0,
    pending: 0,
    total: steps.length,
  };
  // COUNTED THROUGH `stepCheck`, not beside it. Written the other way — its own
  // pass over the results — the two drifted immediately: the tally reached for
  // `openHumanSteps`, which requires a run to exist, while the row's own rule
  // deliberately does not (see `wantsYou` in the dialog), so a shipped checklist
  // nobody had run yet showed accent-marked rows waiting on a person above a
  // summary that said nothing was. One function decides, the other adds up.
  const key: Record<CheckState, keyof CheckTally> = {
    pass: "passed",
    fail: "failed",
    yours: "yours",
    cant: "cant",
    pending: "pending",
  };
  for (const step of steps) t[key[stepCheck(plan, step.id)]]++;
  return t;
}

/** One tally bit: a state, how many steps are in it, and what to call it. */
export interface TallyBit {
  state: CheckState;
  count: number;
  /** Already agreed with `count` — see `tallyBits`. */
  label: string;
}

/** The tally as a list to render — the non-zero states, in a fixed order.
 *
 * WHY THIS EXISTS RATHER THAN TWO MAPS OVER `checkTally`. The roll-up above the
 * steps and the one on the COLLAPSED row are the same sentence said twice, and
 * the moment they are written twice they drift: a state added here would appear
 * in one place and not the other, and — worse — the two would word the same
 * bucket differently, so a row saying "2 open" would sit above a head saying "2
 * need you" and the reader would reasonably conclude they counted different
 * things. The order is fixed and is the order of the legend: what went well,
 * what did not, what is being asked of you, what you looked at and could not
 * settle, what nobody has touched.
 *
 * Zeroes are dropped: "0 failed" is noise on a row that has room for about five
 * words, and the absence of the red bit is the same information.
 */
export function tallyBits(plan: TestPlan): TallyBit[] {
  const t = checkTally(plan);
  const bits: TallyBit[] = [
    { state: "pass", count: t.passed, label: "passed" },
    { state: "fail", count: t.failed, label: "failed" },
    // The one label with a verb in it, so the one that has to agree with its
    // number: this string is read out as the tally's whole aria-label, and "1
    // need you" is where a screen reader gives the surface away.
    { state: "yours", count: t.yours, label: t.yours === 1 ? "needs you" : "need you" },
    { state: "cant", count: t.cant, label: "you couldn't check" },
    { state: "pending", count: t.pending, label: "not checked yet" },
  ];
  return bits.filter((b) => b.count > 0);
}

/** The tally as one plain sentence, for a `title` and for a screen reader.
 *
 * The row renders glyphs and numbers, which are quick to scan and impossible to
 * read aloud: `✓ 5` announces as "5" if the glyph is hidden and as "check mark
 * 5" if it is not, and neither says "passed". So the visual tally is marked
 * `aria-hidden` and this is what is announced instead — the same numbers, in
 * words, from the same source. */
export function tallySentence(plan: TestPlan): string {
  const t = checkTally(plan);
  if (!t.total) return "";
  const bits = tallyBits(plan);
  const head = t.total + (t.total === 1 ? " check" : " checks");
  if (!bits.length) return head;
  return head + ": " + bits.map((b) => b.count + " " + b.label).join(", ");
}

/** The check state of one step — what its glyph and colour say.
 *
 * `blocked` splits in two here for the same reason `receiptLabel` splits it in
 * the dialog: an agent's blocked is "not mine to answer" (so the step is still
 * open, and it is open FOR YOU), while yours is "I couldn't get to it", which is
 * a recorded answer and a known unknown. One word, two meanings, and the glyph
 * is the one place a reader will not tolerate the ambiguity. */
export type CheckState = "pass" | "fail" | "cant" | "yours" | "pending";

/** Has this work SHIPPED — the question every "is this asking me something?"
 * rule is really asking.
 *
 * ``live_at`` is the fact and the state is the usual shorthand for it, and the
 * shorthand alone is wrong in exactly one place: a REWRITE puts a plan that has
 * shipped into ``generating``. `isWaitingOnYou` knows that and deliberately
 * keeps such a plan in the badge and under "Not checked yet"; the two rules
 * written as bare state checks did not, so pressing Rewrite on a live checklist
 * flipped every "need you" mark to "not checked yet" and dropped the accent
 * from the rows — the tally contradicting the heading directly above it, over a
 * press that changed nothing about who owes what. One function, three callers.
 */
export function planShipped(plan: TestPlan): boolean {
  return (
    !!plan?.live_at ||
    plan?.state === "due" ||
    plan?.state === "running" ||
    plan?.state === "done"
  );
}

/** Whether this plan is at the point of ASKING a person for its human steps.
 *
 * Shipping is the usual reason, and a RUN is the other one: `openHumanSteps`
 * — which `needsConfirmation`, `isWaitingOnYou`, the badge and the grouping all
 * read — asks only whether a run exists, so a checklist that has not shipped
 * but has been run (or hand-answered, which opens a run too) is filed under
 * "Not checked yet" and says "2 steps need your eyes". The mark and the tally
 * asked the narrower question and answered "not checked yet" about the very
 * steps the sentence above them was asking for. One condition now, so the
 * heading, the sentence, the tally and the row cannot disagree. */
export function asksHumanSteps(plan: TestPlan): boolean {
  return planShipped(plan) || !!latestRun(plan);
}

export function stepCheck(plan: TestPlan, stepId: string): CheckState {
  const entry = stepResult(plan, stepId);
  if (entry?.result === "pass") return "pass";
  if (entry?.result === "fail") return "fail";
  if (isYourAnswer(entry?.result || "", entry?.by || "")) return "cant";
  const step = (plan.steps || []).find((s) => s.id === stepId);
  if (step && stepIsYours(plan, step) && asksHumanSteps(plan)) return "yours";
  return "pending";
}

/** The one glyph per state, shared by the rows and the tally so a legend can
 * never describe a mark the list does not use. */
export const CHECK_MARK: Record<CheckState, string> = {
  pass: "\u2713",
  fail: "\u2717",
  cant: "\u2013",
  yours: "\u25cf",
  pending: "\u25cb",
};

/** A keypress on a step row, mapped to the answer it records.
 *
 * WHY A PURE FUNCTION AND NOT A SWITCH IN THE HANDLER. `frontend/vitest.config`
 * is node-only by design — there is no jsdom here and adding one to test a
 * keyboard shortcut would be the tail wagging the dog — so every decision the
 * dialog makes that is worth pinning lives out here, where a test can call it.
 * This one is worth pinning: answering is the single most repeated act on the
 * surface, and before these keys existed it cost a Tab past roughly seven
 * focusable controls per step.
 *
 * Two spellings each, digits and initials, because both are things people
 * reach for and neither is more obvious than the other. `""` means "not a key
 * this row handles" and the event is left alone — which is what stops these
 * from eating a keystroke meant for the note box beside them. */
export function stepKeyAction(key: string): TestStepResult | "note" | "" {
  switch (key) {
    case "1":
    case "p":
    case "P":
      return "pass";
    case "2":
    case "f":
    case "F":
      return "fail";
    case "3":
    case "b":
    case "B":
      return "blocked";
    case "u":
    case "U":
      return "";  // handled as "undo" by the caller — see stepKeyIsUndo
    case "n":
    case "N":
      return "note";
    default:
      return "";
  }
}

/** `u` — take back the answer on this step. Split from the map above because
 * "no answer" and "not a key I handle" are the same value there, and the caller
 * has to be able to tell them apart. */
/** What the note composer should hold after an answer is recorded.
 *
 * THE BUG THIS REPLACES WAS A LIE ABOUT WHAT YOU TYPED. The composer wrote
 * itself into a draft map on every keystroke (so the row survives being
 * re-parented under a new heading mid-answer), and the "clear it" path cleared
 * the field and then re-saved the OLD text from a stale closure — so a sentence
 * you deliberately discarded came back on the next paint and was posted, by
 * blur, as the reason for the NEXT answer. A note on this surface is evidence;
 * attaching one to the wrong answer is the worst thing it can do quietly.
 *
 * Out here as a pure rule because the composer's behaviour is worth pinning and
 * `frontend/vitest.config` is node-only by design — the same reason
 * :func:`stepKeyAction` lives here.
 */
export function noteDraftAfter(
  result: TestStepResult,
  noteOpen: boolean,
  note: string,
): { open: boolean; text: string } {
  // Un-answered: whatever was typed described an answer that no longer exists.
  if (result === "") return { open: false, text: "" };
  // A fail or a blocked with no sentence is an answer nobody can act on, so the
  // box opens itself rather than waiting to be found — EMPTY, because anything
  // still in it belonged to the answer before this one.
  if ((result === "fail" || result === "blocked") && !noteOpen)
    return { open: true, text: "" };
  // It rode along with the answer just given and now renders above as the
  // step's note; leaving it in the composer would show the same sentence twice
  // and invite a second, identical POST.
  if (noteOpen && note.trim()) return { open: false, text: "" };
  return { open: noteOpen, text: note };
}

/** Whether a keypress on a step row may record an answer right now.
 *
 * The three answer BUTTONS are `disabled` while a POST is in flight; the keys
 * were not, and did not look at `repeat` either — so holding "1" fired one POST
 * per key-repeat (roughly fifteen a second), and "2" then "1" raced two writes
 * whose last-arriving response need not be the last one the server processed.
 */
export function stepKeyAllowed(busy: boolean, repeat: boolean): boolean {
  return !busy && !repeat;
}

export function stepKeyIsUndo(key: string): boolean {
  return key === "u" || key === "U";
}

/** What a run is evidence ABOUT, in one line — or "" when there is nothing
 * worth saying.
 *
 * This is the only answer this surface can give to "did that actually validate
 * production", and it is worth a line of its own precisely because the honest
 * answer varies: a repo with a deployment configured had its steps worked
 * against that deployment; one without had them worked against a checkout of the
 * live branch on this machine. Saying which is the difference between evidence
 * and a claim. */
export function runEvidence(run: TestRun | null, liveBranch = ""): string {
  if (!run) return "";
  if (run.target) return "Checked against " + run.target;
  const sha = (run.tested_sha || "").slice(0, 7);
  if (!sha) return "";
  return "Checked on " + (liveBranch || "the live branch") + " @ " + sha;
}

/** Whether this run demonstrably worked a tree other than the one asked for.
 *
 * Three-valued in effect: either sha unknown answers false. Unknown is not
 * mismatched — a run recorded before the server asked, or one whose worktree was
 * reclaimed first, must not have its answers thrown away over a question nobody
 * was able to put. Mirrors `test_plans.run_tree_mismatch`, which is the half
 * that actually downgrades the passes. */
export function runTreeMismatch(run: TestRun | null): boolean {
  const tested = (run?.tested_sha || "").trim();
  const expected = (run?.expected_sha || "").trim();
  return !!(tested && expected && tested !== expected);
}

/** The warning a rewrite has to carry, or "" when it costs nothing.
 *
 * A pure function rather than a line of JSX, so the rule "only warn when there
 * is something to lose" is pinned by a test — the dialog's `vitest` setup is
 * node-only by design, and this seam is what keeps it that way. */
export function rewriteWarning(plan: TestPlan): string {
  if (!latestRun(plan)) return "";
  return (
    "Answers on steps that change are lost. Steps you added or edited yourself " +
    "are kept."
  );
}

/** Whether Rewrite may be pressed at all right now.
 *
 * A run in flight is the one hard no: `generate` sets `generating`
 * unconditionally while the poller only ever looks at plans in `running`, so
 * rewriting mid-run orphans a real, billed session — its result file never read,
 * its give-up clock never started, and Cancel gone from the row along with the
 * state that offered it. The server 409s it; the button should not offer it. */
export function rewriteBlockedReason(plan: TestPlan): string {
  if (plan.state === "running")
    return "An agent is checking this right now — cancel the run first.";
  if (plan.state === "generating") return "It is already being written.";
  return "";
}

/** The prefix `POST /api/test-plans/{id}/run` builds every verify session's
 * title from (`server.run_test_plan`: `title = "verify-%s" % plan_id`).
 *
 * Shared here rather than spelled inline because two very different places need
 * to agree on it: the Verify dialog, which offers to open or end the session,
 * and the sidebar, which must NOT list it. */
export const VERIFY_SESSION_PREFIX = "verify-";

/** Whether a session is a verify run rather than a piece of the user's work.
 *
 * WHY THE SIDEBAR CARES. A verify run is a real session — it needs a worktree,
 * a checked-out tree and an agent that can actually run commands, and none of
 * that can be faked in a chat pane. But it is not *work*: it is a window you
 * open to watch something for two minutes and then close, like the assistant.
 * Listing it in the left rail put it alongside the branches a person is
 * actually building, where it accumulates, has to be manually deleted, and
 * makes a flock of four sessions look like a flock of eight.
 *
 * So it lives in the grid and not in the rail: `computeVisible` still sees it
 * (that is what gives it a pane), and only the sidebar filters it out. The
 * Verify dialog is where it is reachable — every plan whose session exists
 * offers to open or end it, so nothing is ever stranded by being unlisted. */
export function isVerifySession(title: string): boolean {
  return (title || "").startsWith(VERIFY_SESSION_PREFIX);
}
