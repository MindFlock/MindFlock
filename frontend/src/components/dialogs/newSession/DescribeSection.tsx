/** The New dialog's describe/plan flow: the one-sentence box (page 1's whole
 * body — one session, or an Intake batch's list), the handlers that send it to POST /api/session-plan and write the answer into
 * the form, and the confirm row for a folder a plan wants made. State stays
 * in NewSessionDialog; these read and write it through what they are handed. */

import { useEffect, type Dispatch, type MutableRefObject, type ReactNode, type SetStateAction } from "react";
import type { PlanAnswer } from "../../../api/types";
import type { deviceApi } from "../../../lib/devices";
import { errMsg } from "../../../lib/format";
import { BROWSE_VALUE, removeItem, runRepoOptions } from "../../../lib/runStart";
import { RunItems, RunOptions, type RunDraft } from "../NewList";
import {
  DESCRIBE_CAP_MS,
  DESCRIBE_MAX_CHARS,
  SUGGEST_SOURCES,
  DESCRIBE_SLOW_MS,
  SUBMIT_ARM_MS,
  cancelPlanRun,
  describeBlockReason,
  enterAction,
  immediateStartBlockReason,
  leafName,
  planInPlace,
  startPlanRun,
  type FolderAction,
  type PlanFolderAction,
  type PlanRun,
  type RepoSuggestion,
} from "../newSession";
import { useFolderPickerWhile } from "../../../lib/folderPicker";
import { Elapsed } from "./FooterActions";
import { useUi } from "../../../state/store";

type Set<T> = Dispatch<SetStateAction<T>>;

/** The create body's plan-decided fields, as newSessionSubmit's buildBody takes them. */
type BodyFields = {
  title: string;
  repoPath: string;
  prompt: string;
  inPlace: boolean;
  initRepo: boolean;
  provisioned: boolean;
};

export interface DescribeFlowCtx {
  describe: string;
  /** The Task box, and the text "Set it up myself" last carried into it. */
  prompt: string;
  carried: MutableRefObject<string>;
  /** Create session without a plan in hand: start from the sentence as
   * typed, in the folder the form holds (NewSessionDialog's startRaw). */
  startRaw(): void;
  describing: boolean;
  newFolderOk: boolean;
  dapi: ReturnType<typeof deviceApi>;
  planRun: MutableRefObject<PlanRun>;
  lastAnswer: MutableRefObject<{ sentence: string; answer: PlanAnswer } | null>;
  submitArmAt: MutableRefObject<number>;
  focusFolderNext: MutableRefObject<boolean>;
  foldOpenedByPlan: MutableRefObject<boolean>;
  folderDo: Dispatch<FolderAction>;
  planFolderDo: Dispatch<PlanFolderAction>;
  setError: Set<string>;
  setTitle: Set<string>;
  setPrompt: Set<string>;
  setInPlace: Set<boolean>;
  setInitRepo: Set<boolean>;
  setNewFolderOk: Set<boolean>;
  setProvision: Set<boolean>;
  setAdvancedOpen: Set<boolean>;
  setPromptOpen: Set<boolean>;
  setPlanError: Set<string>;
  setPlanNote: Set<string>;
  setDescribing: Set<boolean>;
  setDescribeSlow: Set<boolean>;
  /** Which button started the read, and when (the button shows a clock). */
  setDescribeMode: Set<"fill" | "start">;
  setDescribeSince: Set<number>;
  /** The read failed or ran out of time: offer the form, pre-filled. */
  setDescribeFailed: Set<boolean>;
  setPage: Set<number>;
  buildBody(p: BodyFields): Record<string, unknown>;
  postCreate(body: Record<string, unknown>): Promise<void>;
}

