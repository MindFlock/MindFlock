/** ONE session state, read by every channel.
 *
 * The rail dot used to read `inst.status` (green for running), the chip read
 * the live activity and then the git stage, the bell kept its own list and
 * its own words ("needs your answer"), the toasts theirs ("needs your input"),
 * and the tab badge counted only clarify. A crashed agent therefore showed a
 * green dot beside an "offline" chip, and "limit" was painted red although
 * it auto-resumes. `deriveSessionState` is the single reading all of them
 * now share — the dot and the chip on a row are painted from the SAME object,
 * the bell lists exactly the states whose `inBell` is true, and the label
 * here is the word every surface uses.
 *
 *   state      tone       who acts   in bell   label
 *   starting   muted      nobody     no        Starting
 *   working    working    the agent  no        Working
 *   needs_you  attention  you        yes       the reason: Question · Pre-commit
 *                                              failed · Checks failing · Setup failed
 *   idle       neutral    nobody     20 min    Idle (the ship-stage chip shows
 *                                    + dirty   beside it; reason "stuck")
 *   limit      waiting    nobody     no        Waiting on limit[, resumes HH:MM]
 *   offline    broken     you        yes       Offline (the chip's Restart action)
 *
 * Two rows outside the table — a paused session and one whose folder is gone —
 * keep their own words ("Paused", "Missing"): neither is a state of the AGENT,
 * and neither waits on anyone in the bell.
 *
 * The live-activity debounce (`effectiveActivity` and friends) lives here too,
 * because it is the input every state reads; stage.ts re-exports it so the
 * guided ship ladder keeps its import. */

import type { Instance } from "../api/types";

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

// --- The state ----------------------------------------------------------------

export type SessionStateKey = "starting" | "working" | "needs_you" | "idle" | "limit" | "offline";
export type SessionReason = "question" | "precommit_failed" | "checks_failing" | "setup_failed" | "stuck";
export type SessionTone = "muted" | "working" | "attention" | "neutral" | "waiting" | "broken";

export interface SessionState {
  state: SessionStateKey;
  reason?: SessionReason;
  /** The one word (or phrase) every surface uses for this state. */
  label: string;
  tone: SessionTone;
  /** Listed under the bell's "Needs attention". */
  inBell: boolean;
  /** Epoch seconds the usage window reopens (limit only, when known). */
  resumeAt?: number;
  /** The chip offers Restart (offline, local, not paused, folder present). */
  restartable: boolean;
  /** Tooltip detail: the hook that failed, how long it has been idle. */
  hint?: string;
}

export interface SessionStateExtras {
  /** Epoch ms "now" (tests); default Date.now(). */
  now?: number;
  /** The live activity to read; default `effectiveActivity(inst)`. */
  activity?: string;
}

/** Idle-with-unfinished-work threshold before a session counts as stuck. */
export const STUCK_IDLE_S = 20 * 60;

/** The label of each state that has only one. */
export const STATE_LABEL: Record<Exclude<SessionStateKey, "needs_you">, string> = {
  starting: "Starting",
  working: "Working",
  idle: "Idle",
  limit: "Waiting on limit",
  offline: "Offline",
};

/** The label of each reason a session can need you. */
export const REASON_LABEL: Record<SessionReason, string> = {
  question: "Question",
  precommit_failed: "Pre-commit failed",
  checks_failing: "Checks failing",
  setup_failed: "Setup failed",
  stuck: "Idle",
};

/** The Settings → Notifications switch that gates a state's toast and badge
 * (the bell shows it regardless — the bell is the source of truth). A rule
 * the server does not list counts as on (`ruleOn`), so an id named here for
 * a switch that does not exist yet is harmless. "" = never gated. */
export const NOTIFY_RULE: Record<SessionReason | "offline", string> = {
  question: "needs_input",
  precommit_failed: "precommit_failed",
  setup_failed: "setup_failed",
  checks_failing: "checks_failing",
  stuck: "",
  offline: "session_offline",
};

export function notifyRuleFor(ss: SessionState): string {
  if (ss.state === "offline") return NOTIFY_RULE.offline;
  if (ss.reason) return NOTIFY_RULE[ss.reason];
  return "";
}

/** Bell priority (lower = more urgent): a question first, then broken, then
 * checks. Mirrors the ranks `needsAttention` merges with. */
export function attentionPriority(ss: SessionState): number {
  if (ss.reason === "question") return 0;
  if (ss.reason === "checks_failing") return 2;
  return 1; // pre-commit / setup failed, offline, stuck
}

/** "HH:MM" local time for an epoch in seconds. */
export function fmtClock(epochS: number): string {
  const d = new Date(epochS * 1000);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return hh + ":" + mm;
}

