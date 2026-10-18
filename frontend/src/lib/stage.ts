/** Workflow stage + live-activity chips and the guided next-step action
 * (port of app.js section 4). Shared by sidebar rows and pane headers. */

import type { Instance } from "../api/types";
import { queryClient } from "../state/queries";
import type { Config } from "../api/types";
import { copyText } from "./clipboard";
import { confirmPop } from "./errorPop";
import { toast } from "./toast";
import {
  PR_FALLBACK_HINT,
  commitSession,
  offerUrl,
  hasPrSupport,
  makePrSession,
  mergeSession,
  pushSession,
  resetStage,
} from "./sessionActions";
import {
  LANE_DESC,
  LANE_LABEL,
  LANE_SHORT,
  askFirstApplies,
  laneChoice,
  type Lane,
} from "./laneActions";
import { autopilotChipTitle, mergeBlockerLabel } from "./autopilot";
import { useUi } from "../state/store";
import { deriveSessionState, SLEEPING_LABEL, type SessionState } from "./sessionState";
import type { TestPlan, TestPlansResponse } from "../api/types";
import type { SessionMenuModel } from "./sessionMenu";
import { planForSession, planVerdict } from "../components/dialogs/verify";

/** The ship stage's chip words. Sentence case, like the session state's
 * ("Working", "Question") they sit in the same chip slot as, and a verdict
 * mark goes AFTER the word ("Pre-commit ✗", "Checks ✓") everywhere. */
const STAGE_META: Record<string, { label: string }> = {
  provisioning: { label: "Provisioning" },
  agent: { label: "Agent" },
  precommit: { label: "Pre-commit" },
  interrupt: { label: "Pre-commit ✗" },
  committed: { label: "Committed" },
  pushed: { label: "Pushed" },
  pr: { label: "PR open" },
  // F4: the server never emits "merged" (a merged PR moves the stage off
  // "pr"); kept as a harmless defensive default.
  merged: { label: "Complete ✓" },
};

export function stageMeta(stage: string) {
  return STAGE_META[stage] || STAGE_META.agent;
}

// --- "Back to idle": restarting the guided cycle on a finished branch --------
// The workflow stage is git-derived, so it returns to "agent" on its own the
// moment the tree goes dirty. The gap is the CLEAN-tree window — committed,
// pushed, pr — where git says the branch is done and the header therefore
// insists on Push / Make PR / Merge, with no way to say "I opened the PR and I'm
// still working on this branch". The reset pin says exactly that.
//
// THE AUTHORITY IS THE SERVER: `inst.stage_reset`
// (backend/web/core/stage_reset.py). It has to be, for the two reasons a
// browser-local pin could not cover — it must survive a reload, and the sidebar,
// the pane header and /m must not disagree about it. The server releases it as
// soon as the worktree moves (a new commit, or a dirty tree).
//
// The map below is only an OPTIMISTIC ECHO for the gap between pressing the
// button and the row that confirms it (one round trip, or up to a poll if that
// request is lost). It expires on its own, so a failed POST degrades to "the
// button did nothing" rather than to a pin no server agrees with.
const echo = new Map<string, number>();
const ECHO_TTL_MS = 20_000;

function echoing(title?: string): boolean {
  if (!title) return false;
  const at = echo.get(title);
  if (at === undefined) return false;
  if (Date.now() - at > ECHO_TTL_MS) {
    echo.delete(title);
    return false;
  }
  return true;
}

export function markLoopReset(title: string) {
  if (title) echo.set(title, Date.now());
}

export function clearLoopReset(title: string) {
  echo.delete(title);
}

/** Per-poll reconcile: drop the local echo once the server's row carries the pin
 * itself (or once the echo has aged out), so exactly one of the two is ever in
 * play and the server's release is never overridden by a stale echo. */
export function reconcileLoopReset(inst: Instance) {
  if (!inst.title) return;
  if (inst.stage_reset) echo.delete(inst.title);
  else echoing(inst.title); // TTL sweep
}

/** True when this session's guided ladder is pinned back to its start. */
export function isStageReset(inst: Partial<Instance>): boolean {
  return !!inst.stage_reset || echoing(inst.title);
}

