/** The Verify dialog — what shipped, and does it work?
 *
 * This is deliberately NOT a fifth Intake tab, even though it borrows Intake's
 * whole visual vocabulary. Intake is the FRONT of the pipeline: every tab there
 * answers "what work is out there, and which of it may become a session?", and
 * every row's action is *start something*. Verify is the BACK of it. Its rows
 * are not candidates, they are things that already happened — a branch that has
 * reached the live branch — and its action is *check something*. Putting them
 * on one strip would have meant a tab whose count means "waiting to begin" next
 * to a tab whose count means "waiting to be believed", and a user reading the
 * two as one queue would be reading it wrong.
 *
 * There is a second, blunter reason: Intake is where you go when you want more
 * work, and this is where you go when you are done with a piece of it. Nobody
 * opens "the intake screen" to close the loop on last Tuesday's merge. It gets
 * its own top-bar entry, its own badge and its own key (Alt+V) because the
 * moment it exists to serve — a plan going due while you were somewhere else —
 * is exactly the moment nobody remembers to go looking.
 *
 * THE SHAPE, and why it is this one. The surface is one grouped queue, and the
 * first group IS the badge: `verify.ts` exports one predicate, `dueCount`
 * filters by it, and the "Waiting on you" heading renders exactly the plans it
 * counted. There is no tab strip, because the tab strip was a filter that
 * rendered the top of the list under the same headings — one number printed in
 * four places, and no two of them ever equal.
 *
 * Every row carries exactly ONE status: a plain-English sentence saying whose
 * turn it is, and one button that does what the sentence says. It replaced four
 * chips that routinely disagreed (a plan could read "checked" next to "partial"
 * under a heading that said you had to confirm it) and four paragraphs of body
 * prose that nobody expanded a row to read. `planStatus` decides the sentence,
 * the tone and the button together, from the same `planGroup` the heading came
 * from, so the three cannot contradict each other.
 *
 * Everything rare — rewriting, deleting, running something that has not shipped
 * — lives in a ⋯ menu.
 *
 * THE ANATOMY IS INTAKE'S, in Intake's order: an intro sentence, the master
 * switch, the work the sources produced, the SOURCES it watches (these two
 * swap while nothing is configured yet, so + Add leads), and a
 * `<details>` at the bottom for what you touch once. PullRequestsTab and
 * IssuesTab are laid out exactly this way, down to `.set-block-hint` and
 * `.pr-advanced`, and Verify is the fourth surface of the same kind — it should
 * not have to be learned separately. The switch is `repository.verify_enabled`;
 * it pauses the automatic half only (no checklist written on a push, no liveness
 * pass moving work into the list) and deliberately leaves writing one by hand,
 * running and answering a step working, exactly as a forced PR review still runs
 * with automated review switched off.
 *
 * ONE NOUN AND ONE VERB, throughout. The feature is **Verify**, the artifact is
 * a **checklist**, and the act is to **check**. "Test plan" survives in the API
 * paths, the store, the settings keys and these docstrings, where no user reads
 * it — on screen it was a fifth name for a thing that already had four, and to
 * a developer it reads as pytest.
 *
 * The honest words are visible, not hovered. "Blocked is never a pass", who
 * recorded an answer and when, how to take one back, and the two one-way doors
 * (running a plan before it ships, or hand-answering a pre-live one, both close
 * it permanently) were all in `title` attributes — on the one surface whose
 * entire claim is that its labels are true, and where keyboard and touch users
 * never saw them.
 *
 * The furniture (`ws-head`, `pr-open-chip`, `repo-empty`, `btn-primary`,
 * WorkListPanel / WorkGroup) is Intake's, reused rather than re-cut: the two
 * surfaces are read minutes apart by the same person, and a second dialect of
 * "a list of work with counts on it" would be a tell that one of them is bolted
 * on. What is genuinely new here — the plan row, the step row and the overflow
 * menu — lives in VerifyDialog.css under `vf-`.
 */