/** The describe box's handlers, built per render like the closures they were. */
export function describeFlow(ctx: DescribeFlowCtx) {
  const {
    describe,
    prompt,
    carried,
    startRaw,
    describing,
    newFolderOk,
    dapi,
    planRun,
    lastAnswer,
    submitArmAt,
    focusFolderNext,
    foldOpenedByPlan,
    folderDo,
    planFolderDo,
    setError,
    setTitle,
    setPrompt,
    setInPlace,
    setInitRepo,
    setNewFolderOk,
    setProvision,
    setAdvancedOpen,
    setPromptOpen,
    setPlanError,
    setPlanNote,
    setDescribing,
    setDescribeSlow,
    setDescribeMode,
    setDescribeSince,
    setDescribeFailed,
    setPage,
    buildBody,
    postCreate,
  } = ctx;

  /** Write a plan from the Describe box into the form the user is looking at.
   *
   * Deliberately NOT fillFromTemplate, whose last third is wrong for this caller
   * three ways. Its `setAdvancedOpen(!!(t.provisioned || t.init_repo ||
   * !t.in_place))` evaluates to FALSE for the commonest plan there is — an
   * existing repo, in place, no init — so reusing it would CLOSE "Git &
   * workspace" and hide the two checkboxes the model had just decided, which is
   * the exact opposite of what this box is for. Its `setProvision(!!...)` would
   * silently untick a Provision box the user armed by hand. And its
   * `setActiveTemplate(t.name)` would write a session title into the state that
   * highlights a template chip, lighting up whichever template happens to share
   * the name.
   *
   * Every field is written unconditionally (except the title — see below),
   * because the button says it replaces the form, and a half-replaced form is
   * one whose leftover values are indistinguishable from the new ones. */
  const applyPlan = (a: PlanAnswer) => {
    setError("");
    // Through the reducer like every other way of naming a folder, so the rest
    // follows by itself: the debounced /api/repos/check probe fires on the new
    // path (it is absolute, so looksLikePath waves it through), the git nudge
    // appears under a plain folder, the matching suggestion chip lights up, and
    // the name-search effect clears its matches rather than leaving a stale list
    // hanging under a path.
    folderDo({ t: "user-set", path: a.repo_path });
    // A blank title is the server declining to name the session, not an
    // instruction to erase the name the user typed before reaching for the box.
    if (a.title) setTitle(a.title);
    setPrompt(a.prompt || "");
    setInPlace(planInPlace(a));
    setInitRepo(!!a.init_repo);
    // The whole folder, existing or not: the gate reads the "does it need
    // making" half of it and the note reads the path. Keeping a path only for a
    // NEW folder is what left the note with nothing to compare itself against.
    // Reading a MISSING folder_exists as "not there" lives in the reducer,
    // beside the comment about why.
    planFolderDo({ t: "answer", plan: a });
    // ALWAYS false, including over a plan that proposes the same folder as the
    // last one: a tick left over from the previous sentence is consent to that
    // sentence's folder, not to this one's. Every fill asks again.
    setNewFolderOk(false);
    // The plan never provisions — `provisioned` is not in the wire contract and
    // the server never emits it — so this is a clear, not a copy.
    setProvision(false);
    // Never CLOSE a fold the plan has just written into. Unconditional on
    // purpose; see the fillFromTemplate comparison above.
    setAdvancedOpen(true);
    if (a.prompt) {
      foldOpenedByPlan.current = true;
      setPromptOpen(true);
    }
  };

  /** Stop waiting. The seq bump is what actually cancels — it makes the answer
   * a no-op whenever it lands — and the abort only saves the socket. The
   * subprocess on the far side is not killed by this and runs to its own
   * timeout; it is a read-only one-shot with stdin closed, so that is bounded
   * and harmless rather than something worth building a kill channel for. */
  /** Page 1's "Create session": it never waits on a model turn. With a
   * plan already in hand for this very sentence (Review, then Back) it acts
   * on that plan; otherwise it starts from the sentence as typed, in the
   * folder the form holds (startRaw) — the line under the box names it.
   *
   * Acting on the cached plan is also what a second press after ticking a
   * new folder's confirm needs: a fresh turn could answer with a different
   * folder than the one the user just said yes to. The cache is keyed on the
   * sentence, so editing the box drops it. */
  const startNow = () => {
    const cached = lastAnswer.current;
    if (cached && cached.sentence === describe.trim()) {
      cancelPlanRun(planRun.current);
      setDescribing(false);
      setDescribeSlow(false);
      void startFromPlan(cached.answer);
      return;
    }
    startRaw();
  };

  const cancelDescribe = () => {
    cancelPlanRun(planRun.current);
    setDescribing(false);
    setDescribeSlow(false);
  };

  /** The way to the form — "Set it up myself instead", and the way out
   * when the agent did not answer: what was typed goes with it as the Task,
   * so nothing typed is ever lost. A Task edited since an earlier trip is
   * the user's and stays; one still holding the last carried sentence takes
   * the new one. */
  const setUpMyself = () => {
    cancelPlanRun(planRun.current);
    setDescribing(false);
    setDescribeSlow(false);
    setDescribeFailed(false);
    setPlanError("");
    const text = describe.trim();
    if (text && (!prompt.trim() || prompt.trim() === carried.current)) {
      setPrompt(text);
      carried.current = text;
    }
    setPage(2);
  };

  /** Ask the server to read the sentence and fill the form in. */
  /** Start an immediate create from a plan, or refuse and say why.
   *
   * Page 1's "Start session" is the "don't make me read the form" path, and it
   * skips page 2 entirely — but it does NOT skip the one question that is not a
   * validation: a folder that does not exist yet still has to be agreed to in
   * as many words, because nothing else in this flow makes something that
   * outlives the session. So the first press shows the question and stops, and
   * the second press — with the tick on, and reusing the answer rather than
   * paying for a second model turn — creates.
   *
   * The body comes from the ANSWER, not from the fields applyPlan has just
   * written: those setStates are queued, so `inPlace` and friends still hold
   * the previous opening's values at this point. applyPlan still runs, so that
   * anything which stops the create leaves the user on a form that agrees with
   * the sentence instead of an empty one. */
  const startFromPlan = async (a: PlanAnswer) => {
    // Not an error — the plan is good and the question is the point. The confirm
    // row is already on screen (it derives from the folder applyPlan just
    // wrote), so this only has to say which press comes next.
    const ask = immediateStartBlockReason({
      // `!== true`, the same direction applyPlan reads it: of the two ways to be
      // wrong about a missing key, only one of them makes a directory nobody
      // was shown.
      folderExists: a.folder_exists === true,
      folderLabel: a.folder_display || a.repo_path || "that folder",
      confirmed: newFolderOk,
    });
    if (ask) {
      setPlanError(ask);
      return;
    }
    await postCreate(
      buildBody({
        title: a.title || "",
        repoPath: a.repo_path || "",
        prompt: a.prompt || "",
        inPlace: planInPlace(a),
        initRepo: !!a.init_repo,
        // A plan never provisions — `provisioned` is not in the wire contract
        // and the server never emits it — so an immediate start never does
        // either, whatever the form happened to be holding.
        provisioned: false,
      })
    );
  };

  const runDescribe = async (mode: "fill" | "start" = "fill") => {
    // The in-flight guard comes FIRST, before describeBlockReason is consulted,
    // because that one cannot carry it: it answers "" while a turn is in flight
    // on purpose (the button is already saying "Reading…"). The button is
    // disabled, but the box is readOnly rather than disabled — it keeps focus,
    // so Enter goes on firing, and each press used to buy another
    // POST /api/session-plan and another headless CLI turn. Claiming the slot
    // for a sentence the check below then refuses costs nothing: nothing is in
    // flight to invalidate, and the controller it makes never gets a request.
    const started = startPlanRun(planRun.current, describing);
    if (!started) return;
    const { seq, ctl } = started;
    const blocked = describeBlockReason({ text: describe, busy: describing });
    if (blocked) {
      // Said out loud rather than silently ignored: both the button and Enter
      // come through here, and neither is disabled, so a refusal that showed
      // nothing would read as a dead control.
      setPlanError(blocked);
      return;
    }
    setDescribing(true);
    setDescribeSlow(false);
    setDescribeMode(mode);
    setDescribeSince(Date.now());
    setDescribeFailed(false);
    setPlanError("");
    // Last run's note described last run's folder. Clearing it up front means
    // the strip never shows a sentence about a form that is being replaced.
    setPlanNote("");
    const slow = window.setTimeout(() => {
      if (planRun.current.seq === seq) setDescribeSlow(true);
    }, DESCRIBE_SLOW_MS);
    // The cap: past it the wait stops, the late answer is dropped (the seq
    // bump), and the form is offered with the sentence as its Task.
    const cap = window.setTimeout(() => {
      if (planRun.current.seq !== seq) return;
      cancelPlanRun(planRun.current);
      setDescribing(false);
      setDescribeSlow(false);
      setPlanError("The agent didn't answer within " + Math.round(DESCRIBE_CAP_MS / 1000) + " seconds.");
      setDescribeFailed(true);
    }, DESCRIBE_CAP_MS);
    try {
      const a = await dapi<PlanAnswer>("/api/session-plan", {
        json: { text: describe.trim().slice(0, DESCRIBE_MAX_CHARS) },
        signal: ctl.signal,
      });
      if (planRun.current.seq !== seq) return;
      applyPlan(a);
      setPlanNote(a.note || "");
      // Kept so "Start session" can be pressed a second time — after ticking a
      // new folder's confirm — without buying another CLI turn, and without the
      // risk that the second turn answers with a DIFFERENT folder than the one
      // just agreed to.
      lastAnswer.current = { sentence: describe.trim(), answer: a };
      if (mode === "start") {
        // Straight to the create. Nothing below applies: there is no form to
        // hold, no caret to move, and no page 2 to move it on.
        setDescribing(false);
        setDescribeSlow(false);
        void startFromPlan(a);
        return;
      }
      setPage(2);
      // Create is held for a breath before the caret moves, not after: the
      // answer arrives ~10-25s after the keystroke that asked for it, by which
      // time the user's hand may be back on Enter for some other reason.
      submitArmAt.current = Date.now() + SUBMIT_ARM_MS;
      // The Folder field, not Create: it holds the value most likely to be
      // wrong, it is what the note is about, and focusing it scrolls it into
      // view — .nf-body is the scroll region and a filled-in form is taller than
      // the empty one that asked for it, so the folder can easily be above the
      // fold by now. Plain Enter there still submits (that field only swallows
      // Enter for a NAME, and a plan's path is never one), so the two-Enter path
      // survives with the caret on the thing you would be confirming.
      // Deferred to an effect, because the Folder field is on page 2 and page 2
      // has not rendered yet — repoRef.current is still null on this line.
      focusFolderNext.current = true;
    } catch (err) {
      if (planRun.current.seq !== seq) return;
      // Our own cancel, not a failure: cancelDescribe has already put the button
      // back and there is nothing to report.
      if ((err as Error)?.name === "AbortError") return;
      // Inline, never a toast: toast is a 1.4s strip at the bottom of the screen
      // that lands behind this modal, while this is a field-level failure in an
      // open dialog — which is exactly what #new-error and MakePrDialog's .error
      // line already are. The remedy never changes and is already on screen, so
      // the sentence names it.
      // The way out is the button beside it ("Fill the form with what I
      // typed"), so the sentence only says what went wrong.
      setPlanError(errMsg(err));
      setDescribeFailed(true);
    } finally {
      window.clearTimeout(slow);
      window.clearTimeout(cap);
      // Guarded like every other landing: a later run owns the button now, and
      // an overtaken run must not turn its spinner off.
      if (planRun.current.seq === seq) {
        setDescribing(false);
        setDescribeSlow(false);
        planRun.current.abort = null;
      }
    }
  };

  return {
    applyPlan,
    startNow,
    cancelDescribe,
    startFromPlan,
    runDescribe,
    setUpMyself,
    fillFormInstead: setUpMyself,
  };
}

