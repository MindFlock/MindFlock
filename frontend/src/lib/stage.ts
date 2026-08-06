/** Workflow stage + live-activity chips and the guided next-step action
 * (port of app.js section 4). Shared by sidebar rows and pane headers. */

import type { Instance } from "../api/types";
import { queryClient } from "../state/queries";
import type { Config } from "../api/types";
import { copyText } from "./clipboard";
import { toast } from "./toast";
import {
  PR_FALLBACK_HINT,
  commitSession,
  offerUrl,
  hasPrSupport,
  makePrSession,
  mergeSession,
  pushSession,
  resolveDepth,
  selectSession,
  startFastTrack,
  stopFastTrack,
} from "./sessionActions";
import {
  DEPTH_SHORT,
  autopilotChipTitle,
  depthLabel,
  mergeBlockerLabel,
} from "./autopilot";
import { useUi } from "../state/store";

/** Longest `failed_step` that still reads as a hook NAME rather than a line of
 * output, and so still belongs in the stage pill. "Run Tests (+3)" is 14. */
const STEP_BADGE_MAX = 24;

const STAGE_META: Record<string, { label: string }> = {
  provisioning: { label: "provisioning" },
  agent: { label: "agent" },
  precommit: { label: "pre-commit" },
  interrupt: { label: "pre-commit ✗" },
  committed: { label: "committed" },
  pushed: { label: "pushed" },
  pr: { label: "PR open" },
  // F4: the server never emits "merged" (a merged PR moves the stage off
  // "pr"); kept as a harmless defensive default.
  merged: { label: "complete ✓" },
};

export function stageMeta(stage: string) {
  return STAGE_META[stage] || STAGE_META.agent;
}

// --- "Loop reset" pin: after a successful Make PR, restart the guided cycle ---
// The workflow stage is git-derived and would otherwise sit on "pr" (open PR)
// until the branch is merged, leaving the pill stuck on "Merge". After Make PR
// succeeds we pin the session's guided stage back to the start ("agent" -> chip
// "idle", button "Commit…") so the commit→push→PR loop can repeat in the same
// session. The pin is dropped the moment the real git-derived stage moves off
// "pr" (new uncommitted work, a fresh commit) — at that point reality already
// matches the reset, so the true stage takes over again.
const loopReset = new Set<string>();

export function markLoopReset(title: string) {
  if (title) loopReset.add(title);
}

export function clearLoopReset(title: string) {
  loopReset.delete(title);
}

/** Per-poll reconcile: forget the pin once the git-derived stage has genuinely
 * left "pr", so we never override a stage the backend already agrees with. */
export function reconcileLoopReset(inst: Instance) {
  if (loopReset.has(inst.title) && (inst.stage || "agent") !== "pr")
    loopReset.delete(inst.title);
}

/** The guided stage to render: the pinned "agent" while the loop-reset is
 * active, otherwise the real git-derived stage. */
function guidedStage(inst: Partial<Instance>): string {
  if (inst.title && loopReset.has(inst.title)) return "agent";
  return inst.stage || "agent";
}

// --- Live agent activity, debounced across 2 polls (events push instantly) --

const actShown = new Map<string, string>();
const actPending = new Map<string, { value: string; count: number }>();

export function noteActivity(inst: Instance) {
  const title = inst.title;
  const raw = inst.activity || "idle";
  if (!actShown.has(title)) {
    actShown.set(title, raw);
    return;
  }
  if (actShown.get(title) === raw) {
    actPending.delete(title);
    return;
  }
  const p = actPending.get(title);
  if (p && p.value === raw) {
    p.count += 1;
    if (p.count >= 2) {
      actShown.set(title, raw);
      actPending.delete(title);
    }
  } else {
    actPending.set(title, { value: raw, count: 1 });
  }
}

/** Authoritative push from the event stream — no debounce. */
export function forceActivity(title: string, value: string) {
  if (!title) return;
  actShown.set(title, value || "idle");
  actPending.delete(title);
}

export function dropActivity(title: string) {
  actShown.delete(title);
  actPending.delete(title);
}

export function effectiveActivity(inst: Partial<Instance>): string {
  return actShown.get(inst.title || "") || inst.activity || "idle";
}

// --- The one persistent chip -------------------------------------------------

