/** One Verify checklist row: the three-line header (what it is, whose turn,
 * what to press), its ⋯ menu, and the expanded body — the run watched inline,
 * the roll-up, and the steps (VerifyStep.tsx).
 *
 * Split out of VerifyDialog.tsx. Every request a row sends lives in
 * lib/verifyActions.ts; this file owns which button is busy and which box is
 * open. */

import { useEffect, useRef, useState } from "react";
import type { TestPlan, TestStep, TestStepResult } from "../../api/types";
import { refreshTestPlans } from "../../state/queries";
import { useUi } from "../../state/store";
import { errorPop } from "../../lib/errorPop";
import { relTime } from "../../lib/format";
import * as actions from "../../lib/verifyActions";
import { patchPlan } from "../../lib/verifyActions";
import { useWsTerm } from "../../lib/wsTerm";
import { InlineConfirm } from "../InlineConfirm";
import { OverflowMenu, type MenuItem } from "../OverflowMenu";
import { RowCheck, type RowSelection } from "./rowSelect";
import {
  CHECK_MARK,
  checkTally,
  failCount,
  latestRun,
  openHumanSteps,
  planName,
  planStatus,
  runEvidence,
  runTreeMismatch,
  stepIsYours,
  tallyBits,
  tallySentence,
} from "./verify";
import {
  AddStep,
  RewriteBox,
  StepRow,
  byLabel,
  forgetRowUi,
  rowUi,
  setRowUi,
} from "./VerifyStep";

/** The same age phrased as a MOMENT rather than as a duration: "3h ago".
 *
 * "live, 3h old" is how you describe a thing's age; "shipped 3h ago" is how you
 * describe when something happened, and every timestamp on a plan row is the
 * latter. The one formatter (`relTime`) with its default suffix, so the step
 * receipts' "3h old" and this can never round differently. "" for no stamp. */
function agoOf(epochSeconds: number): string {
  return epochSeconds ? relTime(epochSeconds) : "";
}

/** One plan: a three-line header that says what it is, whose turn it is and what
 * to press, and an expanded body carrying the steps.
 *
 * The header is three lines and one button on purpose. It used to be a title,
 * four chips and three equally-weighted buttons, where the rarest and only
 * irreversible action (Delete) had the same visual weight as the one thing you
 * opened the dialog to press. */