/** "25m" / "3h" for a span in seconds, the rail's idle-for word. */
function spanWord(secs: number): string {
  if (secs < 3600) return Math.max(1, Math.round(secs / 60)) + "m";
  if (secs < 86400) return Math.round(secs / 3600) + "h";
  return Math.round(secs / 86400) + "d";
}

function uncommittedWork(inst: Partial<Instance>): boolean {
  const un = (inst.diff_stat as { uncommitted?: { additions?: number; deletions?: number } } | null | undefined)
    ?.uncommitted;
  return (Number(un?.additions) || 0) + (Number(un?.deletions) || 0) > 0;
}

function needsYou(reason: SessionReason, hint?: string): SessionState {
  return { state: "needs_you", reason, label: REASON_LABEL[reason], tone: "attention", inBell: true, restartable: false, hint };
}

/** The one session state. Pure: reads the row and the debounced activity. */
export function deriveSessionState(inst: Partial<Instance>, extras?: SessionStateExtras): SessionState {
  const now = extras?.now ?? Date.now();
  if (inst.workspace_missing)
    return {
      state: "offline",
      label: "Missing",
      tone: "broken",
      inBell: false,
      restartable: false,
      hint: "Workspace directory no longer exists — delete this session",
    };
  if (inst.status === "paused")
    return { state: "idle", label: "Paused", tone: "muted", inBell: false, restartable: false, hint: "Session paused" };
  if (inst.pending || inst.status === "loading")
    return { state: "starting", label: STATE_LABEL.starting, tone: "muted", inBell: false, restartable: false, hint: "Provisioning workspace" };
  if (inst.setup && inst.setup.state === "running")
    return {
      state: "starting",
      label: STATE_LABEL.starting,
      tone: "muted",
      inBell: false,
      restartable: false,
      hint: "Worktree setup running (deps / env files) — queued prompts are held until it finishes",
    };
  if (inst.setup && inst.setup.state === "failed")
    return needsYou(
      "setup_failed",
      "Worktree setup failed — prompts are held; re-run it from the actions menu (log: .mindflock_setup.log)"
    );
  const act = extras?.activity ?? effectiveActivity(inst);
  if (act === "clarify")
    return needsYou("question", "The agent asked you a question — answer it in its pane or from the bell");
  if (act === "working")
    return { state: "working", label: STATE_LABEL.working, tone: "working", inBell: false, restartable: false, hint: "Agent is working" };
  if (act === "limit") {
    const until = Number(inst.queue?.limited_until) || 0;
    const resumeAt = until > now / 1000 ? until : undefined;
    return {
      state: "limit",
      label: STATE_LABEL.limit + (resumeAt ? ", resumes " + fmtClock(resumeAt) : ""),
      tone: "waiting",
      inBell: false,
      resumeAt,
      restartable: false,
      hint: "Usage limit reached — the queue waits out the window and auto-resumes when it resets",
    };
  }
  if (act === "offline")
    return {
      state: "offline",
      label: STATE_LABEL.offline,
      tone: "broken",
      inBell: true,
      restartable: !inst.device,
      hint: "The agent's terminal is gone — Restart relaunches it (resuming the conversation)",
    };
  if (inst.stage === "interrupt") {
    const step = (inst.failed_step || "").trim();
    return needsYou("precommit_failed", step ? "Pre-commit failed at: " + step : "Pre-commit failed");
  }
  if (inst.check && inst.check.state === "failed" && !(inst.check as { stale?: boolean }).stale)
    return needsYou("checks_failing", "Checks failed — push is gated until they pass (or push anyway)");
  // Idle. In the bell only once it has sat on uncommitted work for a while:
  // an agent that stopped in the middle of something with nobody typing. A
  // committed branch is not unfinished work — git considers it done.
  const since = Number(inst.activity_since) || 0;
  const idleFor = since > 0 ? now / 1000 - since : 0;
  if (idleFor > STUCK_IDLE_S && uncommittedWork(inst))
    return {
      state: "idle",
      reason: "stuck",
      label: STATE_LABEL.idle,
      tone: "neutral",
      inBell: true,
      restartable: false,
      hint: "Idle " + spanWord(idleFor) + " with unfinished work — possibly stuck",
    };
  return { state: "idle", label: STATE_LABEL.idle, tone: "neutral", inBell: false, restartable: false, hint: "Agent is idle — waiting for input" };
}

/** The bell row's words for a state that is in the bell: the label, or for a
 * stuck session the hint (its label is just "Idle"). */
export function attentionReason(ss: SessionState): string {
  return ss.reason === "stuck" && ss.hint ? ss.hint : ss.label;
}