/** The guided stage to render: "agent" while the reset pin holds, otherwise the
 * real git-derived stage. */
function guidedStage(inst: Partial<Instance>): string {
  if (isStageReset(inst)) return "agent";
  return inst.stage || "agent";
}

/** Stages a reset can act on: git considers the branch finished, so the ladder
 * has somewhere to be put back FROM. */
const RESETTABLE = new Set(["committed", "pushed", "pr"]);

/** What the header is currently asking for at each finished stage — named in the
 * tooltip, because "back to idle" only means something against the ask it
 * silences. */
const RESET_ASK: Record<string, string> = {
  committed: "push",
  pushed: "open a PR",
  pr: "merge",
};

/** The ↺ control beside the guided button: put this window back to idle.
 *
 * A ONE-SHOT ACTION, deliberately not a toggle like ⏩ beside it. "Back to idle"
 * is something you do to a window, not a mode the window is in — once the header
 * has stopped asking you to finish a cycle you consider finished, there is
 * nothing left to turn off, and a control that lingered in an ON state would
 * assert otherwise. So it does its job and leaves; the pin it set expires on its
 * own the moment you commit again.
 *
 * Shown only where the ladder has somewhere to be put back FROM — a clean branch
 * git considers finished, not already pinned. Returns null everywhere else. */
export function resetStep(inst: Partial<Instance>): NextStep | null {
  const title = inst.title;
  const caps = queryClient.getQueryData<Config>(["config"])?.caps;
  if (caps && !caps.git) return null;
  if (!title || inst.status === "loading" || inst.status === "paused") return null;
  if (inst.workspace_missing) return null;
  if (isStageReset(inst)) return null; // done — the ladder is back at its start
  const stage = inst.stage || "agent";
  if (!RESETTABLE.has(stage)) return null;
  return {
    label: "↺",
    title:
      "Back to idle — restart the guided cycle on this branch.\n" +
      "Nothing is undone: the commits stay, an open PR stays open. The header\n" +
      "just stops asking you to " +
      (RESET_ASK[stage] || "finish up") +
      " so you can keep working here.\n" +
      "It clears itself the moment you commit again.",
    run: () => resetStage(title),
  };
}

// --- Live agent activity ------------------------------------------------------
// The 2-poll debounce moved to lib/sessionState.ts (it is the input every
// session state reads); re-exported here so the ship ladder's callers keep
// their import.
export { noteActivity, forceActivity, dropActivity, effectiveActivity } from "./sessionState";

// --- The one persistent chip -------------------------------------------------

export interface ChipState {
  label: string;
  cls: string;
  title: string;
}

/** The CSS class a session state paints its chip with. The class names are
 * the old activity words (Pane.tsx keys its reduce-motion cover on
 * `s-running`); the colour comes from the tone class beside it. */
const STATE_CLS: Record<SessionState["state"], string> = {
  starting: "s-provisioning",
  working: "s-running",
  needs_you: "s-clarify",
  idle: "s-idle",
  limit: "s-limit",
  offline: "s-offline",
};

/** The chip for a derived session state alone — no git stage. */
export function stateChip(ss: SessionState): ChipState {
  let cls = STATE_CLS[ss.state];
  if (ss.label === "Paused") cls = "s-paused";
  else if (ss.label === SLEEPING_LABEL) cls = "s-sleeping";
  else if (ss.label === "Missing") cls = "s-missing";
  return { label: ss.label, cls: cls + " tone-" + ss.tone, title: ss.hint || ss.label };
}

/** The pill for a live agent state, on its own — no session, no stage.
 *
 * Split out of `chipState` so a window that has an AGENT but is not a session
 * (the Assistant) is painted from the same words with the same class and the
 * same tooltip as a session's row: both read `deriveSessionState`. */
export function activityChip(act: string): ChipState {
  return stateChip(deriveSessionState({ title: "", activity: act }, { activity: act }));
}

/** The one persistent chip: the session state (lib/sessionState) while the
 * agent is starting, working, waiting on you, limited or offline; the ship
 * stage (committed / pushed / PR open / pre-commit) while it sits idle on a
 * finished branch. Pass the row's already-derived `ss` so the dot beside the
 * chip is painted from the SAME object. */