export function PlanRow({
  plan,
  liveBranch,
  selection,
  expanded,
  onToggle,
  onExpand,
}: {
  plan: TestPlan;
  /** The flock-wide default, used ONLY as a stand-in when this plan's own repo
   * has not been resolved (an older payload). What the row compares against is
   * `plan.effective_live_branch` — see `nowLive` below. */
  liveBranch: string;
  /** The dialog's checkbox selection, when the list is selectable. */
  selection?: RowSelection;
  expanded: boolean;
  onToggle(): void;
  /** Open the body if it is closed — what "Answer 2" needs, which is not a
   * toggle: pressing it on an already-open plan must not shut it. */
  onExpand(): void;
}) {
  const closeDialog = useUi((s) => s.closeDialog);
  const openVerifyPane = useUi((s) => s.openVerifyPane);
  const [busy, setBusy] = useState("");
  const [stepBusy, setStepBusy] = useState("");
  const [focusStep, setFocusStep] = useState("");
  // All three are seeded from ROW_UI and written back through it, so a row that
  // changes group mid-gesture — which is what a run finishing does — comes back
  // exactly as it was. See ROW_UI.
  const savedUi = rowUi(plan.id);
  /** The inline rewrite composer, open or not. Replaces a native `confirm()`. */
  const [rewriting, setRewritingRaw] = useState(!!savedUi.rewriting);
  const setRewriting = (next: boolean) => {
    setRewritingRaw(next);
    setRowUi(plan.id, { rewriting: next });
  };
  /** The one destructive action left, asked inline for the same reason: a
   * browser `confirm()` inside an `aria-modal` dialog is a second window the app
   * did not draw and cannot phrase properly. "" = nothing pending. */
  const [confirming, setConfirmingRaw] = useState<"" | "delete" | "early">(
    savedUi.confirming || "",
  );
  const setConfirming = (next: "" | "delete" | "early") => {
    setConfirmingRaw(next);
    setRowUi(plan.id, { confirming: next });
  };
  /** Watch the run HERE rather than in a grid pane behind the modal.
   *
   * THREE STATES, not two, and that is what makes Hide work. Left alone the
   * watcher follows the run — expand a plan an agent is working and its output
   * is there, which is the behaviour this was built for. Pressing Hide or
   * "Show the run here" records a preference that outranks it, and pressing Run
   * latches it ON, so the terminal survives the moment the run ENDS: that is
   * when the failure it was streaming actually arrives, and it used to vanish
   * exactly then (the plan leaves `running`, and the row is re-parented into
   * another group, which remounts it — see ROW_UI). */
  const [watchPref, setWatchPrefRaw] = useState<boolean | null>(
    savedUi.watching === undefined ? null : savedUi.watching,
  );
  const setWatchingHere = (next: boolean) => {
    setWatchPrefRaw(next);
    setRowUi(plan.id, { watching: next });
  };
  const bodyRef = useRef<HTMLDivElement>(null);

  const run = latestRun(plan);
  const running = plan.state === "running";
  // THE SESSION THE LOG LIVES IN, which outlives `plan.run_session`: every
  // terminal transition blanks that field (`finish_run`, `cancel_run`,
  // `fail_run`, `mark_due`), so keying the watcher on it alone meant the
  // terminal — and the ⋯ item offering to bring it back — disappeared at the
  // instant the run ended. The run RECORD keeps the title, and the sweep
  // deliberately keeps that session open for exactly this reason: so the person
  // can read what it did.
  const watchSession = plan.run_session || run?.session || "";
  const watchingHere = watchPref === null ? running : watchPref;
  const evidence = runEvidence(run, plan.live_branch || liveBranch);
  const failedCount = failCount(plan);
  const steps = plan.steps || [];
  const status = planStatus(plan, liveBranch);
  const open = openHumanSteps(plan);
  // The split, counted once. Said in the legend so "which of these are mine?"
  // is answered before you start reading rows, and so a plan that is entirely
  // one lane says so instead of leaving you to check every row for a label.
  // Counted through `stepIsYours`, like every other count on this card: a run
  // that handed six steps back moved them into your lane, and a legend still
  // reading "6 for the agent" would be describing the checklist as written
  // rather than the one on screen — directly contradicting the eight ● marks
  // underneath it.
  const mineCount = steps.filter((s) => stepIsYours(plan, s)).length;
  const agentCount = steps.length - mineCount;
  // What THIS PLAN'S REPO calls live today, which is the only branch the plan's
  // own stamp can honestly be compared with. "What counts as shipped" is a
  // per-repo fact and a plan records the per-repo answer, so measuring it
  // against the flock-wide default would flag every plan in a repo that set its
  // own live branch as written against a branch that has moved — while the
  // branch it names is precisely the one the repo is configured for. The
  // flock-wide value survives only as the fallback for a payload old enough not
  // to carry the per-repo answer.
  const nowLive = plan.effective_live_branch || liveBranch;

  // WHERE THE WORK IS RIGHT NOW, in one word, and it is the only word on this
  // line that is not the branch. `plan.branch` is where it was pushed and
  // `live_branch` is what this checklist is waiting for; between the two, a
  // change in a repo that ships through staging spends most of its life merged
  // somewhere neither field names — so the row names the rung it is actually
  // standing on and lets the reader watch it climb: local → staging → main.
  //
  // It REPLACED "shipped 4d ago". "Shipped" was a verb this surface had to
  // teach (shipped where? this repo ships from `release`), it was the same word
  // for every plan past the first rung, and it said nothing a reader triaging a
  // list of eight actually wants — which of these is still only on my laptop,
  // which got as far as staging, which is out. The moment stays; only the noun
  // in front of it changes, so the line still dates itself.
  //
  // `local` is not a branch name and is deliberately not styled like one: it is
  // the absence of a landing, which is exactly the state a checklist spends its
  // first hours in. `live_at` without a `merged_into` is an older payload — the
  // rung that answers "merged into what?" is newer than the one that stamps
  // "this is live" — so it falls back to the branch the plan is measured
  // against rather than reporting a shipped plan as local.
  const where = plan.merged_into || (plan.live_at ? nowLive || "shipped" : "local");
  // "" for a landing the rung that answered could not date — a squash merge is
  // known by its PR, and a PR does not carry the moment it merged.
  const whereAgo = agoOf(plan.merged_into_at || plan.live_at || plan.generated_at);
  const whereTitle = (
    plan.merged_into
      ? [
          "Most recently merged into " +
            plan.merged_into +
            " on origin" +
            (whereAgo ? " " + whereAgo : "") +
            ".",
          // The trail, when there is one: staging on the day it merged, main on
          // the day somebody promoted it. One name per landing, so this is short.
          (plan.merged_into_all || []).length > 1
            ? "Also in " + (plan.merged_into_all || []).slice(1).join(", ") + "."
            : "",
          plan.merged_into === nowLive
            ? ""
            : "This repo ships from " + (nowLive || "?") + ".",
        ]
      : [
          plan.live_at
            ? "Live on " + (nowLive || "the live branch") + "."
            : "Still only on " + (plan.branch || "its own branch") + " — origin has it nowhere else yet.",
          plan.generated_at && !plan.live_at
            ? "Checklist written " + agoOf(plan.generated_at) + "."
            : "",
          plan.live_at ? "" : "This repo ships from " + (nowLive || "?") + ".",
        ]
  )
    .filter(Boolean)
    .join("\n");

  // "Answer N" opens the plan and puts the first thing it is asking about in
  // the middle of the screen. Expanding a twelve-step plan and leaving the
  // reader at step 1 to hunt for the two that are theirs is most of the work
  // the button was supposed to save.
  useEffect(() => {
    if (!focusStep || !expanded) return;
    const root = bodyRef.current;
    if (root) {
      const esc = typeof CSS !== "undefined" && CSS.escape ? CSS.escape(focusStep) : focusStep;
      const el = root.querySelector('[data-step-id="' + esc + '"]');
      if (el) {
        el.scrollIntoView({ block: "center", behavior: "smooth" });
        // ...and FOCUS it. Scrolling a row into view and leaving focus on the
        // button you pressed is most of the work the button was supposed to
        // save: the row is where the keys work, and where a screen reader has
        // to be for any of this to have happened at all.
        (el as HTMLElement).focus?.({ preventScroll: true });
      }
    }
    setFocusStep("");
  }, [focusStep, expanded]);

  /** Start a run, optionally scoped to one step (the per-step Re-check) —
   * `actions.startChecklistRun`, with this row's busy flags around it. The inline
   * terminal latches on as soon as the server has taken it. */
  const startRun = async (only?: string[]) => {
    if (only) setStepBusy(only[0]);
    else setBusy("run");
    try {
      await actions.startChecklistRun(plan, { only, onStarted: () => setWatchingHere(true) });
    } finally {
      setBusy("");
      setStepBusy("");
    }
  };

  const watch = () => {
    // Never a silent no-op. The session can go away between the row rendering
    // and the press, and returning quietly made the button look broken — no
    // pane, no message, nothing. Say it, and pull the plan so the row stops
    // offering a window onto something that is not there.
    if (!watchSession) {
      errorPop(
        "Nothing to watch",
        "That run's session is gone. The row will catch up in a moment — start it again from there.",
      );
      void refreshTestPlans();
      return;
    }
    openVerifyPane(watchSession);
    // The dialog HAS to close: the verify pane is a grid window and this is a
    // full-screen `.modal` over it, so a pane opened behind the dialog is a
    // press with no visible effect. The pane's own head carries the way back
    // (SpecialPane's "Verify" button), which is what makes the round trip
    // symmetrical rather than one-way.
    closeDialog();
  };

  /** The first step matching a result the latest run recorded.
   *
   * What the expanding buttons scroll to. Read off the same run `planStatus`
   * counted, so a button cannot land on a step that is no longer the one its
   * sentence is about. `by` narrows it to who recorded the answer, which is the
   * only way to tell your "Can't check" from the agent's "blocked". */
  const firstStepWith = (result: TestStepResult, by = ""): string => {
    const results = run?.results || {};
    return (
      steps.find((s) => {
        const entry = results[s.id];
        return entry?.result === result && (!by || entry.by === by);
      })?.id || ""
    );
  };

  /** Ask for a new checklist — see `actions.regeneratePlan`. The composer closes
   * once the server has taken it. */
  const regenerate = async (focus = "") => {
    setBusy("regen");
    try {
      if (await actions.regeneratePlan(plan, focus)) setRewriting(false);
    } finally {
      setBusy("");
    }
  };

  /** Run a plan whose commit has not shipped yet.
   *
   * A ONE-WAY DOOR, and the confirm is the only thing that says so. The run
   * checks out the plan's own commit rather than the live branch, which is
   * reasonable — but `finish_run` writes `state = "done"` whatever happened, and
   * the liveness loop only ever re-asks plans still in `generated`. So a plan
   * run early never comes back as due when the branch actually ships, which is
   * the opposite of what somebody pressing "run it now" is trying to achieve. */
  const runEarly = () => setConfirming("early");

  /** One busy flag around one action: the press that started it reads "…" and
   * every other button on the row holds still until it settles. */
  const withBusy = async (key: string, fn: () => Promise<void>) => {
    setBusy(key);
    try {
      await fn();
    } finally {
      setBusy("");
    }
  };
  const cancelRun = () => withBusy("cancel", () => actions.cancelChecklistRun(plan));
  const fixFailures = (only?: string[]) =>
    withBusy("fix", () => actions.fixPlanFailures(plan, only));
  const markDeployed = () => withBusy("deployed", () => actions.markPlanDeployed(plan));
  // Titles are reused (a new session with the same name produces the same plan
  // id), so the row's scratch UI goes with it — a "watching" flag left behind
  // would open a terminal on the NEXT checklist of that name for a session that
  // no longer exists.
  const remove = () =>
    withBusy("delete", () =>
      actions.removePlan(plan, { watchSession, onGone: () => forgetRowUi(plan.id) }),
    );
  const recordStep = (step: TestStep, result: TestStepResult, note: string) =>
    actions.recordStepAnswer(plan, step, result, note);

  const doPrimary = () => {
    if (status.action === "answer") {
      onExpand();
      // Three buttons share this action and they are asking three different
      // questions, so they land in three different places: "Answer N steps" on
      // the first step that is still yours, "See what failed" on the first
      // failure, "Check again" on the first thing you couldn't get to. Ordered
      // rather than switched, and every arm falls through to the next, so the
      // button always lands somewhere useful even if a result changed under it.
      const target =
        (status.tone === "bad" ? firstStepWith("fail") : "") ||
        (open.length ? open[0].id : "") ||
        firstStepWith("blocked", "human");
      if (target) setFocusStep(target);
      return;
    }
    if (status.action === "watch") {
      // The run is watched INSIDE the checklist now. The grid pane is still
      // there for anyone who wants the agent on screen while they work
      // elsewhere, but it is no longer the price of pressing Watch.
      onExpand();
      setWatchingHere(true);
      return;
    }
    if (status.action === "rewrite") {
      onExpand();
      setRewriting(true);
      return;
    }
    void startRun();
  };

  // Every applicable action EXCEPT the one already on the primary button.
  const menu: MenuItem[] = [];
  // BRING THE LOG BACK. Opening it inline is otherwise reachable only while the
  // plan is `running` (the primary button's "watch" arm), so the terminal that
  // vanished when the run finished — the moment its own failure arrived — could
  // not be reopened at all, and the only other route to it, below, closes the
  // dialog to show a grid pane. Offered whenever there is a session to show.
  if (watchSession && !watchingHere)
    menu.push({
      key: "watch-here",
      label: running ? "Show the run here" : "Show the last run here",
      title:
        watchSession +
        " — its output, inline under this checklist, without closing the dialog",
      onSelect: () => setWatchingHere(true),
    });
  if (watchSession)
    menu.push({
      key: "watch",
      label: "Open the session in its own window",
      title:
        watchSession +
        " — the same stream this checklist shows inline, in a grid pane you can " +
        "keep open while you work somewhere else. Closing it does not stop the run.",
      onSelect: watch,
    });
  if (running)
    menu.push({
      key: "cancel",
      label: "Cancel run",
      title:
        "Stops the session and puts this plan back. Nothing it found is lost — " +
        "the session is closed, not deleted, and Recently closed (under the session list) reopens it.",
      className: "test-btn vf-cancel",
      onSelect: () => void cancelRun(),
    });
  // Only while it is actually waiting on a pipeline — see the route, which
  // refuses anything else rather than silently reopening a finished checklist.
  if (plan.state === "generated" && plan.merged_at)
    menu.push({
      key: "deployed",
      label: "It's deployed — check it now",
      title: "Skip the rest of this repo's deploy window and make it due now",
      onSelect: () => void markDeployed(),
    });
  if (!running && steps.length && plan.state !== "generating") {
    // Whether an agent could do anything here at all. A checklist whose every
    // step is a person's own is not runnable — the run prompt forbids the agent
    // from settling one — so offering it would spend a worktree and minutes of
    // a billed session to hand everything straight back.
    const agentCan = steps.some((s) => s.actor !== "human");
    if (status.action === "none" && agentCan)
      // The pre-live plan. Its only run is the early one, and it is named for
      // what it costs rather than dressed as an ordinary Run. Gated on
      // `agentCan` like every other run offer: without that, a checklist whose
      // every step is a person's own showed this as its ONLY action, and the
      // run route refuses it — a row whose single button always errors.
      menu.push({
        key: "early",
        label: "Check it early…",
        title:
          "Runs an agent against this checklist's own commit rather than the live " +
          "branch — and closes it, so it will not come back when the branch ships",
        onSelect: runEarly,
      });
    else if (agentCan && status.action !== "run" && status.action !== "rerun")
      // Every state whose primary button is something else — notably a recorded
      // failure, whose button now opens the evidence. Re-running after a fix is
      // the right thing to do second, not first.
      menu.push({
        key: "run",
        label: run ? "Run again with an agent" : "Run with an agent",
        title: "Starts a session that works every step it can from a shell",
        onSelect: () => void startRun(),
      });
  }
  // Only when there is something to fix — a button that opens an empty session
  // is worse than no button, and the route 409s it anyway.
  if (!running && failedCount > 0)
    menu.push({
      key: "fix",
      label:
        failedCount === 1
          ? "Fix what failed…"
          : "Fix the " + failedCount + " that failed…",
      title:
        "Opens an ordinary session in this repo with the step, what was expected " +
        "and what happened instead. It is told to reproduce it first — a step can " +
        "be wrong, and changing shipped code to satisfy a wrong step is worse.",
      onSelect: () => void fixFailures(),
    });
  if (plan.state !== "generating" && status.action !== "rewrite")
    menu.push({
      key: "regen",
      label: "Rewrite the checklist",
      title:
        "Ask the model again — and tell it what this draft got wrong, which is " +
        "the part that makes the second one better than the first",
      onSelect: () => {
        onExpand();
        setRewriting(true);
      },
    });
  menu.push({
    key: "delete",
    label: "Delete this checklist",
    danger: true,
    title: "Forget this checklist and everything recorded against it",
    onSelect: () => {
      onExpand();
      setConfirming("delete");
    },
  });

  return (
    <div
      className={"vf-plan" + (selection?.has(plan.id) ? " picked" : "")}
      data-tone={status.tone}
      data-plan-id={plan.id}
    >
      <div className="vf-plan-head">
        {selection ? (
          // BEFORE the expander, so the column of boxes is the first thing the
          // eye can run down — the same place Recently closed puts it. The
          // checkbox stops its own click, so ticking a row never expands it.
          <span className="vf-plan-check">
            <RowCheck
              checked={selection.has(plan.id)}
              title={"Select " + planName(plan) + " (Shift-click to extend the range)"}
              onToggle={(shift) => selection.toggle(plan.id, shift)}
            />
          </span>
        ) : null}
        {/* The whole title block is the expander. `.tk-caret` inside an
            aria-expanded button is Intake's affordance and the rotation rule in
            IntakeDialog.css reaches it here for free, because these rows render
            inside a WorkListPanel's `.ik-groups`. */}
        <button
          type="button"
          className="vf-plan-toggle"
          aria-expanded={expanded}
          onClick={onToggle}
          title={
            [
              planName(plan),
              "branch " + (plan.branch || "?"),
              "commit " + (plan.sha ? plan.sha.slice(0, 12) : "?"),
              "live branch " + (plan.live_branch || "?"),
              plan.merged_into ? "merged into " + plan.merged_into : "",
              plan.repo_root,
            ]
              .filter(Boolean)
              .join("\n")
          }
        >
          <span className="tk-caret">▸</span>
          <span className="vf-plan-title">{planName(plan)}</span>
          {/* WHAT SHIPPED, in a sentence. `planName` is the session's title —
              the key everything addresses this plan by — so a checklist coming
              due three weeks later was headed "sc-1234-fix-filters" over a list
              of imperatives, and the reader had to reconstruct what the change
              was from the steps themselves. The model writes this alongside the
              steps; a plan generated before it did simply has no second line. */}
          {plan.summary ? (
            <span className="vf-plan-summary">{plan.summary}</span>
          ) : null}
        </button>
        {/* THE status. One sentence, in the second person where it matters,
            replacing four chips that could disagree with each other and with
            the heading above them. Plain text, so a screen reader and a
            pointer-less user get exactly what the eye gets. */}
        {/* THE SENTENCE AND THE TALLY, on one line. The tally used to sit on
            the meta line below, where it was the third thing on a row of
            identifiers and read as more metadata; beside the sentence it reads
            as what it is — the same statement, counted. It also puts the two
            things a triaging eye runs down, "whose turn is it" and "is anything
            red", on the same baseline, so a list of eight is one pass. */}
        <div className="vf-plan-line">
          <div className="vf-stopline" data-tone={status.tone}>
            {/* THE WHEEL. `generating` is the one state with nothing to press
                and nothing on screen changing for up to three minutes; without
                motion, "working" and "wedged" are the same quiet row. The
                stalled branch flips the tone to "broken" and takes the wheel
                with it — a spinner beside "stopped part-way" would be the row
                calling itself a liar. */}
            {plan.state === "generating" && status.tone === "wait" ? (
              <span className="vf-spin" aria-hidden="true" />
            ) : null}
            {status.line}
          </div>
          <PlanTally plan={plan} />
        </div>
        {/* The branch, and the rung it is standing on. No short sha: it was the
            middle of three identifiers on a line only wide enough for one and a
            half, it is not what anyone addresses this plan by, and the branch
            name it was stealing width from is. The full twelve are still in the
            row's own tooltip, where a sha is actually of use. */}
        <div className="vf-plan-meta">
          <span className="vf-plan-meta-text">{plan.branch}</span>
          {/* A chip rather than more meta text, because this is the one fact on
              the row somebody SCANS a list for — "which of these are in main
              already?" — and running text at 11px is not scannable. Tinted when
              the branch it names is the one this repo ships from, so "it's out"
              and "it's got as far as staging" are told apart without reading. */}
          <span
            className="vf-plan-landed"
            data-live={where === nowLive && !!nowLive ? "yes" : "no"}
            title={whereTitle}
          >
            {where}
            {whereAgo ? " · " + whereAgo : ""}
          </span>
        </div>
        {status.action !== "none" ? (
          <button
            type="button"
            className="btn-primary vf-primary"
            data-action={status.action}
            // `stepBusy` too: a per-step Re-check and this button start the SAME
            // request, and two of them in flight for one plan is how a title
            // gets claimed twice — one create wins, the loser's failure lands on
            // the winner's plan, and the winner's tmux session is left with
            // nothing owning it. The server refuses the second now; the button
            // should not offer it.
            disabled={
              busy !== "" ||
              stepBusy !== "" ||
              (status.action === "watch" && !plan.run_session)
            }
            title={
              status.action === "answer"
                ? status.tone === "bad"
                  ? "Open the steps that didn't do what was expected"
                  : status.tone === "warn"
                    ? "Open the steps you couldn't get to"
                    : "Open the steps that need your eyes"
                : status.action === "watch"
                  ? "Open " + (plan.run_session || "the verify session")
                  : status.action === "rewrite"
                    ? "Ask the model for a new set of steps from the same diff"
                    : // The cost, said plainly. This is the one control on the
                      // surface that spends a workspace and minutes of a billed
                      // agent, and it used to be indistinguishable from "run the
                      // tests".
                      "Starts a real session in its own workspace and works every " +
                      "step it can from a shell — minutes, not seconds. Steps only " +
                      "you can judge come back for you."
            }
            onClick={doPrimary}
          >
            {busy === "run" ? "Starting…" : busy === "regen" ? "…" : status.actionLabel}
          </button>
        ) : null}
        <OverflowMenu
          label={"More actions for " + planName(plan)}
          items={menu}
          boundaryId="verify-body"
        />
      </div>

      {expanded && (
        <div className="vf-plan-body" ref={bodyRef}>
          {!!plan.error && (
            // The generator failing is a defect in the plumbing, not an answer
            // about the code, so it gets the error verbatim rather than a
            // friendly paraphrase — the raw line is what makes it fixable.
            //
            // Keyed on the ERROR and not on `state === "failed"`, because a
            // rewrite that fails no longer costs the plan its rung: a checklist
            // with steps keeps its place in the queue and records the reason
            // here (see `test_plans._fail`). Rendering this only for `failed`
            // meant the commonest failure — a rewrite of a good checklist timing
            // out — reported itself nowhere at all.
            <div className="vf-error">
              {/* Neutral, because more than one thing writes here now: a
                  generation that failed, a rewrite that failed, and a verify
                  session that could not start. The stored sentence says which —
                  each writer stores a self-describing one — so a heading that
                  guessed would be wrong a third of the time. */}
              <strong>{steps.length ? "That didn't work." : "Couldn't write a plan."}</strong>{" "}
              {plan.error || "no reason recorded"}
              <div className="vf-note">
                {steps.length
                  ? "The steps below are untouched, and still answerable."
                  : "Rewriting asks the model again. If it keeps failing, the coding CLI " +
                    "it used probably has no headless mode configured, or the worktree it " +
                    "was written from is gone — you can also write the steps yourself below."}
              </div>
            </div>
          )}
          {/* The second entrance to the one-way door, and the quieter one:
              `record_result` closes ANY plan to "done" the moment nothing is
              unanswered, liveness be damned. So a helpful person working through
              a pre-live checklist by hand silently removes it from the due
              pipeline — with nothing anywhere on the screen having said so. */}
          {plan.state === "generated" && steps.length > 0 && (
            <div className="vf-warn">
              Answering these now closes the checklist — it will not come back to
              <strong> Not checked yet</strong> when the branch ships.
            </div>
          )}
          {/* The plan was measured against whatever ITS REPO counted as live
              when its answers were recorded. A checklist NOBODY has answered
              follows the repo's current setting on its own — even back out of
              `due` (`retarget_live_branch`) — so by the time this can persist
              the difference is a fact about answers already given rather than
              something still to fix. (An unanswered plan can show it for up to
              a minute, until the due loop's next pass re-aims it — which is
              what the last clause promises.) The comparison is against the
              repo's own branch and not the flock-wide default, which would
              fire on every plan in a correctly-configured repo and teach the
              reader to ignore it. */}
          {plan.live_branch && nowLive && plan.live_branch !== nowLive && (
            <div className="vf-drift">
              Measured against <code>{plan.live_branch}</code>; this repo now ships{" "}
              <code>{nowLive}</code>. Answers here were recorded against that branch, so it
              keeps it — a checklist with nothing answered re-aims itself at{" "}
              <code>{nowLive}</code> on its own, within a minute.
            </div>
          )}
          {run && (
            <div className="vf-note">
              Last checked by {byLabel(run.by)}
              {run.session ? " (" + run.session + ")" : ""}, {agoOf(run.at) || "just now"}.
              {/* WHAT THAT RUN IS EVIDENCE ABOUT. The only place this surface
                  can honestly answer "did it validate production", and the
                  answer genuinely differs per repo — a deployment, or a checkout
                  of the live branch on this machine. */}
              {evidence ? " " + evidence + "." : ""}
            </div>
          )}
          {runTreeMismatch(run) && (
            // The failure `build_run_prompt` calls the one that must not be able
            // to happen, made visible when it does. The server has already
            // downgraded that run's passes; this says why, so the row is not a
            // mystery full of blocked steps.
            <div className="vf-warn">
              That run wasn't on {nowLive || "the live branch"} — it worked{" "}
              <code>{(run?.tested_sha || "").slice(0, 7)}</code>, so its passes have been
              set aside. Run it again.
            </div>
          )}
          {steps.length > 0 && (
            // The one sentence a new user genuinely must read, said once per
            // plan where it is unmissable. It used to be three `title`
            // attributes, which is a strange place to keep the promise the
            // whole surface rests on.
            <div className="vf-legend">
              {/* Two sentences, in the order a reader needs them: first whose
                  step is whose, then what the three answers mean. The lane
                  labels are quoted in their own colours so the line teaches the
                  column beside it rather than describing it. */}
              <div>
                <span className="vf-step-who vf-who-you">you</span> = only a person can
                check it ·{" "}
                <span className="vf-step-who">agent</span> = a verify run settles it from
                a shell
                {mineCount > 0 && agentCount > 0
                  ? " — " + agentCount + " for the agent, " + mineCount + " for you"
                  : ""}
                .
              </div>
              {/* The one sentence a new user must read, and the promise the
                  whole surface rests on: all three are answers, and only one of
                  them says it works. Spelling that out is what makes "Can't
                  check" safe to press — before it, the honest answer was the one
                  that left the row nagging, so people pressed Pass instead. */}
              <div>
                <strong>Pass</strong> = it did what's expected · <strong>Fail</strong> = it
                didn't · <strong>Can't check</strong> = you couldn't get to it. All three
                count as your answer; only Pass says it works.
              </div>
              {/* Not optional furniture: none of this is discoverable, and the
                  legend is the one place per plan where this surface's
                  vocabulary is taught rather than assumed. */}
              <div className="vf-keys">
                On a step: <kbd>1</kbd> pass · <kbd>2</kbd> fail · <kbd>3</kbd> can't
                check · <kbd>u</kbd> undo · <kbd>n</kbd> note.
              </div>
            </div>
          )}
          {rewriting && (
            <RewriteBox
              plan={plan}
              busy={busy === "regen"}
              onCancel={() => setRewriting(false)}
              onRewrite={(focus) => void regenerate(focus)}
            />
          )}
          {confirming === "delete" && (
            <InlineConfirm
              title={"Delete the checklist for " + planName(plan) + "?"}
              body={
                "Every answer recorded against it goes too. " +
                (running
                  ? "This also stops " + (plan.run_session || "the verify session") + "."
                  : "A later push of the same branch writes a new one.")
              }
              confirmLabel={busy === "delete" ? "Deleting…" : "Delete it"}
              cancelLabel="Keep it"
              busy={busy !== ""}
              onCancel={() => setConfirming("")}
              onConfirm={() => {
                setConfirming("");
                void remove();
              }}
            />
          )}
          {confirming === "early" && (
            <InlineConfirm
              title={"Check it before it ships?"}
              body={
                planName(plan) +
                " hasn't reached " +
                (plan.live_branch || nowLive || "the live branch") +
                " yet, so this checks out its own commit (" +
                (plan.sha ? plan.sha.slice(0, 7) : "its branch") +
                ") rather than what users have. Once it finishes, this checklist is " +
                "closed — it will NOT come back to \u201cNot checked yet\u201d when the " +
                "branch does ship."
              }
              confirmLabel="Check it early"
              cancelLabel="Keep it"
              busy={busy !== ""}
              onCancel={() => setConfirming("")}
              onConfirm={() => {
                setConfirming("");
                void startRun();
              }}
            />
          )}
          {/* THE RUN, HERE. Watching used to mean closing this whole dialog so a
              grid pane could open behind it — which threw away the checklist you
              were reading, on the surface where the checklist is the point, and
              left every step the agent handed back sitting unanswered in a window
              you had just been thrown out of. The same read-only stream renders
              inline, so "watch the agent" and "answer the steps it left you" are
              one screen. The pane is still one ⋯ item away for anyone who wants
              the agent on screen while they work elsewhere. */}
          {watchSession && watchingHere && (
            <RunWatch
              session={watchSession}
              onPopOut={watch}
              onHide={() => setWatchingHere(false)}
            />
          )}
          {steps.length > 0 && <ChecksHead plan={plan} />}
          {steps.length > 0 ? (
            <div className="vf-steps">
              {steps.map((step, i) => (
                <StepRow
                  key={step.id}
                  step={step}
                  index={i}
                  plan={plan}
                  onRecord={recordStep}
                  // Withheld while a session is already working this plan: a
                  // second run would race the first for the same result file.
                  // Withheld while anything else is starting a run — two
                  // starts for one plan race the same result file, and the
                  // server now refuses the second — but NOT while this step's
                  // own re-check is starting: the row wants to keep its item
                  // and relabel it "Re-checking…", which is the only feedback
                  // between the menu click and the toast seconds later.
                  onRunStep={
                    running || busy !== "" || (stepBusy !== "" && stepBusy !== step.id)
                      ? undefined
                      : (s) => startRun([s.id])
                  }
                  running={stepBusy === step.id}
                  // Withheld while a run is in flight for the same reason
                  // `onRunStep` is: the agent is working from these exact
                  // sentences right now, and the server 400s it anyway.
                  onEdit={
                    running ? undefined : (st, fields) => actions.editPlanStep(plan, st, fields)
                  }
                  onRemove={(st) => actions.removePlanStep(plan, st)}
                />
              ))}
            </div>
          ) : null}
          {/* Not while the plan is still being written: its step list is about
              to be replaced wholesale, and a step added into that window would
              look like it had been thrown away. Every other state can take one,
              including a plan that failed to generate — writing the checklist
              by hand is a perfectly good answer to "the model could not". */}
          {plan.state !== "generating" && (
            <details className="vf-addstep-fold">
              <summary>Add a step of your own</summary>
              <AddStep
                plan={plan}
                onAdded={(next) => {
                  patchPlan(plan.id, () => next);
                  void refreshTestPlans();
                }}
              />
            </details>
          )}
        </div>
      )}
    </div>
  );
}

