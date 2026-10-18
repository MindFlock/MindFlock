/** The New dialog's create path, lifted out of NewSessionDialog.tsx: the one
 * body builder, the one POST /api/instances (and everything that follows it),
 * the run-mode start and the submit that picks between them. Built per render
 * from the dialog's state, exactly as the closures it replaces were, so a
 * caller that reaches postCreate from a several-renders-old closure (page 1's
 * immediate start, after a model turn) still sees that render's form. */

import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import type { Instance } from "../api/types";
import { refreshInstances } from "../state/queries";
import { deviceApi, deviceTitle } from "./devices";
import { rememberNewSessionChoice, useUi } from "../state/store";
import { toast } from "./toast";
import { addPendingSession, clearStaleAlias, failPendingSession, selectSession } from "./sessionActions";
import { tokenize } from "../components/dialogs/FlagChips";
import type { RunDraft } from "../components/dialogs/NewList";
import { LANE_LABEL, setLaneWhenReady, type Lane } from "./laneActions";
import { clampSplit } from "./runStart";
import {
  baseBranchFields,
  isNameQuery,
  submitHoldReason,
  type PlanFolderAction,
} from "../components/dialogs/newSession";

/** What the create path reads from the dialog at the render it was built in. */
export interface NewSessionSubmitCtx {
  title: string;
  repoPath: string;
  prompt: string;
  program: string;
  planFirst: boolean;
  planOk: boolean;
  launchArgs: string;
  profileId: string;
  profileModel: string;
  strategy: string;
  inPlace: boolean;
  initRepo: boolean;
  provision: boolean;
  baseBranch: string;
  baseIsRemote: boolean;
  continueOn: string;
  activeTemplate: string;
  maxPieces: number;
  splitLimit: number;
  runMode: boolean;
  splitOn: boolean;
  laneNeedsWorktree: boolean;
  newFolderBlocked: string;
  provisionBlocked: string;
  draft: RunDraft;
  laneAtPress: MutableRefObject<{ lane: Lane; askFirst: boolean }>;
  deviceRef: MutableRefObject<string>;
  failedReopen: MutableRefObject<boolean>;
  submitArmAt: MutableRefObject<number>;
  setError: Dispatch<SetStateAction<string>>;
  setPlanError: Dispatch<SetStateAction<string>>;
  setPromptOpen: Dispatch<SetStateAction<boolean>>;
  setPage: Dispatch<SetStateAction<number>>;
  planFolderDo: Dispatch<PlanFolderAction>;
  closeDialog: () => void;
}