export function chipState(inst: Partial<Instance>, ss: SessionState = deriveSessionState(inst)): ChipState {
  if (ss.state !== "idle" || ss.label !== "Idle") return stateChip(ss);
  const stage = guidedStage(inst);
  if (stage === "agent" || stage === "interrupt") return stateChip(ss);
  const m = stageMeta(stage);
  return { label: m.label, cls: "s-" + stage, title: ss.reason === "stuck" && ss.hint ? ss.hint : m.label };
}

/** O3 verification-gate chip descriptor (second, smaller chip) or null. */
export function checkChip(
  inst: Partial<Instance>
): { label: string; cls: string; title: string } | null {
  const c = inst.check;
  if (!c || !c.state) return null;
  const cmd = String((c as { command?: string }).command || "");
  if (c.state === "running")
    return { label: "Checks…", cls: "s-provisioning", title: "Checks running: " + cmd };
  if (c.state === "failed") {
    const rc = (c as { rc?: number | null }).rc;
    return {
      label: "Checks ✗",
      cls: "s-interrupt",
      title:
        "Checks failed (exit " + (rc == null ? "?" : rc) + ") — push is gated until they pass (or push anyway)",
    };
  }
  if (c.state === "ok") {
    return (c as { stale?: boolean }).stale
      ? {
          label: "Checks stale",
          cls: "s-idle",
          title: "Checks passed for an older commit — a fresh run starts after the next commit",
        }
      : { label: "Checks ✓", cls: "s-committed", title: "Checks passed for this commit" };
  }
  return null;
}

// --- Guided next step ---------------------------------------------------------

// A *runnable* example, not a `<url>` placeholder: the copied line only needs
// owner/repo swapped. SSH is spelled out first because MindFlock never touches
// your remote — it pushes with plain `git push` over whatever you configure —
// and an SSH-only contributor should see her own setup treated as normal.
export const NO_ORIGIN_CMD = "git remote add origin git@github.com:owner/repo.git";
/** HTTPS is exactly as good; shown in the tooltip so neither reads as "the
 * supported one". */
export const NO_ORIGIN_ALT = "git remote add origin https://github.com/owner/repo.git";

export interface NextStep {
  label: string;
  run: () => void;
  hint?: boolean;
  title?: string;
  /** Rendered unclickable. Used when we KNOW the action would be refused — a merge
   * whose PR has conflicts, a failing required check, or a missing review. Offering
   * a button that cannot work is worse than showing why it cannot. */
  disabled?: boolean;
  /** Rendered as an ON toggle (filled rather than outlined). Used by the ⏩
   * fast-track control while a chain is armed, so the button you pressed stays
   * visible, visibly on, and clickable to turn back off. */
  active?: boolean;
}

/* --- Live step indicator ----------------------------------------------------
 * What the top of a window says while work is actually happening.
 *
 * The header used to fill the primary button's slot with a DISABLED grey pill
 * whenever `nextStep()` returned null — so the single busiest moment in the
 * workflow (pre-commit hooks running) rendered as dead greyed-out text reading
 * "pre-commit". This replaces it with a small indicator that reads as active,
 * names the step, and — when a chain is armed — says what it is waiting on
 * (where it is heading is the ⏩ button's to say).
 */

export type StepTone = "work" | "blocked" | "ok" | "quiet";

export interface LiveStep {
  label: string;
  tone: StepTone;
  title: string;
  /** Click opens the PR (only set when there is one to open). */
  href?: string;
}

/** Verbs that are in flight but have no stage of their own.
 *
 * Push, make-PR and merge all type a command / call a route and return; the stage
 * only moves once the RESULT is observable (origin matches, a PR exists). Between
 * the click and that flip the header would otherwise still show the previous
 * step, which is what made people press Push twice. Same self-clearing pattern as
 * `reconcileLoopReset`: the entry dies when the stage moves past it or on TTL. */
const inflight = new Map<string, { verb: "push" | "pr" | "merge"; at: number }>();
const INFLIGHT_TTL_MS = 120_000;

export function markStep(title: string, verb: "push" | "pr" | "merge") {
  if (title) inflight.set(title, { verb, at: Date.now() });
}