export interface ChipState {
  label: string;
  cls: string;
  title: string;
}

export function chipState(inst: Partial<Instance>): ChipState {
  if (inst.workspace_missing)
    return {
      label: "missing",
      cls: "s-missing",
      title: "Workspace directory no longer exists — clean up this session",
    };
  if (inst.status === "loading")
    return { label: "provisioning", cls: "s-provisioning", title: "Provisioning workspace" };
  if (inst.status === "paused")
    return { label: "paused", cls: "s-paused", title: "Session paused" };
  if (inst.setup && inst.setup.state === "failed")
    return {
      label: "setup ✗",
      cls: "s-interrupt",
      title:
        "Worktree setup failed — prompts are held; re-run it from the actions menu (log: .mindflock_setup.log)",
    };
  if (inst.setup && inst.setup.state === "running")
    return {
      label: "setting up",
      cls: "s-provisioning",
      title:
        "Worktree setup running (deps / env files) — queued prompts are held until it finishes",
    };
  const act = effectiveActivity(inst);
  if (act === "working") return { label: "running", cls: "s-running", title: "Agent is working" };
  if (act === "clarify")
    return {
      label: "clarify",
      cls: "s-clarify",
      title: "Agent paused to ask you a question — needs your answer",
    };
  if (act === "limit")
    return {
      label: "limit",
      cls: "s-limit",
      title:
        "Usage limit reached — the queue waits out the window and auto-resumes when it resets",
    };
  const stage = guidedStage(inst);
  if (stage === "agent")
    return act === "offline"
      ? { label: "offline", cls: "s-offline", title: "Agent offline" }
      : { label: "idle", cls: "s-idle", title: "Agent is idle — waiting for input" };
  if (stage === "interrupt") {
    const step = (inst.failed_step || "").trim();
    // A pre-commit hook NAME is pill-sized ("ruff", "Run Tests (+3)"), and that
    // is what the badge is for. The server's generic fallback can instead hand
    // back a whole line of hook output (up to 80 chars) — real detail, but not a
    // pill: badging it would either overflow the row or force the chip to be
    // truncated, and a truncated pill is exactly what we don't want. So long
    // details ride in the tooltip and the badge stays generic.
    const badgeable = !!step && step.length <= STEP_BADGE_MAX;
    return {
      label: badgeable ? "✗ " + step : "pre-commit ✗",
      cls: "s-interrupt",
      title: step ? "Pre-commit failed at: " + step : "Pre-commit failed",
    };
  }
  const m = stageMeta(stage);
  return { label: m.label, cls: "s-" + stage, title: m.label };
}