export function newSessionSubmit(ctx: NewSessionSubmitCtx) {
  const {
    title,
    repoPath,
    prompt,
    program,
    planFirst,
    planOk,
    launchArgs,
    profileId,
    profileModel,
    strategy,
    inPlace,
    initRepo,
    provision,
    baseBranch,
    baseIsRemote,
    continueOn,
    activeTemplate,
    maxPieces,
    splitLimit,
    runMode,
    splitOn,
    laneNeedsWorktree,
    newFolderBlocked,
    provisionBlocked,
    draft,
    laneAtPress,
    deviceRef,
    failedReopen,
    submitArmAt,
    setError,
    setPlanError,
    setPromptOpen,
    setPage,
    planFolderDo,
    closeDialog,
  } = ctx;

  /** A start in run mode: POST /api/runs, and the dialog closes only once the
   * server has taken it — a refusal ("one-for-all needs a single repository",
   * "repo_path must be a git repository") has to land beside the choices it
   * is about: page 1's note for a batch, the form's error row for a split. */
  const startRun = async () => {
    const fail = splitOn ? setError : setPlanError;
    setPlanError("");
    setError("");
    if (splitOn && !prompt.trim()) {
      setPromptOpen(true);
      fail("Auto-split needs a task — write it in the Task below, or untick Auto-split.");
      return;
    }
    const r = await draft.start({
      split: splitOn,
      maxPieces: clampSplit(maxPieces, splitLimit),
      name: title.trim(),
      initRepo,
    });
    if (!r.ok) {
      if (r.error) fail(r.error);
      return;
    }
    const n = Array.isArray(r.body.items) ? r.body.items.length : 0;
    const name = String(r.body.name || "");
    closeDialog();
    refreshInstances();
    if (splitOn)
      toast(
        `Starting it${name ? " — “" + name + "”" : ""}. Its agent decides whether to split; a plan shows in its Thread tab`,
        { duration: 6000 }
      );
    else if (n >= 2) {
      const c = Math.min(n, draft.concurrency);
      toast(
        `Started “${name}” — ${n} sessions, ${c === n ? "all at once" : c + " at a time"} · fast-track ${draft.lane === "leave" ? "off" : "→ " + LANE_LABEL[draft.lane]}`,
        { duration: 6000 }
      );
    } else toast("Starting it — the session appears in the rail", { duration: 4000 });
  };

  /** Report a create that never happened. The line beside the Create button is
   * the primary surface, but this card scrolls and its actions row is off the
   * bottom of a small window (and a reopened dialog comes back scrolled to the
   * top), so the same words also go to the toast — fixed to the viewport, and
   * impossible to be scrolled away from. */
  const failCreate = (msg: string) => {
    setError(msg);
    toast(msg, { duration: 9000 });
  };

  /** The create body for one session.
   *
   * The six fields a plan decides are PARAMETERS; everything else — agent,
   * launch flags, account, model pin, workspace strategy — is read from the
   * form, where it is either the user's own pick or the default loaded when the
   * dialog opened, and where no plan ever writes. That split is what lets an
   * immediate start build a body from an answer it has just received while the
   * form still holds the previous opening's values. */
  const buildBody = (p: {
    title: string;
    repoPath: string;
    prompt: string;
    inPlace: boolean;
    initRepo: boolean;
    provisioned: boolean;
    /** The Base branch pick; "" = the server's own choice. */
    baseBranch?: string;
    /** The pick is a remote-tracking name ("origin/feature"). */
    baseBranchRemote?: boolean;
    /** Work ON that remote branch itself instead of a new one. */
    continueBranch?: boolean;
  }): Record<string, unknown> => {
    const body: Record<string, unknown> = {
      title: p.title.trim(),
      program: program.trim(),
      repo_path: p.repoPath.trim(),
    };
    const promptVal = p.prompt.trim();
    if (promptVal) body.prompt = promptVal;
    // The server appends the plan request to the prompt (and never twice).
    if (planFirst && planOk) body.plan_first = true;
    // Sent EXPLICITLY (even empty) so a toggled-off default is honored.
    body.launch_args = tokenize(launchArgs);
    // Absent = inherit the app-wide default account (same tri-state as
    // launch_args), so only an explicit pick rides along.
    if (profileId) body.profile_id = profileId;
    if (profileId && profileModel.trim()) body.profile_model = profileModel.trim();
    if (p.provisioned) {
      body.provisioned = true;
      body.workspace_strategy = strategy;
      if (body.repo_path) body.init_repo = p.initRepo;
    } else {
      body.init_repo = p.initRepo;
      body.in_place = p.inPlace;
      // A chosen base for a NEW WORKTREE only — the server refuses the pair
      // for in-place and provisioned sessions, and base_branch needs base_ref.
      // base_ref is the thing the worktree is cut from (a remote-tracking
      // name included); base_branch is the branch its diff and PR are read
      // against, which never carries a remote prefix — "origin/feature" cuts
      // from the remote and compares against "feature". "Work on feature
      // itself" sends continue_branch instead (baseBranchFields).
      const base = (p.baseBranch || "").trim();
      if (base && !p.inPlace)
        Object.assign(body, baseBranchFields(base, !!p.baseBranchRemote, !!p.continueBranch));
    }
    return body;
  };

  /** POST the create, and own everything that follows it.
   *
   * BOTH ways of starting a session end here — the form's Create button on page
   * 2, and page 1's "Start session", which never touches the form at all — so
   * the optimistic close, the pending row, the alias fix and the failure reopen
   * happen once and identically. It takes a finished body rather than reading
   * state because that is the only way the second caller can work: applyPlan's
   * setState calls have not been flushed when an immediate start needs the
   * values, so it passes the answer's own fields and this function cannot tell
   * the two apart. */
  const postCreate = async (body: Record<string, unknown>) => {
    // The fast-track picked in the dialog, as it stood at the press.
    // Read through a ref: an immediate start reaches here after a model turn,
    // from a closure that is several renders old.
    const { lane, askFirst } = laneAtPress.current;
    setError("Creating…");
    // Close NOW with an optimistic "provisioning" row — the POST can take
    // seconds; on failure the dialog re-opens with fields and error intact.
    // Where it was aimed at the press — the picker can move once the dialog
    // has closed and reopened while this POST is still in flight.
    const target = deviceRef.current;
    const guess = addPendingSession((body.title as string) || "untitled", target);
    closeDialog();
    try {
      // On another device the create is answered there, and the session joins
      // this rail under its namespaced title (the server refreshes that
      // device's list before answering, so the row is already in the poll).
      const made = await deviceApi(target)<Instance>("/api/instances", { json: body });
      const inst = { ...made, title: deviceTitle(target, made.title) };
      // The create came back 200, so the plan's folder is on disk now — and the
      // question about making it goes with it. A reopen deliberately KEEPS the
      // plan (see planFolderReducer), so without this the next opening would
      // offer to create a folder that is already there.
      planFolderDo({ t: "created" });
      // What Name → Enter reuses next time in this folder.
      if (!target)
        rememberNewSessionChoice(String(body.repo_path || ""), {
          template: activeTemplate,
          program: String(body.program || ""),
          profile_id: String(body.profile_id || ""),
        });
      // Same reason as the guess in addPendingSession: the server's real title
      // must not arrive wearing a closed session's rename.
      clearStaleAlias(inst.title);
      await refreshInstances();
      // Plan first lands on the Map's Plan section, to watch the plan arrive
      // and press Go there; anything else on the Agent tab.
      if (body.plan_first) useUi.getState().openMap(inst.title, { section: "plan" });
      else selectSession(inst.title);
      // Arming waits for the worktree; it never holds the dialog.
      if (lane !== "leave") void setLaneWhenReady(inst.title, lane, askFirst);
    } catch (err) {
      failPendingSession(guess);
      failCreate((err as Error).message);
      const ui = useUi.getState();
      // Armed only when there is a reopen to arm it for. Someone who reopened
      // the dialog by hand while the POST was in flight is already looking at
      // it, and a flag left set here would go on to suppress the reset of an
      // open that had nothing to do with this failure.
      if (ui.openDialog !== "new-session") {
        failedReopen.current = true;
        // The FORM, not the sentence — including for a create fired from page
        // 1, which is the case that needs it most: a refused create is a thing
        // to fix, every field to fix it with is here, and applyPlan has already
        // put the plan's values in them. Set before the reopen, because the
        // reset effect early-returns on failedReopen and will not touch it.
        setPage(2);
        ui.openDialogFor("new-session");
      }
    }
  };

  /** Create from the form. `over.prompt` is page 1's Create session, which
   * starts from the sentence as typed (it is also written into the Task box,
   * so a refusal lands on a form that still holds it). Resolves true once a
   * create or run has been sent, false when a guard refused it here. */
  const submit = async (over: { prompt?: string } = {}): Promise<boolean> => {
    if (runMode) {
      if (splitOn && isNameQuery(repoPath)) {
        setError(
          `“${repoPath.trim()}” is a name to look up, not a folder — pick one of the matches, or type a full path.`
        );
        return false;
      }
      void startRun();
      return true;
    }
    const held = submitHoldReason(submitArmAt.current, Date.now());
    if (held) {
      // First, and — like the guards below — before the optimistic close:
      // once the dialog has shut there is nothing on screen to correct. setError
      // rather than failCreate because a half-second hold is not a create
      // failure and does not deserve a nine-second toast for it.
      setError(held);
      return false;
    }
    if (newFolderBlocked) {
      // failCreate, not setError, for the reason its two siblings below give:
      // this card scrolls, its actions row is off the bottom of a small window,
      // and the refusal has to reach somebody whose confirm row is three screens
      // up. And before the optimistic close, like every guard here — once the
      // dialog has shut the directory is made and there is nothing to correct.
      failCreate(newFolderBlocked);
      return false;
    }
    if (provisionBlocked) {
      // Before the optimistic close below, for the same reason the name guard
      // is: once the dialog has shut there is nothing on screen to correct.
      failCreate(provisionBlocked);
      return false;
    }
    if (isNameQuery(repoPath)) {
      // Every way of reaching Create lands here — the button, Ctrl/Cmd+Enter,
      // and Enter in any other field — so this is the one place that can stop a
      // search term being sent as a folder. It has to stop it BEFORE the
      // optimistic close below: the dialog shuts and the POST goes out in the
      // same breath, so by the time the server has made its stray directory
      // there is nothing left on screen to cancel. See isNameQuery for what the
      // server does with a name.
      failCreate(
        `“${repoPath.trim()}” is a name to look up, not a folder — pick one of the matches, or type a full path starting with / or ~ (Browse… fills one in).`
      );
      return false;
    }
    await postCreate(
      buildBody({
        title,
        repoPath,
        prompt: over.prompt ?? prompt,
        inPlace: inPlace && !laneNeedsWorktree,
        initRepo,
        provisioned: provision,
        baseBranch,
        baseBranchRemote: baseIsRemote,
        continueBranch: !!baseBranch && continueOn === baseBranch,
      })
    );
    return true;
  };

  return { failCreate, buildBody, postCreate, startRun, submit };
}