export function clearStep(title: string) {
  inflight.delete(title);
}

function inflightVerb(inst: Partial<Instance>): "push" | "pr" | "merge" | null {
  const title = inst.title;
  if (!title) return null;
  const rec = inflight.get(title);
  if (!rec) return null;
  if (Date.now() - rec.at > INFLIGHT_TTL_MS) {
    inflight.delete(title);
    return null;
  }
  // The stage has caught up — the verb is done.
  const stage = inst.stage || "";
  if (
    (rec.verb === "push" && (stage === "pushed" || stage === "pr")) ||
    (rec.verb === "pr" && stage === "pr") ||
    (rec.verb === "merge" && stage !== "pr")
  ) {
    inflight.delete(title);
    return null;
  }
  return rec.verb;
}

/** The current step for a session, or null when nothing is happening. */
export function liveStep(inst: Partial<Instance>): LiveStep | null {
  const stage = guidedStage(inst);

  if (inst.workspace_missing)
    return { label: "workspace gone", tone: "blocked", title: "The workspace directory no longer exists." };
  if (inst.status === "paused")
    return { label: "paused", tone: "quiet", title: "Session paused — resume it to continue." };

  // Worktree setup outranks everything: queued prompts are HELD while it runs,
  // and the autopilot refuses to act at all, so it must be visible.
  const setup = inst.setup;
  if (setup?.state === "running")
    return { label: "setting up", tone: "work", title: "Running the workspace setup commands." };
  if (setup?.state === "failed")
    return {
      label: "setup failed",
      tone: "blocked",
      title:
        "Workspace setup failed" +
        (setup.failed_step ? " at " + setup.failed_step : "") +
        ". Queued prompts are held until it succeeds.",
    };

  if (stage === "provisioning")
    return { label: "provisioning", tone: "work", title: "Creating the workspace." };
  if (stage === "precommit")
    return { label: "pre-commit", tone: "work", title: "Running the pre-commit hooks." };
  if (stage === "interrupt")
    return {
      label: "pre-commit ✗",
      tone: "blocked",
      title:
        "A pre-commit hook blocked the commit" +
        (inst.failed_step ? " at " + inst.failed_step : "") +
        ".",
    };

  const verb = inflightVerb(inst);
  if (verb === "push") return { label: "pushing", tone: "work", title: "Pushing the branch to origin." };
  if (verb === "pr") return { label: "opening PR", tone: "work", title: "Opening the pull request." };
  if (verb === "merge") return { label: "merging", tone: "work", title: "Merging the pull request." };

  // Verification checks run while the stage still reads "committed", and the push
  // route soft-rejects a push until they pass — so say so before Push is pressed.
  const check = inst.check;
  if (check?.state === "running")
    return { label: "checks", tone: "work", title: "Running the verification check." };
  if (check?.state === "failed")
    return {
      label: "checks ✗",
      tone: "blocked",
      title: "The verification check failed" + (check.failed_step ? " at " + check.failed_step : "") + ".",
    };

  // An armed chain: say what it is waiting on, in the server's words. Where
  // it is going is the ⏩ button's job (it names the target, filled while a
  // run works), so the step never repeats it; with no note there is nothing
  // the ⏩ doesn't already say and the step falls through. A halt falls
  // through too: the ⏩ turns red with ✗ and carries the reason.
  const run = inst.autopilot;
  if (run && run.depth && run.state === "running" && run.note)
    return { label: run.note, tone: "work", title: autopilotChipTitle(run) };

  if (stage === "pr") {
    const ms = inst.merge_state;
    if (ms && !ms.can_merge)
      // Say WHAT is blocking it, right in the header — "PR open" is true but
      // useless when the thing you want to know is why Merge is greyed out.
      return {
        label: mergeBlockerLabel(ms),
        tone: "blocked",
        title:
          "This PR cannot be merged yet:\n" +
          (ms.blockers || []).map((b) => "• " + b).join("\n"),
        href: inst.pr_url || ms.url || undefined,
      };
    return {
      label: "PR open",
      tone: "ok",
      title: inst.pr_url ? "Open the pull request" : "A pull request is open for this branch.",
      href: inst.pr_url || undefined,
    };
  }
  return null;
}