import { useEffect, useMemo, useState } from "react";
import type { TestPlan } from "../../api/types";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import {
  refreshInstances,
  refreshTestPlans,
  useInstances,
  useTestPlans,
} from "../../state/queries";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
// Failures go to the bottom-right cards, never a toast — see lib/verifyActions.ts.
import { errorPop } from "../../lib/errorPop";
import { errMsg } from "../../lib/format";
import { requestPlanDelete, requestPlanRun } from "../../lib/verifyActions";
// THE LIST DIALOGS' OWN KIT, not a second one. Recently-closed and Workspaces
// already have a Ctrl+F filter and checkbox multi-select; a checklist list that
// grows without bound needs both for the same reason, and inventing a Verify
// dialect of either would make the third list the one people have to learn.
import { searchTokens } from "../../lib/rowSearch";
import { previewList } from "../../lib/rowSelection";
import { DialogFilter } from "./DialogFilter";
import { BulkRowBar, SelectAllCheck, useRowSelection } from "./rowSelect";
import { WorkListPanel, panelNote } from "../intake/kit";
import { SettingsCtx, useSettingsModel } from "../settings/useSettings";
import { InlineConfirm } from "../InlineConfirm";
import { Modal } from "../Modal";
import {
  type ClosedTarget,
  canRunNow,
  closedTargets,
  liveBranchOverridden,
  noTargetsReason,
  planMatches,
  planName,
  planTargets,
} from "./verify";
import { forgetNoteDrafts } from "./VerifyStep";
import { PlanList, VerifyByHand, VerifyEmpty, VerifySources, VerifySwitch } from "./VerifyChrome";

