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
  hasPrSupport,
  makePrSession,
  mergeSession,
  pushSession,
} from "./sessionActions";

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
}

export function nextStep(inst: Partial<Instance>): NextStep | null {
  const title = inst.title;
  const caps = queryClient.getQueryData<Config>(["config"])?.caps;
  if (caps && !caps.git) return null; // no git -> no commit/push/PR workflow
  if (!title || inst.status === "loading" || inst.status === "paused") return null;
  if (inst.workspace_missing) return null; // L7: Clean up lives in the row
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
      return { label: "Merge", run: () => mergeSession(title) };
    case "merged":
      return inst.pr_url
        ? { label: "Open PR ↗", run: () => window.open(inst.pr_url!, "_blank") }
        : null;
    default:
      return null; // precommit (running), provisioning
  }
}