/** The roll-up line above the steps — the "checks" summary.
 *
 * WHY THIS EXISTS NEXT TO A STATUS SENTENCE THAT ALREADY SAYS SOMETHING.
 * `planStatus` is one ordered decision producing one sentence, which is what
 * stops the row contradicting itself — but it can only ever say the LOUDEST true
 * thing ("2 steps need your eyes"), and the question somebody has on opening a
 * twelve-step checklist is a different one: how much of this is settled, and
 * how. That is the question a checks panel answers at a glance, and it is why
 * every CI surface has one. Computed from the same run the sentence read, so the
 * two are two renderings of one state rather than two opinions.
 *
 * Zeroes are omitted rather than shown as "0 failed". A row of zeroes reads as a
 * form, and the one number that matters is the one that is not zero. */
function ChecksHead({ plan }: { plan: TestPlan }) {
  const t = checkTally(plan);
  return (
    <div className="vf-checks-head">
      <span className="vf-checks-total">
        {t.total} {t.total === 1 ? "check" : "checks"}
      </span>
      {tallyBits(plan).map((bit) => (
        <span key={bit.state} className="vf-checks-bit" data-check={bit.state}>
          <span className="vf-mark-glyph" aria-hidden="true">
            {CHECK_MARK[bit.state]}
          </span>
          {bit.count} {bit.label}
        </span>
      ))}
    </div>
  );
}