/* --- Follow an autopilot run ------------------------------------------------
 * When the driver reaches a step, do what pressing that button by hand would do:
 * the commit step focuses the window and shows its shell (so the pre-commit hooks
 * are watchable), and the PR step opens the pull request.
 *
 * Driven from BOTH the `session.autopilot_changed` event (sub-second) and the 4s
 * poll (guaranteed). Relying on the event alone made this depend on catching one
 * transient message: the socket can be disconnected, the server's per-client queue
 * drops events when full, and a page can load mid-commit. Both paths share the
 * guard below, so it still happens exactly once per step.
 */

const followed = new Map<string, string>();

export function resetFollow(title?: string) {
  if (title) followed.delete(title);
  else followed.clear();
}

/** React to an autopilot run reaching a new step. `live` marks a real-time event,
 * where firing on a first sighting is correct; a poll seeds silently instead, so
 * loading the page during a commit does not yank focus. */
export function followAutopilot(
  inst: Partial<Instance>,
  opts?: { live?: boolean }
): string | null {
  const title = inst.title;
  const run = inst.autopilot;
  if (!title) return null;
  if (!run || !run.depth || run.state !== "running") {
    followed.delete(title);
    return null;
  }
  const step = String(run.step || "");
  const seen = followed.get(title);
  if (seen === step) return null;
  const first = seen === undefined;
  followed.set(title, step);

  if (step === "commit") {
    // SWITCH THE TAB, DO NOT TAKE FOCUS. Autopilot runs unattended and often on a
    // window you are not looking at, so yanking the view away from whatever you
    // ARE doing is wrong. Setting the tab is harmless and does the useful half:
    // whenever you next look at that window, it is already showing the hooks.
    // (The manual Commit dialog still focuses, because you asked for it there.)
    //
    // Fires even on a first sighting, including from a poll: with no focus theft
    // there is nothing to protect against, and a page loaded mid-commit should
    // still find the right tab selected.
    try {
      useUi.getState().setLastTab(title, "shell");
    } catch {
      /* following is a courtesy — never let it break the poll */
    }
    return "commit";
  }
  if (step === "pr") {
    // Opening a browser tab IS intrusive, so this one stays limited to a real live
    // transition: a page load must not re-open a PR you have already seen.
    if (first && !opts?.live) return null;
    const url = String(run.url || "") || inst.pr_url || "";
    if (url) {
      try {
        offerUrl(url, "PR opened for " + title);
      } catch {
        /* a blocked/absent window must not break the loop */
      }
    }
    return "pr";
  }
  return null;
}

/** The ⏩ button: THE per-session fast-track control.
 *
 * It SAYS where fast-track will carry this session ("⏩ off", "⏩ Commit",
 * "⏩ PR", "⏩ Merge"; `askFirst` adds the "ask me first" mark) and a click
 * opens the picker (grid/FastTrackMenu) — Off / Commit / Push / Open a PR /
 * Merge when green, and "Ask me before it ships". It is a control, never a
 * status chip (a `.stagechip` in a pane head is display:none by design).
 *
 * What it keeps from the old one-press toggle: it is visibly ON while a run is
 * working (`active`), says ✗ and why on a halt (`halted`), and an ARMED target
 * is never hidden — an intake-armed session spends its first minutes
 * provisioning, which is precisely when you might change your mind.
 *
 * Returns null only when there is nothing it could set: no git, a session
 * that isn't there yet, a missing workspace, or a workspace still being made
 * for a session with no target. */
export interface FastTrackButton {
  lane: Lane;
  askFirst: boolean;
  /** "⏩ PR" — plus " ✗" on a halt. */
  label: string;
  /** A run is working toward the target right now. */
  active: boolean;
  /** The run stopped short and said why (in `title`). */
  halted: boolean;
  title: string;
}