/** The focus moves that belong to the describe flow itself, plus the
 * opening's deferred one (see NewSessionDialog's opening focus). */
export function useDescribeFocus({
  open,
  page,
  focusFolderNext,
  focusOpeningNext,
  openedFocus,
  openedPage,
  repoRef,
  titleRef,
  describeRef,
}: {
  open: boolean;
  page: number;
  focusFolderNext: MutableRefObject<boolean>;
  focusOpeningNext: MutableRefObject<boolean>;
  openedFocus: MutableRefObject<"describe" | "title">;
  openedPage: MutableRefObject<number>;
  repoRef: MutableRefObject<HTMLInputElement | null>;
  titleRef: MutableRefObject<HTMLInputElement | null>;
  describeRef: MutableRefObject<HTMLTextAreaElement | null>;
}) {
  /** Move the caret to the Folder field once page 2 is actually on screen.
   *
   * runDescribe cannot do this itself: it sets the page and the field it wants
   * is rendered by that same update, so repoRef.current is still null on the
   * line after. Focusing also SCROLLS the field into view, which is half the
   * point — a filled-in form is taller than the sentence that asked for it. */
  useEffect(() => {
    if (page !== 2 || !focusFolderNext.current) return;
    focusFolderNext.current = false;
    const el = repoRef.current;
    if (!el) return;
    // Top of the form FIRST, then the caret without moving it again.
    // focus() scrolls its target into view all by itself, which landed page 2
    // already scrolled — Folder pinned to the top edge and Name cut off above
    // it, so the page you had just been sent to appeared to start in the
    // middle. preventScroll keeps the caret where the value most likely to be
    // wrong is, while the page still reads from its own beginning.
    const body = el.closest(".nf-body");
    if (body) body.scrollTop = 0;
    el.focus({ preventScroll: true });
    el.setSelectionRange(el.value.length, el.value.length);
  }, [page]);

  // The opening's target, once the page it asked for is on screen.
  useEffect(() => {
    if (!open || !focusOpeningNext.current || page !== openedPage.current) return;
    const el = openedFocus.current === "title" ? titleRef.current : describeRef.current;
    if (!el) return;
    focusOpeningNext.current = false;
    el.focus();
  }, [open, page]);
}

