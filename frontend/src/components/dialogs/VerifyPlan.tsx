/** One Verify checklist row: the three-line header (what it is, whose turn,
 * what to press), its ⋯ menu, and the expanded body — the run watched inline,
 * the roll-up, and the steps (VerifyStep.tsx).
 *
 * Split out of VerifyDialog.tsx. Every request a row sends lives in
 * lib/verifyActions.ts; this file owns which button is busy and which box is
 * open. */

import { useEffect, useRef, useState } from "react";
import type { TestPlan, TestStep, TestStepResult } from "../../api/types";
import { refreshTestPlans, useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { selectSession } from "../../lib/sessionActions";
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
  beforeMergeRun,
  beforeMergeSummary,
  failCount,
  isConfigBlocked,
  isNoCommits,
  planVerdict,
  stepCheck,
  latestRun,
  openHumanSteps,
  isVerifySession,
  planForSession,
  planName,
  planStatus,
  runEvidence,
  runTreeMismatch,
  tallyBits,
  tallySentence,
} from "./verify";
import { VerdictBadge } from "./VerdictChip";
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
  reveal,
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
  /** A request from outside the row — a deep link, or "Write steps yourself"
   * — to bring this checklist into view (and open its Add a step form).
   * `nonce` changes per request so the same one can be made twice. */
  reveal?: { nonce: number; addStep?: boolean };
}) {
  const closeDialog = useUi((s) => s.closeDialog);
  const openVerifyPane = useUi((s) => s.openVerifyPane);
  // The session this checklist is for, when it is still open — what "Open
  // session", "View diff" and "Open PR" in the ⋯ need.
  const { data: instances } = useInstances();
  const session =
    (instances || []).find((i) => !isVerifySession(i) && planForSession([plan], i) === plan) ||
    null;
  // A fix session the route opened (`fix-<plan>`) that is still open: while it
  // is, the card says the failure is being fixed (B1) — the press otherwise
  // left no trace on the checklist it came from.
  const fixSession = (instances || []).find((i) => i.title === "fix-" + plan.id) || null;
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
  const rowRef = useRef<HTMLDivElement>(null);
  const [addOpen, setAddOpen] = useState(false);

  const run = latestRun(plan);
  // A check made before merge on a plan that has since shipped — kept, and
  // named, but no longer the current answer (see `latestRun`).
  const earlier = beforeMergeRun(plan);
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
  const evidence = runEvidence(run, plan.live_branch || liveBranch, plan.branch);
  const failedCount = failCount(plan);
  const steps = plan.steps || [];
  const status = planStatus(plan, liveBranch);
  const open = openHumanSteps(plan);
  const verdict = planVerdict(plan);
  // CHECK NOW, VISIBLE (M3): on every card with an agent step nobody has
  // answered, beside the row's main button — not only when it IS the main
  // button. The intro's first instruction is "press Check now", and on a
  // failed or needs-you card it lived in the ⋯.
  const uncheckedAgent = steps.filter(
    (s) => s.actor !== "human" && stepCheck(plan, s.id) === "pending",
  ).length;
  const statusRuns =
    status.action === "run" || status.action === "rerun" || status.action === "early";
  const showCheckNow =
    !running && plan.state !== "generating" && uncheckedAgent > 0 && !statusRuns;
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
  // "not merged", never "local": a checklist exists because the branch was
  // PUSHED, and to a git user "local" means the opposite. `live_at` without a
  // `merged_into` is an older payload — the rung that answers "merged into
  // what?" is newer than the one that stamps "this is live" — so it falls back
  // to the branch the plan is measured against.
  const landed = plan.merged_into || (plan.live_at ? nowLive || "" : "");
  const where = landed
    ? "merged \u2192 " + landed
    : plan.live_at
      ? "shipped"
      : plan.merged_at
        ? "merged"
        : "not merged";
  // "" for a landing the rung that answered could not date — a squash merge is
  // known by its PR, and a PR does not carry the moment it merged.
  const whereAgo = agoOf(
    plan.merged_into_at || plan.live_at || plan.merged_at || plan.generated_at,
  );
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
            : "Pushed to " + (plan.branch || "its own branch") + " — not merged anywhere yet.",
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

  // Brought here from outside (see `reveal`): scroll the row into view once the
  // body has rendered, and open + focus Add a step when that was the ask.
  useEffect(() => {
    if (!reveal?.nonce) return;
    if (reveal.addStep) setAddOpen(true);
    const id = window.setTimeout(() => {
      const row = rowRef.current;
      if (!row) return;
      row.scrollIntoView({ block: "start", behavior: "smooth" });
      if (reveal.addStep)
        row.querySelector<HTMLInputElement>(".vf-addstep-text")?.focus({ preventScroll: true });
    }, 60);
    return () => window.clearTimeout(id);
  }, [reveal?.nonce, reveal?.addStep]);

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
    withBusy(only?.length === 1 ? "fix:" + only[0] : "fix", () =>
      actions.fixPlanFailures(plan, only),
    );
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
    // "early" too: Check now on work that hasn't merged. It used to sit behind
    // a ⋯ item and a confirm warning that it closed the checklist for good;
    // the server now keeps it open, so it is just a run.
    void startRun();
  };

  // Every applicable action EXCEPT the one already on the primary button.
  const menu: MenuItem[] = [];
  // THE WAY BACK to the work this checklist is about — the session, its diff,
  // its PR. Only while that session is open; a closed one has no pane to show.
  if (session) {
    const go = () => {
      closeDialog();
      selectSession(session.title);
    };
    menu.push({
      key: "open-session",
      label: "Open the work session",
      title: "Show " + session.title + " in the grid",
      onSelect: () => go(),
    });
    menu.push({
      key: "view-diff",
      label: "View diff",
      title: "Show what changed on " + (plan.branch || "this branch"),
      onSelect: () => {
        closeDialog();
        useUi.getState().openDiff(session.title);
      },
    });
    menu.push({
      key: "could-break",
      label: "What could break",
      title: "Open the map on what imports the changes on " + (plan.branch || "this branch"),
      onSelect: () => {
        closeDialog();
        useUi.getState().openMap(session.title, { section: "risk" });
      },
    });
    if (session.pr_url)
      menu.push({
        key: "open-pr",
        label: "Open PR \u2197",
        title: session.pr_url,
        onSelect: () => void window.open(session.pr_url!, "_blank"),
      });
  }
  // BRING THE LOG BACK. Opening it inline is otherwise reachable only while the
  // plan is `running` (the primary button's "watch" arm), so the terminal that
  // vanished when the run finished — the moment its own failure arrived — could
  // not be reopened at all, and the only other route to it, below, closes the
  // dialog to show a grid pane. Offered whenever there is a session to show.
  if (watchSession && !watchingHere)
    menu.push({
      key: "watch-here",
      label: running ? "Show the checking run here" : "Show the last checking run here",
      title: "Show " + watchSession + " under this checklist",
      onSelect: () => setWatchingHere(true),
    });
  if (watchSession)
    menu.push({
      key: "watch",
      label: "Open the checking run",
      title: "Open " + watchSession + " as a pane — closing it doesn't stop the check",
      onSelect: watch,
    });
  if (running)
    menu.push({
      key: "cancel",
      label: "Cancel run",
      title: "Stop the agent — what it found so far is kept",
      className: "test-btn vf-cancel",
      onSelect: () => void cancelRun(),
    });
  // Only while it is actually waiting on a pipeline — see the route, which
  // refuses anything else rather than silently reopening a finished checklist.
  if (plan.state === "generated" && plan.merged_at)
    menu.push({
      key: "deployed",
      label: "It's deployed — check it now",
      title: "Don't wait for the deploy timer",
      onSelect: () => void markDeployed(),
    });
  if (!running && steps.length && plan.state !== "generating") {
    // Whether an agent could do anything here at all. A checklist whose every
    // step is a person's own is not runnable — the run prompt forbids the agent
    // from settling one — so offering it would spend a worktree and minutes of
    // a billed session to hand everything straight back.
    const agentCan = steps.some((s) => s.actor !== "human");
    // Every state whose primary button is something else — notably a recorded
    // failure, whose button opens the evidence. Re-checking after a fix is the
    // right thing to do second, not first. (Check now on unmerged work is the
    // PRIMARY button now, not a ⋯ item.)
    if (agentCan && !statusRuns && !showCheckNow)
      menu.push({
        key: "run",
        label: "Check now",
        title: "An agent checks the steps it can",
        onSelect: () => void startRun(),
      });
  }
  if (plan.state !== "generating" && status.action !== "rewrite")
    menu.push({
      key: "regen",
      label: "Rewrite the checklist",
      title: "Ask the agent to write the steps again",
      onSelect: () => {
        onExpand();
        setRewriting(true);
      },
    });
  menu.push({
    key: "delete",
    label: "Delete this checklist",
    danger: true,
    title: "Delete this checklist and its answers",
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
      ref={rowRef}
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
          {/* THE VERDICT first — the yes-or-no a lead reads before merging. */}
          {/* ONE COUNTER PER CARD (m1): a partly-checked verdict IS a count
              ("1 of 3 checked"), so the tally beside the sentence carries it
              alone and the pill is dropped. */}
          {verdict.key !== "partial" ? <VerdictBadge plan={plan} /> : null}
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
            {isConfigBlocked(plan) ? <ConfigLine line={status.line} /> : status.line}
            {fixSession && failedCount > 0 ? (
              <span className="vf-fixing">
                {" "}Being fixed in{" "}
                <button
                  type="button"
                  className="linklike"
                  onClick={() => {
                    closeDialog();
                    selectSession(fixSession.title);
                  }}
                >
                  {fixSession.title}
                </button>
                .
              </span>
            ) : null}
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
            data-tour="verify-merge"
            data-live={landed === nowLive && !!nowLive ? "yes" : "no"}
            title={whereTitle}
          >
            {where}
            {whereAgo ? " · " + whereAgo : ""}
          </span>
        </div>
        {/* ONE primary button per row at most: filled only when the row is
            asking something of YOU; every other row's button is a plain one,
            so a list of eight is not eight purple buttons. A failed row also
            offers Fix… beside it — the most useful next press there. */}
        <span className="vf-acts">
        {!running && failedCount > 0 ? (
          <button
            type="button"
            className="vf-fix"
            disabled={busy !== ""}
            title={
              fixSession
                ? "Sends the failure to " + fixSession.title + " again"
                : "Starts a new session with the failure attached"
            }
            onClick={() => void fixFailures()}
          >
            {busy === "fix" ? "Opening…" : "Fix in a new session"}
          </button>
        ) : null}
        {showCheckNow ? (
          <button
            type="button"
            className="test-btn vf-checknow"
            data-tour="verify-check"
            disabled={busy !== "" || stepBusy !== ""}
            title={
              plan.live_at
                ? "An agent checks the steps it can — takes a few minutes"
                : "An agent checks the branch now — it's checked again after merge"
            }
            onClick={() => void startRun()}
          >
            {busy === "run" ? "Starting…" : "Check now"}
          </button>
        ) : null}
        {status.action !== "none" ? (
          <button
            type="button"
            className={(status.tone === "you" ? "btn-primary" : "test-btn") + " vf-primary"}
            data-action={status.action}
            data-tour={statusRuns ? "verify-check" : undefined}
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
                  ? "Show the steps that failed"
                  : status.tone === "ok"
                    ? "Show every step and its answer"
                    : status.tone === "warn"
                      ? "Show the steps you couldn't check"
                      : "Show the steps that need you"
                : status.action === "watch"
                  ? "Show " + (plan.run_session || "the agent") + " at work"
                  : status.action === "rewrite"
                    ? "Ask the agent to write the steps again"
                    : status.action === "early"
                      ? "An agent checks the branch now — it's checked again after merge"
                      : // The cost, said plainly: minutes of a real agent.
                        "An agent checks the steps it can — takes a few minutes"
            }
            onClick={doPrimary}
          >
            {busy === "run" ? "Starting…" : busy === "regen" ? "…" : status.actionLabel}
          </button>
        ) : null}
        </span>
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
              <strong>
                {steps.length ? "That didn't work." : "Not written yet."}
              </strong>{" "}
              {plan.error || "no reason recorded"}
              <div className="vf-note">
                {steps.length
                  ? "The steps below are unchanged."
                  : isNoCommits(plan)
                    ? "Comes back automatically when the session commits — or add steps yourself below."
                    : "Rewrite to try again, or add steps yourself below."}
              </div>
            </div>
          )}
          {/* The second entrance to the one-way door, and the quieter one:
              `record_result` closes ANY plan to "done" the moment nothing is
              unanswered, liveness be damned. So a helpful person working through
              a pre-live checklist by hand silently removes it from the due
              pipeline — with nothing anywhere on the screen having said so. */}
          {/* Before merge: what an answer here is about, and what happens
              next. (It used to warn that answering closed the checklist for
              good; the server keeps it open now.) */}
          {earlier && (
            <div className="vf-note vf-moment">
              Checked before merge: {beforeMergeSummary(plan, earlier)}
              {agoOf(earlier.at) ? ", " + agoOf(earlier.at) : ""}. This is the check
              after merge.
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
              Checked against <code>{plan.live_branch}</code>; this repo now ships from{" "}
              <code>{nowLive}</code>.
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
              That check wasn't on {nowLive || "the live branch"} — it ran on{" "}
              <code>{(run?.tested_sha || "").slice(0, 7)}</code>, so its passes don't
              count. Check again.
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
                  ? "This also stops " + (plan.run_session || "the agent checking it") + "."
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
                  onFix={
                    running || busy !== "" ? undefined : (st) => void fixFailures([st.id])
                  }
                  fixing={busy === "fix:" + step.id}
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
            <details
              className="vf-addstep-fold"
              open={addOpen}
              onToggle={(e) => setAddOpen((e.currentTarget as HTMLDetailsElement).open)}
            >
              <summary>Add a step</summary>
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
          {bit.count} {bit.short}
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

/** A config-blocked status line with its "Edit repositories" made a link
 * (m10): it opens the repositories editor above the list. */
function ConfigLine({ line }: { line: string }) {
  const at = line.indexOf("Edit repositories");
  if (at < 0) return <>{line}</>;
  return (
    <>
      {line.slice(0, at)}
      <button
        type="button"
        className="linklike"
        onClick={() => {
          const btn = document.getElementById("vf-repos-edit");
          if (!btn) return;
          if (btn.getAttribute("aria-expanded") !== "true") btn.click();
          btn.scrollIntoView({ block: "center", behavior: "smooth" });
        }}
      >
        Edit repositories
      </button>
      {line.slice(at + "Edit repositories".length)}
    </>
  );
}