/** O3 verification-gate chip descriptor (second, smaller chip) or null. */
export function checkChip(
  inst: Partial<Instance>
): { label: string; cls: string; title: string } | null {
  const c = inst.check;
  if (!c || !c.state) return null;
  const cmd = String((c as { command?: string }).command || "");
  if (c.state === "running")
    return { label: "checks…", cls: "s-provisioning", title: "Checks running: " + cmd };
  if (c.state === "failed") {
    const rc = (c as { rc?: number | null }).rc;
    return {
      label: "✗ checks",
      cls: "s-interrupt",
      title:
        "Checks failed (exit " + (rc == null ? "?" : rc) + ") — push is gated until they pass (or push anyway)",
    };
  }
  if (c.state === "ok") {
    return (c as { stale?: boolean }).stale
      ? {
          label: "✓ stale",
          cls: "s-idle",
          title: "Checks passed for an older commit — a fresh run starts after the next commit",
        }
      : { label: "✓ checks", cls: "s-committed", title: "Checks passed for this commit" };
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
 * names the step, and — when a chain is armed — says what it is waiting on and
 * where it is heading.
 */

export type StepTone = "work" | "blocked" | "ok" | "quiet";

export interface LiveStep {
  label: string;
  tone: StepTone;
  title: string;
  /** Short "→ PR" target suffix while a chain is armed. */
  target?: string;
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

  // An armed chain: say what it is waiting on and where it is going.
  const run = inst.autopilot;
  if (run && run.depth && run.state === "running")
    return {
      label: run.note || "fast-tracking",
      tone: "work",
      title: autopilotChipTitle(run),
      target: DEPTH_SHORT[run.depth] || "",
    };
  if (run && run.depth && run.state === "halted")
    return { label: "fast-track ✗", tone: "blocked", title: autopilotChipTitle(run) };

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
  if (first && !opts?.live) return null; // seed only — never steal focus on load
  if (step === "commit") {
    // Side effects are guarded: this runs once per session per poll, so a DOM
    // hiccup here must never break the loop for every other session.
    try {
      useUi.getState().setLastTab(title, "shell");
      selectSession(title);
    } catch {
      /* following is a courtesy — never let it break the poll */
    }
    return "commit";
  }
  if (step === "pr") {
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

/** The ⏩ sibling of the guided button: a per-session TOGGLE for autopilot.
 *
 * It stays visible while a chain is armed and turns it off on click. It used to
 * hide itself once armed, leaving the only off-switch on the primary button —
 * which read as "the button I pressed disappeared and I can't undo it". A toggle
 * is what a one-press control has to be.
 *
 * Returns null only when a press would be meaningless: no git, a session that
 * isn't running, a missing workspace, or a commit already in flight. */
export function fastTrackStep(inst: Partial<Instance>): NextStep | null {
  const title = inst.title;
  const caps = queryClient.getQueryData<Config>(["config"])?.caps;
  if (caps && !caps.git) return null;
  if (!title) return null;

  const run = inst.autopilot;
  const armed = !!(run && run.depth && (run.state === "running" || run.state === "halted"));
  // AN ARMED RUN IS ALWAYS CANCELLABLE. The guards below hide the OFF state where
  // a press would be meaningless (still provisioning, a commit in flight), but
  // they must never hide the ON state: an intake-armed session spends its first
  // minutes provisioning, and that is precisely when you might change your mind.
  // Hiding the toggle there left no way to stop it at all.
  if (!armed) {
    if (inst.status === "loading" || inst.status === "paused") return null;
    if (inst.workspace_missing) return null;
    const stage = guidedStage(inst);
    if (stage === "provisioning" || stage === "precommit") return null;
  }

  // Armed: show it ON and make the click turn it off.
  if (run && run.depth && run.state === "running")
    return {
      label: "⏩",
      active: true,
      title: autopilotChipTitle(run) + "\n\nClick ⏩ to turn fast-track off.",
      run: () => stopFastTrack(title),
    };
  // Halted: say so on the control itself, and let a click arm it again. A chain
  // that stopped without saying why is the failure mode that kills trust here.
  if (run && run.depth && run.state === "halted")
    return {
      label: "⏩✗",
      hint: true,
      title:
        autopilotChipTitle(run) + "\n\nClick ⏩✗ to start fast-track again.",
      run: () => startFastTrack(title, run.depth),
    };

  const depth = resolveDepth();
  return {
    label: "⏩",
    title:
      "Fast-track: carry this session to " +
      depthLabel(depth) +
      " on its own.\n" +
      "Waits for the agent to finish, then commits, pushes and stops at your\n" +
      "chosen rung. Click again to turn it off.\n" +
      "Change the default in Settings → Workspace.",
    run: () => startFastTrack(title, depth),
  };
}

export function nextStep(inst: Partial<Instance>): NextStep | null {
  const title = inst.title;
  const caps = queryClient.getQueryData<Config>(["config"])?.caps;
  if (caps && !caps.git) return null; // no git -> no commit/push/PR workflow
  if (!title || inst.status === "loading" || inst.status === "paused") return null;
  if (inst.workspace_missing) return null; // L7: Clean up lives in the row
  // An armed chain deliberately does NOT take over this slot. It used to, which
  // meant arming fast-track replaced your manual Commit/Push/Make PR button with
  // a status readout and left no way to act by hand. Autopilot state lives on the
  // ⏩ toggle beside this button (see fastTrackStep); the manual ladder below
  // always keeps working, so you can still drive a step yourself at any time.
  switch (guidedStage(inst)) {
    case "agent":
      return { label: "Commit…", run: () => commitSession(title) };
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
        ? { label: "Make PR", run: () => makePrSession(title) }
        : {
            label: "Make PR ↗",
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
        ? { label: "Open PR ↗", run: () => window.open(inst.pr_url!, "_blank") }
        : null;
    default:
      return null; // precommit (running), provisioning
  }
}