/** THE SAME TALLY, ON THE COLLAPSED ROW — where the decision is actually made.
 *
 * Everything a person needs to triage a list of checklists was one click inside
 * each one: the row said whose turn it was ("2 steps need your eyes") and the
 * roll-up said where the work had got to (5 passed, 1 failed, 3 not started),
 * and only the first of those was visible without opening the plan. On a list
 * of eight, that is eight expansions to answer "is anything red, and how far
 * along is any of this" — which is the question the surface exists to answer.
 *
 * Glyph + number, not words: it shares its line with the status sentence, which
 * is the one thing on the row allowed to be long, and five labelled bits would
 * push it into an ellipsis two words from its start. The
 * words are still there for anyone who needs them — the `title`, and the
 * announced sentence beside it — and the glyphs and colours are the ones the
 * step rows and the legend already use, so nothing here is a new vocabulary.
 * Colour is never the only signal: each bucket has its own mark (✓ ✗ ● – ○). */
function PlanTally({ plan }: { plan: TestPlan }) {
  const bits = tallyBits(plan);
  const sentence = tallySentence(plan);
  if (!bits.length) return null;
  return (
    // `role="img"` + a label is what makes a group of glyphs announce as one
    // sentence instead of as five bare numbers; there is no visually-hidden
    // utility class in this app and inventing a global one for a tally would be
    // the wrong shape of change.
    <span className="vf-plan-tally" role="img" aria-label={sentence} title={sentence}>
      {bits.map((bit) => (
        <span
          key={bit.state}
          className="vf-plan-tally-bit"
          data-check={bit.state}
          aria-hidden="true"
        >
          <span className="vf-mark-glyph">{CHECK_MARK[bit.state]}</span>
          {bit.count}
        </span>
      ))}
    </span>
  );
}