export function VerifyDialog() {
  const open = useUi((s) => s.openDialog === "verify");
  const closeDialog = useUi((s) => s.closeDialog);
  // Polls only while the dialog is up; the top bar's badge keeps its own copy
  // of the same query alive for the count.
  const plansQ = useTestPlans(open);
  // Read here rather than inside the bar: the same list decides what the picker
  // may offer. The query is the app-wide one the sidebar and grid already keep
  // warm, so this costs no extra polling.
  const instancesQ = useInstances();
  // CLOSED SESSIONS COUNT TOO: asking for a checklist is something people do
  // once the work is finished and the window has been put away, which is
  // precisely when the session is no longer in `instancesQ`.
  //
  // DECLARED UP HERE WITH THE OTHER QUERIES, and that is not tidiness. This
  // component returns `null` when the dialog is shut (below), so a hook added
  // after that early return only exists on the renders where it is open — the
  // hook count changes the instant you press Verify, React throws "rendered more
  // hooks than during the previous render", and the entire app unmounts to a
  // white screen. Every hook in this component has to be above that line.
  //
  // `enabled: open` is what keeps it honest instead: the hook always runs, the
  // request only happens while somebody is looking.
  const closedQ = useQuery({
    queryKey: ["recently-closed"],
    queryFn: () => api<ClosedTarget[]>("/api/recently-closed"),
    enabled: open,
    staleTime: 30_000,
    retry: false,
  });
  // The tracked-repo list is ordinary settings now, so this dialog needs the
  // same settings model the Intake tabs get from IntakeDialog — loaded on open,
  // provided below, and read by `VerifySwitch` / `VerifySources` through
  // `useSettings()`. Nothing Verify-specific: `repository.verify_repos` saves
  // through the one POST /api/settings every other card list already uses.
  const model = useSettingsModel(open);

  /** The Ctrl+F filter, and the checkbox selection it drives.
   *
   * ABOVE THE `if (!open)` RETURN, like every other hook here — see the note on
   * `closedQ` for what adding one below it costs. */
  const [query, setQuery] = useState("");
  const allPlans = plansQ.data?.plans || [];
  const shown = useMemo(() => {
    const tokens = searchTokens(query);
    return allPlans.filter((p) => planMatches(p, tokens));
  }, [allPlans, query]);
  // Both key lists follow the DISPLAY order — shift-ranges have to match what
  // the eye sees, and a bulk confirmation reads like the list it came from.
  // Grouping reorders within a group but never across the list's own order, so
  // the flat order is the one to select in.
  const sel = useRowSelection(
    useMemo(() => allPlans.map((p) => p.id), [allPlans]),
    useMemo(() => shown.map((p) => p.id), [shown]),
  );
  const [bulk, setBulk] = useState("");
  const [confirmBulk, setConfirmBulk] = useState(false);

  // Half-typed step notes belong to the visit, not to the app: drop them when
  // the dialog shuts so nothing reappears in a row a week later. The filter and
  // the selection go with them, for a sharper reason: a checkbox still ticked
  // from last time is how somebody deletes a checklist they never looked at.
  useEffect(() => {
    if (open) return;
    forgetNoteDrafts();
    setQuery("");
    setConfirmBulk(false);
    sel.clear();
  }, [open, sel]);

  // Put the caret somewhere sensible on open. Without this, a keyboard user
  // arriving via Alt+V has focus still on whatever was behind the modal, so the
  // first Tab walks the page UNDER the dialog — and on a surface whose whole job
  // is pressing one of three small buttons per step, that is the difference
  // between usable and not.
  useEffect(() => {
    if (!open) return;
    const id = window.setTimeout(() => {
      const panel = document.getElementById("verify-panel");
      // One selector at a time, in PRIORITY order. A single comma-separated
      // selector would not do: querySelector returns the first match in DOCUMENT
      // order, and #verify-close lives up in the header — so every visit would
      // have landed on Close, which is the one control nobody opened this dialog
      // to press.
      for (const sel of [".vf-primary", "#vf-repo-add-btn", "#verify-close"]) {
        const el = panel?.querySelector<HTMLElement>(sel);
        if (el) {
          el.focus();
          break;
        }
      }
    }, 0);
    return () => window.clearTimeout(id);
  }, [open]);

  if (!open) return null;

  const plans = allPlans;
  const byId = new Map(plans.map((p) => [p.id, p]));
  const picked = sel.keys.map((id) => byId.get(id)).filter(Boolean) as TestPlan[];
  const liveBranch = plansQ.data?.live_branch || "";
  // Does anything on this surface actually wait on a different branch? "What
  // counts as shipped" is per repo, so the one branch in the header is a
  // DEFAULT, not a fact about every plan — and a header that states it as a
  // fact contradicts the repo card in Sources, and the drift line on the rows.
  // Asked of the PLANS and of the CONFIGURED REPOS both: a plan outlives its
  // repo's place on the list, and a repo configured a minute ago has no plans
  // at all (they cost a model call and are written on push), so either source
  // alone leaves a case where the header lies. `liveBranchOverridden` owns the
  // rule and says why.
  const repoCfg = (model.settings.repository || {}) as {
    verify_repos?: string[];
    verify_repo_settings?: Record<string, { live_branch?: string }>;
    deploy_delay_minutes?: number;
  };
  // The flock-wide wait, shown as each card's placeholder so an empty field
  // reads as "inherits 5" rather than as "nothing set". Absent => the backend
  // default, which is 5.
  const deployDelay = String(
    repoCfg.deploy_delay_minutes == null ? 5 : repoCfg.deploy_delay_minutes
  );
  const overridden = liveBranchOverridden(
    liveBranch,
    plans,
    repoCfg.verify_repos,
    repoCfg.verify_repo_settings
  );
  const closed = closedQ.data || [];
  const targets = planTargets(instancesQ.data || [], plans, closed);
  const closedNames = closedTargets(instancesQ.data || [], closed);

  /** Fan a per-plan endpoint out over the selection, then reload once.
   *
   * ONE REQUEST PER PLAN, deliberately: each route carries its own refusals
   * (a run needs an agent step and a repo that still exists; a delete closes
   * that plan's session), and a bulk endpoint would have to re-implement every
   * one of them. What the bar owes the user instead is an honest tally — how
   * many went, and what the rest said. */
  const fanOut = async (
    targets: TestPlan[],
    verb: string,
    each: (plan: TestPlan) => Promise<unknown>,
  ) => {
    if (!targets.length) return;
    setBulk(verb);
    const failures: string[] = [];
    for (const plan of targets) {
      try {
        await each(plan);
      } catch (err) {
        failures.push(planName(plan) + " — " + errMsg(err));
      }
    }
    setBulk("");
    setConfirmBulk(false);
    sel.clear();
    await refreshInstances();
    await refreshTestPlans();
    const done = targets.length - failures.length;
    if (done) toast(verb + " " + done + " of " + targets.length);
    if (failures.length)
      errorPop(
        failures.length + (failures.length === 1 ? " checklist" : " checklists") +
          " couldn't be " + verb.toLowerCase(),
        failures.join("\n"),
      );
  };

  // Only the ones a run would actually take: the route refuses a checklist that
  // is generating, one an agent is already working, and one with no agent step
  // in it — so firing at all of them would answer a wall of 409s and start
  // nothing. Said in the button's own label rather than discovered afterwards.
  const runnable = picked.filter(canRunNow);

  const error = plansQ.error
    ? "Could not load the checklists: " + (plansQ.error.message || "error")
    : "";
  const note = panelNote({
    error,
    fetching: plansQ.isFetching,
    loaded: !!plansQ.data,
  });

  // Work first once there is any: a tracked repo or a checklist. Before that,
  // Sources (and its + Add) is the only thing there is to do.
  const workFirst = (repoCfg.verify_repos || []).length > 0 || plans.length > 0;

  const sources = (
    <VerifySources key="sources" liveBranch={liveBranch} deployDelay={deployDelay} />
  );
  const checklists = (
    <WorkListPanel
      key="work"
      label="Checklists"
      onRefresh={() => void plansQ.refetch()}
      note={note}
      rowId="vf-plans-row"
      refreshId="vf-plans-refresh"
      noteId="vf-plans-note"
      listId="vf-plans-list"
      // FIND ONE, OR PICK SEVERAL. This list grows without bound — one
      // checklist per session branch per repo — and until now the only
      // way to a particular one was scrolling. The filter is Recently
      // closed's, down to Ctrl+F and the token rule; the select-all box
      // beside it applies to what the filter is SHOWING, which is what
      // makes "find the sitecheck ones, run them all" a two-gesture job.
      toolbarExtra={
        plans.length ? (
          <>
            <SelectAllCheck
              state={sel.allState}
              onChange={sel.setAllVisible}
              label="Select every checklist shown"
            />
            <DialogFilter
              id="vf-plans-filter"
              value={query}
              onChange={setQuery}
              placeholder="Filter by ticket, branch, repo, or what a step says…  ( Ctrl+F )"
              onEscape={closeDialog}
            />
          </>
        ) : undefined
      }
      // Dropped entirely when there is nothing to read it against: a
      // line teaching what a step's lane means, printed over a list with
      // no steps in it, is vocabulary a first-time reader has nowhere to
      // spend.
      hint={
        plans.length ? (
          <>
            Steps marked <strong>you</strong> are the job — an agent settles the rest.
          </>
        ) : undefined
      }
    >
      {error ? (
        <div className="repo-empty">{error}</div>
      ) : !plansQ.data ? null : !plans.length ? (
        <VerifyEmpty liveBranch={liveBranch} />
      ) : (
        <>
          {picked.length ? (
            <BulkRowBar
              count={picked.length}
              hiddenCount={sel.hiddenCount}
              noun="checklist"
              onClear={sel.clear}
            >
              <button
                type="button"
                disabled={!runnable.length || bulk !== ""}
                title={
                  runnable.length
                    ? "Start a verify session for each — minutes of a real agent apiece"
                    : "None of the selected checklists is one an agent can run"
                }
                onClick={() =>
                  void fanOut(runnable, "Started", (plan) => requestPlanRun(plan.id))
                }
              >
                {/* The NUMBER, always: this is the one control here
                    that spends a workspace and minutes of a billed
                    agent per row, so "Run selected" over eight ticked
                    boxes is not an informed press. Delete asks for
                    confirmation instead, because it cannot be undone;
                    a run can be cancelled from the row it starts. */}
                {bulk === "Started"
                  ? "Starting…"
                  : "Run " +
                    runnable.length +
                    (runnable.length === picked.length
                      ? ""
                      : " of " + picked.length)}
              </button>
              <button
                type="button"
                className="danger"
                disabled={bulk !== ""}
                title="Delete the selected checklists and every answer recorded against them"
                onClick={() => setConfirmBulk(true)}
              >
                {bulk === "Deleted" ? "Deleting…" : "Delete selected"}
              </button>
            </BulkRowBar>
          ) : null}
          {confirmBulk ? (
            // Inline, not a native confirm(): this dialog is
            // `aria-modal`, and a second window the app did not draw
            // cannot say what is about to be destroyed. `previewList` is
            // the same summary Recently closed shows before a wipe.
            <InlineConfirm
              title={
                "Delete " +
                picked.length +
                (picked.length === 1 ? " checklist?" : " checklists?")
              }
              body={
                "Their steps and every answer recorded against them go too. " +
                "This cannot be undone.\n" +
                previewList(picked.map(planName))
              }
              confirmLabel={"Delete " + picked.length}
              cancelLabel="Keep it"
              busy={bulk !== ""}
              onCancel={() => setConfirmBulk(false)}
              onConfirm={() => void fanOut(picked, "Deleted", (plan) => requestPlanDelete(plan.id))}
            />
          ) : null}
          {shown.length ? (
            <PlanList plans={shown} liveBranch={liveBranch} selection={sel} />
          ) : (
            <p className="muted vf-nomatch">
              No checklist matches “{query}”.
            </p>
          )}
        </>
      )}
    </WorkListPanel>
  );

  return (
    <SettingsCtx.Provider value={model}>
      {/* The shared shell: backdrop PRESS and Escape dismiss, Escape only for
          the topmost modal (so a ⋯ menu or a note box spends it first). */}
      <Modal
        id="verify-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="verify-title"
        onClose={closeDialog}
      >
        <div id="verify-panel">
          <div className="ws-head">
            <h2 id="verify-title">Verify</h2>
            <span className="ik-subtitle">What shipped, and does it work?</span>
            {/* The live branch, named out loud. "Live" is the entire premise of
                this surface — a plan is due because its commit reached THIS
                branch — and leaving the reader to guess which one it resolved to
                would make every row an assertion they cannot check. Shown only
                once the server has answered: guessing "main" here would be the
                one lie the surface cannot afford — and so is stating one branch
                for the whole flock when a repo in Sources has been given its own. */}
            {liveBranch ? (
              <span
                className="vf-live"
                title={
                  "A checklist sits quiet until its commit reaches this branch, then turns " +
                  "up here to be checked." +
                  (overridden
                    ? " Some repos ship from another branch — each checklist's own is on its row."
                    : "")
                }
              >
                checked once work reaches <code>{liveBranch}</code>
                {overridden ? <span className="vf-live-more"> · some repos differ</span> : null}
              </span>
            ) : null}
            <button type="button" id="verify-close" onClick={closeDialog}>
              Close
            </button>
          </div>

          <div id="verify-body">
            {/* INTAKE'S ANATOMY, in Intake's order: a sentence saying what the
                automation does, the master switch, the work those sources
                produced, the SOURCES it watches, and a fold at the bottom for
                what you touch once. Work leads once there is any to show (a
                tracked repo or a checklist); before that the empty Sources
                list and its + Add lead, exactly as on the Tickets, Pull
                requests and Issues tabs — this is the fourth surface of the
                same kind and it should not have to be learned separately. */}
            <p className="set-hint set-block-hint">
              MindFlock writes a checklist for every session branch pushed in the
              repositories below, and brings it here to be checked once that work
              reaches the branch you ship from. An agent settles the steps a shell can
              settle; the rest are yours.
            </p>
            <VerifySwitch />
            {/* Keyed, so the swap MOVES the two instead of remounting them
                (adding the first repo flips it mid-edit; see PullRequestsTab). */}
            {workFirst ? [checklists, sources] : [sources, checklists]}
            <VerifyByHand
              candidates={targets}
              closed={closedNames}
              reason={noTargetsReason(instancesQ.data || [], plans, closed)}
            />
          </div>
        </div>
      </Modal>
    </SettingsCtx.Provider>
  );
}