export interface DescribeSectionProps {
  batch: boolean;
  describe: string;
  setDescribe: Set<string>;
  describing: boolean;
  describeRef: MutableRefObject<HTMLTextAreaElement | null>;
  runMode: boolean;
  runDescribe(mode?: "fill" | "start"): Promise<void>;
  cancelDescribe(): void;
  draft: RunDraft;
  planNoteShown: string;
  planError: string;
  setPlanError: Set<string>;
  /** The read failed or timed out: show the way to the form. */
  describeFailed: boolean;
  fillFormInstead(): void;
  newFolderRow: ReactNode;
  runBrowse: boolean;
  setRunBrowse: Set<boolean>;
  repoPath: string;
  folderDo: Dispatch<FolderAction>;
  suggestions: RepoSuggestion[];
  device: string;
  homePath: string;
  /** Clear (shown while something typed is in the form). */
  onClear?: () => void;
  /** A note about the agent (not signed in), shown above the buttons. */
  agentNote?: ReactNode;
  /** What the "Runs in" line says after the folder (the branch, or that it
   * runs in the folder itself). */
  startsIn: ReactNode;
  /** The agent's name for "Asking Claude Code to fill in the details…". */
  agentLabel: string;
  /** When the read in flight started (epoch ms). */
  describeSince: number;
  /** The timeout's one recovery: start now from the words as typed. */
  startInstead(): void;
  /** Enter in the box: Start. */
  onEnterStart(): void;
}