/** The verify session, streamed into the checklist that asked for it.
 *
 * Read-only ON PURPOSE — `useWsTerm(..., false)` sets `disableStdin`. The run is
 * working a checklist it was given and its answers are the artifact; typing at it
 * mid-run produces a report about a conversation nobody can reconstruct later. If
 * you want to take over, the session is real and one ⋯ item away in its own
 * window. */
function RunWatch({
  session,
  onPopOut,
  onHide,
}: {
  session: string;
  onPopOut(): void;
  onHide(): void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  // RECONNECTING, unlike the grid pane. This watcher mounts the instant Run is
  // pressed — that is its whole point — and at that instant the session does not
  // exist: `create_instance` returns as soon as the record is registered and cuts
  // the worktree in the background. The first socket is refused, and without a
  // retry the box sat blank and "disconnected" forever while the run started
  // perfectly normally behind it.
  const state = useWsTerm(
    hostRef,
    "/api/instances/" + encodeURIComponent(session) + "/terminal",
    false,
    true
  );
  return (
    <div className="vf-runwatch">
      <div className="vf-runwatch-head">
        <span className="vf-runwatch-title">{session}</span>
        <span className="vf-runwatch-state" data-state={state}>
          {state === "starting"
            ? "starting the session…"
            : state === "reconnecting"
              ? "reconnecting…"
              : state === "streaming"
                ? "live"
                : state}
        </span>
        <button
          type="button"
          className="test-btn"
          onClick={onPopOut}
          title="Open it as a grid window so you can keep it while you work elsewhere"
        >
          Own window
        </button>
        <button
          type="button"
          className="test-btn"
          onClick={onHide}
          title="Hide this view — the run keeps going"
        >
          Hide
        </button>
      </div>
      <div className="vf-runwatch-term" ref={hostRef} />
    </div>
  );
}