export function fastTrackStep(inst: Partial<Instance>): FastTrackButton | null {
  const title = inst.title;
  const caps = queryClient.getQueryData<Config>(["config"])?.caps;
  if (caps && !caps.git) return null;
  if (!title || inst.workspace_missing) return null;
  // A shared-folder (peer) session runs sandboxed: the server refuses
  // autopilot and ship for it, so there is no ⏩ to offer.
  if (inst.peer_share) return null;

  const { lane, askFirst } = laneChoice(inst);
  const run = inst.autopilot;
  const running = !!(run && run.depth && run.state === "running");
  const halted = !!(run && run.depth && run.state === "halted");
  // Nothing it could set yet — the session or its workspace is still being
  // made — unless a run is armed: that one must stay visible and changeable.
  if (!running && !halted) {
    if (inst.status === "loading") return null;
    if (lane === "leave" && guidedStage(inst) === "provisioning") return null;
  }

  const ask = askFirstApplies(lane) && askFirst;
  const where = LANE_LABEL[lane] + (ask ? ", asks first" : "");
  const change = "\n\nClick to change it or turn it off (Ctrl+K F).";
  let tip: string;
  if (running && run) {
    const doing = run.note
      ? " — " + run.note
      : run.step
        ? " (last step: " + run.step + ")"
        : " (waits for the agent to finish)";
    const skipped = run.skipped?.length ? "\nSkipped hooks: " + run.skipped.join(", ") : "";
    tip = "Fast-track → " + where + doing + skipped + change;
  } else if (halted && run) {
    tip =
      "Fast-track stopped: " +
      (run.reason || "unknown reason") +
      "\n\nClick to pick it again or turn it off (Ctrl+K F).";
  } else if (lane === "leave") {
    tip =
      "Fast-track is off. Click to choose how far MindFlock carries this session once its " +
      "agent is done — commit, push, open a PR, or merge when green (Ctrl+K F).";
  } else {
    tip = "Fast-track → " + where + ": " + LANE_DESC[lane] + "." + change;
  }
  return {
    lane,
    askFirst: ask,
    label: "⏩ " + LANE_SHORT[lane] + (halted ? " ✗" : ""),
    active: running,
    halted,
    title: tip,
  };
}

export function nextStep(inst: Partial<Instance>): NextStep | null {
  const title = inst.title;
  const caps = queryClient.getQueryData<Config>(["config"])?.caps;
  if (caps && !caps.git) return null; // no git -> no commit/push/PR workflow
  if (!title || inst.status === "loading" || inst.status === "paused") return null;
  if (inst.workspace_missing) return null; // L7: Clean up lives in the row
  if (inst.peer_share) return null; // sandboxed shared folder: commit/push/PR are refused
  // An armed chain deliberately does NOT take over this slot. It used to, which
  // meant arming fast-track replaced your manual Commit/Push/Make PR button with
  // a status readout and left no way to act by hand. Autopilot state lives on the
  // ⏩ toggle beside this button (see fastTrackStep); the manual ladder below
  // always keeps working, so you can still drive a step yourself at any time.
  switch (guidedStage(inst)) {
    case "agent":
      return {
        label: "Commit…",
        title: "Commit this session's changes — you check the message first. Next: Push, then Open PR",
        run: () => commitSession(title),
      };
    case "interrupt":
      return { label: "Re-commit", run: () => commitSession(title) };
    case "committed":
      // L8: no origin remote -> swap Push for a non-destructive hint that
      // copies the fix command.
      if (inst.has_origin === false) {
        return {
          label: "No remote — add origin…",
          hint: true,
          title:
            "This repo has no origin remote, so there is nowhere to push.\n" +
            "Run this in the workspace (click to copy):\n" +
            NO_ORIGIN_CMD +
            "\nHTTPS works just as well:\n" +
            NO_ORIGIN_ALT,
          run: () =>
            copyText(NO_ORIGIN_CMD).then((ok) => {
              toast(ok ? "command copied" : "copy failed — " + NO_ORIGIN_CMD);
            }),
        };
      }
      return { label: "Push", run: () => pushSession(title) };
    case "pushed":
      // The branch is already on the remote — plain git got it there. Only
      // *filing* the PR may need credentials we don't have, and that degrades
      // to GitHub's compare page, so the step stays clickable either way; it
      // just renders as a hint that says where the click will land.
      return hasPrSupport(caps)
        ? { label: "Open PR", run: () => makePrSession(title) }
        : {
            label: "Open PR ↗",
            hint: true,
            title: PR_FALLBACK_HINT,
            run: () => makePrSession(title),
          };
    case "pr":
      if (!hasPrSupport(caps))
        // Merging is the one thing a browser does better than we can here.
        return inst.pr_url
          ? {
              label: "Merge on GitHub ↗",
              hint: true,
              title: PR_FALLBACK_HINT,
              run: () => window.open(inst.pr_url!, "_blank"),
            }
          : { label: "Merge ↗", hint: true, title: PR_FALLBACK_HINT, run: () => mergeSession(title) };
      // Only offer Merge when GitHub says it would actually go through. `merge_state`
      // absent means we could not find out (no token, no gh, a network fault) — in
      // that case leave the button alone rather than block on a guess.
      {
        const ms = inst.merge_state;
        if (ms && !ms.can_merge)
          return {
            label: "Merge blocked",
            disabled: true,
            title:
              "This PR cannot be merged yet:\n" +
              (ms.blockers || []).map((b) => "• " + b).join("\n") +
              (inst.pr_url ? "\n\nOpen the PR to resolve it." : ""),
            run: () => {},
          };
      }
      return { label: "Merge", run: () => mergeSession(title) };
    case "merged":
      return inst.pr_url
        ? { label: "View PR ↗", run: () => window.open(inst.pr_url!, "_blank") }
        : null;
    default:
      return null; // precommit (running), provisioning
  }
}