/** Page 1's body: the sentence box, what it is for, and — for an Intake
 * batch — the list's rows and run options. */
export function DescribeSection({
  batch,
  describe,
  setDescribe,
  describing,
  describeRef,
  runMode,
  runDescribe,
  cancelDescribe,
  draft,
  planNoteShown,
  planError,
  setPlanError,
  describeFailed,
  fillFormInstead,
  newFolderRow,
  runBrowse,
  setRunBrowse,
  repoPath,
  folderDo,
  suggestions,
  device,
  homePath,
  onClear,
  agentNote,
  startsIn,
  agentLabel,
  describeSince,
  startInstead,
  onEnterStart,
}: DescribeSectionProps) {
  // "Runs in" → Browse… (one session) or the list's "tasks start in" →
  // Browse…: the same floating picker window every path field opens (never a
  // browser dialog: Electron has none). A click fills the picker; "Use this
  // folder", ✕ and Escape all leave the folder last clicked, as Done did.
  useFolderPickerWhile(runBrowse, () => ({
    title: runMode ? "Pick the folder the tasks start in" : "Pick the folder it runs in",
    device,
    start: repoPath || homePath || "",
    selected: repoPath,
    anchor: document.querySelector<HTMLElement>("#new-run-repo, #new-describe-folder"),
    onSelect: (p) => folderDo({ t: "user-set", path: p }),
    onPick: (p) => {
      folderDo({ t: "user-set", path: p });
      setRunBrowse(false);
    },
    onCancel: () => setRunBrowse(false),
  }));
  /* The one-sentence door, the whole of page 1 because it is the widest of
     the three ways to fill this form in (a sentence, a template chip, or by
     hand) and comes first as the least specific. It wears .new-templates so
     it is the same card as the Templates and folder-suggestion strips rather
     than a second form stacked on the first — nothing here is submitted, and
     looking like a form would say otherwise. */
  return (
    <div id="new-describe" className="new-templates nf-describe">
      <div className="nt-head">
        <span id="new-describe-label">
          {batch ? "Starting together — one session per line or ticket ID" : "What do you want to work on?"}
        </span>
      </div>
      <div className="nf-describe-row">
        {/* A textarea, so a longer task can be typed or pasted: three
            lines tall, growing to eight. However many lines, it is ONE
            prompt (only Intake's "Start together…" opens New as a list).
            Enter keeps its old meaning ("read this"); Shift+Enter starts
            another line, and in a batch or an auto-split Enter does too. */}
        <textarea
          id="new-describe-text"
          // The question above it is its name.
          aria-labelledby="new-describe-label"
          ref={describeRef}
          rows={Math.min(8, Math.max(3, describe.split("\n").length))}
          value={describe}
          maxLength={DESCRIBE_MAX_CHARS}
          autoComplete="off"
          spellCheck={false}
          // readOnly rather than disabled: a disabled input loses focus to
          // the body, so the caret would jump out of the box the moment
          // Enter was pressed and the sentence would stop being
          // selectable while the user waits to see what it produced.
          readOnly={describing}
          data-tour="new-task"
          placeholder="e.g. fix the login bug in acme-api"
          onChange={(e) => {
            setDescribe(e.target.value);
            // Typing means the folder on the "Runs in" line is the one being
            // used: a folder suggestion that lands late must not swap it.
            folderDo({ t: "keep" });
            // Typing is what clears the refusal: whatever it objected to
            // has just changed, and a stale red line under a box the user
            // is actively fixing is noise.
            setPlanError("");
          }}
          onKeyDown={(e) => {
            if (e.key !== "Enter" || e.ctrlKey || e.metaKey) return;
            // A new line: Shift+Enter always, plain Enter in a list.
            if (enterAction({ field: "describe", shift: e.shiftKey, runMode }) === "newline") {
              e.stopPropagation();
              return;
            }
            // Enter here is Start — the sentence as the task, in the folder
            // the "Runs in" line shows. Never the form's submit: this input
            // sits inside <form id="new-form" onSubmit={submit}>, whose
            // fields are not on screen. stopPropagation keeps the modal
            // root's Ctrl+Enter handler out of it.
            e.preventDefault();
            e.stopPropagation();
            onEnterStart();
          }}
        />
        {describing && (
          <button
            type="button"
            id="new-describe-cancel"
            className="linklike"
            onClick={cancelDescribe}
          >
            Cancel
          </button>
        )}
        {!describing && onClear && (
          /* What you type stays across closing New (and trips to Intake or
             Settings) until a Create — this is the other way to drop it. */
          <button
            type="button"
            id="new-clear"
            className="linklike"
            title="Empty the box and the form, and start over"
            onClick={onClear}
          >
            Clear
          </button>
        )}
      </div>
      {/* Where Start runs, as ONE line under the box: the folder is a real
          picker (recent folders + Browse…), then whether it gets a branch of
          its own. Nothing on it moves while you type, and the way to the full
          form is the footer's More options alone (one name, one place). */}
      {!runMode && (
      <>
      <StartsInLine
        repoPath={repoPath}
        suggestions={suggestions}
        folderDo={folderDo}
        onBrowse={() => setRunBrowse(true)}
        startsIn={startsIn}
      />
      {!batch && (
        /* Work that starts from somewhere else — a ticket, a PR, an issue —
           lives in Intake; the draft here waits for you. */
        <p className="nf-describe-eg" id="new-intake-door">
          Starting from a ticket, pull request or issue?{" "}
          <button
            type="button"
            className="linklike"
            onClick={() => useUi.getState().openDialogFor("intake")}
          >
            Open Intake
          </button>
        </p>
      )}
      </>
      )}
      {draft.listMode && (
        <RunItems
          rows={draft.rows}
          onRemove={(row) => {
            setDescribe((d) => removeItem(d, row));
            setPlanError("");
          }}
        />
      )}
      {draft.listMode && draft.previewError && (
        <p className="rt-note error" aria-live="polite">
          Couldn't read the list: {draft.previewError}
        </p>
      )}
      {draft.listMode &&
        draft.warnings.map((w) => (
          <p key={w} className="rt-note">
            {w}
          </p>
        ))}
      {/* Options only for a BATCH (Intake's "Start together…"). One
          session's page 1 is the prompt and nothing else: the model
          fills in the form, and Fast-track and Auto-split sit there
          with the rest of the details. */}
      {batch && (
      <RunOptions
        draft={draft}
        n={draft.listMode ? draft.count : 1}
        split={false}
        repoPicker={
          runMode && draft.items.some((i) => i.kind === "task") ? (
            // Typed tasks need a folder; tickets bring their own (the
            // source's repo).
            <span className="rt-repo-pick">
              <select
                id="new-run-repo"
                className="rt-repo"
                value={runBrowse ? BROWSE_VALUE : repoPath}
                title={repoPath + " — each gets its own worktree there"}
                onChange={(e) => {
                  // "Browse…" opens the folder browser right here, so
                  // ANY folder can be the one — not only a suggestion.
                  if (e.target.value === BROWSE_VALUE) {
                    setRunBrowse(true);
                    return;
                  }
                  setRunBrowse(false);
                  folderDo({ t: "user-set", path: e.target.value });
                }}
              >
                {runRepoOptions(repoPath, suggestions, leafName).map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </span>
          ) : null
        }
        // A batch is already one session per line: nothing to split.
        splitBox={null}
      />
      )}
      {planNoteShown && (
        <p className="nf-describe-note" aria-live="polite">
          {planNoteShown}
        </p>
      )}
      {describing && !runMode && (
        <p className="nf-describe-wait" id="new-describe-wait" role="status">
          Asking {agentLabel} to fill in the details… <Elapsed since={describeSince} />{" "}
          <span className="muted">(up to {Math.round(DESCRIBE_CAP_MS / 1000)} s)</span>
        </p>
      )}
      {planError && (
        <p id="new-describe-error" className="error" aria-live="polite">
          {planError}
          {describeFailed && !runMode && (
            <>
              {" "}
              {/* ONE way on: start it anyway, where the line above says. With
                  no folder to start in, the form is where one gets picked. */}
              <button
                type="button"
                id="new-describe-fallback"
                className="nf-fallback"
                onClick={repoPath.trim() ? startInstead : fillFormInstead}
              >
                {repoPath.trim() ? "Start it in " + leafName(repoPath.trim()) + " instead" : "Pick a folder…"}
              </button>
            </>
          )}
        </p>
      )}
      {newFolderRow}
      {agentNote}
    </div>
  );
}

/** Page 1's "Runs in [folder ▾] · on a new branch" — the whole of where Start
 * goes, on one line. The folder is a real picker: the server's recent folders
 * (only ones a session was started in) and the rest of its suggestions, then
 * Browse…, which opens the inline folder browser. */
function StartsInLine({
  repoPath,
  suggestions,
  folderDo,
  onBrowse,
  startsIn,
}: {
  repoPath: string;
  suggestions: RepoSuggestion[];
  folderDo: Dispatch<FolderAction>;
  onBrowse(): void;
  startsIn: ReactNode;
}) {
  const path = repoPath.trim();
  const groups = SUGGEST_SOURCES.map((g) => ({
    label: g.key === "recent" ? "Recent folders" : g.label,
    items: suggestions.filter((s) => s.source === g.key),
  })).filter((g) => g.items.length > 0);
  const known = suggestions.some((s) => s.path === path);
  return (
    <p className="nf-starts-line" id="new-describe-help">
      Runs in{" "}
      <select
        id="new-describe-folder"
        className="nf-folder-chip"
        data-tour="new-folder"
        aria-label="Folder it runs in"
        title={path || "Pick the folder it runs in"}
        value={path}
        onChange={(e) => {
          if (e.target.value === BROWSE_VALUE) {
            onBrowse();
            return;
          }
          folderDo({ t: "user-set", path: e.target.value });
        }}
      >
        {!path && <option value="">Pick a folder…</option>}
        {path && !known && <option value={path}>{"📁 " + leafName(path)}</option>}
        {groups.map((g) => (
          <optgroup key={g.label} label={g.label}>
            {g.items.map((s) => (
              <option key={s.path} value={s.path} title={s.path}>
                {(s.is_git ? "📦 " : "📁 ") + (s.name || leafName(s.path))}
              </option>
            ))}
          </optgroup>
        ))}
        <option value={BROWSE_VALUE}>Browse…</option>
      </select>
      {startsIn}
    </p>
  );
}