// --- Verify's verdict where you merge (round 5, M6) --------------------------
// The SHIP menu offered Open PR with no word about the branch's checklist, so a
// FAILED branch was one click from a PR with nothing in the way. The verdict
// rides on the Open PR item as its note, a failed checklist asks first, and the
// menu's Verify item says the feature's name.

/** "Checklist ✗ Failed" — the verdict as a menu note, or "" with no checklist. */
export function verdictNote(plan: TestPlan | null | undefined): string {
  if (!plan) return "";
  const v = planVerdict(plan);
  return "Checklist " + v.glyph + " " + v.short;
}

/** The question Open PR asks first on a failed checklist, or "". */
export function prVerdictConfirm(plan: TestPlan | null | undefined): string {
  if (!plan || planVerdict(plan).key !== "failed") return "";
  return "Checklist failed — open the PR anyway?";
}

/** The checklist covering a session, from the list the top bar keeps warm —
 * never a request of its own (the menu is built on open, outside render). */
export function cachedPlanFor(inst: Partial<Instance>): TestPlan | null {
  const data = queryClient.getQueryData<TestPlansResponse>(["test-plans"]);
  if (!inst.title) return null;
  return planForSession(data?.plans || [], {
    title: inst.title,
    branch: inst.branch,
    repo: inst.repo,
  });
}

/** The session menu with the verdict beside Open PR (and Merge PR), a confirm
 * on a failed checklist, and "Verify checklist…" as the Verify item's name. */
export function withVerifyVerdict(
  menu: SessionMenuModel,
  inst: Partial<Instance>,
  plan: TestPlan | null | undefined = cachedPlanFor(inst),
): SessionMenuModel {
  const note = verdictNote(plan);
  const confirm = prVerdictConfirm(plan);
  return {
    ...menu,
    groups: menu.groups.map((g) =>
      g.id !== "ship"
        ? g
        : {
            ...g,
            items: g.items.map((item) => {
              if (item.key === "checklist") return { ...item, label: "Verify checklist…" };
              if ((item.key === "pr" || item.key === "merge") && !item.disabled && note)
                return {
                  ...item,
                  note: item.note ? item.note + " · " + note : note,
                  // A question card, not the menu's own `confirm` strip: that
                  // strip is Delete's, and the menu arms one at a time.
                  run: confirm
                    ? () =>
                        confirmPop(
                          confirm,
                          "A step on this branch's checklist failed — see which, or go ahead.",
                          [
                            {
                              label: "Show the checklist",
                              run: () =>
                                useUi.getState().openDialogFor("verify", plan?.branch || inst.branch || ""),
                            },
                            {
                              label: item.label.replace(/…$/, "") + " anyway",
                              primary: true,
                              run: () => void item.run(),
                            },
                          ],
                        )
                    : item.run,
                };
              return item;
            }),
          },
    ),
  };
}
