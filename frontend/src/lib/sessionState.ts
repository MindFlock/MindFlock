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
 *   needs_you  attention  you        yes       the reason: Question (a dialog, or
 *                                              a reply that asks in plain words) ·
 *                                              You: <step> (a flight parked on a
 *                                              *you* step, docs/flights.md) ·
 *                                              Halted (a route halted at a
 *                                              stop until you Retry or Skip;
 *                                              the broken tone — red) ·
 *                                              Pre-commit failed · Checks failing ·
 *                                              Setup failed
 *   idle       neutral    nobody     no        Idle (the ship-stage chip shows
 *                                              beside it)
 *   idle       attention  you        yes       Idle 25m — 20 min on uncommitted
 *   (stuck)                                    work (reason "stuck")
 *   limit      waiting    nobody     no        Waiting on limit[, resumes HH:MM]
 *   offline    broken     you        yes       Offline (the chip's Restart action)
 *   offline    broken     you        no        Offline — HELD after a crash
 *   (held)                                     (lib/heldOffline): the chip says
 *                                              "Offline · Start"; one rail
 *                                              notice instead of the bell
 *
 * Verify (the row's `verify` — the session's own agent checking its work, in
 * its own terminal; docs/web-ui.md "Verify"):
 *
 *   working    verifying  the agent  no        Verifying ("3 of 5 steps done")
 *   needs_you  attention  you        yes       Confirm verified (its steps done;
 *                                              the hint lists yours)
 *   needs_you  broken     you        yes       Verify failed · Verify blocked
 *   idle       verified   nobody     no        Verified (for ten minutes)
 *
 * Three rows outside the table — a paused session, one whose folder is gone,
 * and one whose agent is asleep — keep their own words ("Paused", "Missing",
 * "Sleeping"): none is a state of a running AGENT, and none waits on anyone in
 * the bell. A sleeping agent (backend core/sleep.py) was stopped on purpose,
 * idle, to give its memory back; its activity reads "offline", but it is not
 * broken — it is neither Offline nor counted by "Restart all", and opening it
 * or sending it anything wakes it.
 *
 * The live-activity debounce (`effectiveActivity` and friends) lives here too,
 * because it is the input every state reads; stage.ts re-exports it so the
 * guided ship ladder keeps its import. */

import type { Instance, SessionVerify } from "../api/types";
import { backgroundHint } from "./backgroundJobs";
import { agentName } from "./agentNames";
import { haltedWhy, perchLabel } from "./flights";
import { heldHint, isHeldOffline } from "./heldOffline";

// --- Live agent activity, debounced across 2 polls (events push instantly) --

const actShown = new Map<string, string>();
const actPending = new Map<string, { value: string; count: number }>();

export function noteActivity(inst: Instance) {
  const title = inst.title;
  if (inst.sleeping) {
    // Asleep: its "offline" is not a reading to carry into the wake — drop
    // it, so the first row after the wake is shown as-is instead of keeping
    // "Offline" up for two more polls.
    dropActivity(title);
    return;
  }
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
export type SessionReason =
  | "question"
  | "flight_you"
  | "flight_halted"
  | "precommit_failed"
  | "checks_failing"
  | "setup_failed"
  | "stuck"
  | "verify_confirm"
  | "verify_failed"
  | "verify_blocked";
export type SessionTone = "muted" | "working" | "attention" | "neutral" | "waiting" | "broken" | "verifying" | "verified";

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
  /** Held offline after a crash (lib/heldOffline): its remedy is Start, and
   * nothing else will bring it back. */
  held?: boolean;
  /** Waiting for memory (lib/launchGate): it starts by itself once memory
   * allows; its remedy is Start anyway. */
  waiting?: boolean;
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
  // "Wait for me: <the stop's title>" when the row names it (flightYouLabel).
  flight_you: "Wait for me",
  // A route halted at a stop until you Retry or Skip it; the reason (the
  // stop and what failed) is the chip's tooltip, not its words.
  flight_halted: "Halted",
  precommit_failed: "Pre-commit failed",
  checks_failing: "Checks failing",
  setup_failed: "Setup failed",
  stuck: "Idle",
  // The agent did its own verify steps; a person did theirs and says so.
  verify_confirm: "Confirm verified",
  verify_failed: "Verify failed",
  verify_blocked: "Verify blocked",
};

/** The calm words of a verify that is under way, and one that passed. */
export const VERIFYING_LABEL = "Verifying";
export const VERIFIED_LABEL = "Verified";

/** The Settings → Notifications switch that gates a state's toast and badge
 * (the bell shows it regardless — the bell is the source of truth). A rule
 * the server does not list counts as on (`ruleOn`), so an id named here for
 * a switch that does not exist yet is harmless. "" = never gated. */
export const NOTIFY_RULE: Record<SessionReason | "offline", string> = {
  question: "needs_input",
  flight_you: "flight_needs_you",
  // The server rings a halt as one of a route's finishes (addons/notify.py).
  flight_halted: "flight_finished",
  precommit_failed: "precommit_failed",
  setup_failed: "setup_failed",
  checks_failing: "checks_failing",
  stuck: "",
  verify_confirm: "verify_needs_you",
  verify_failed: "verify_failed",
  verify_blocked: "verify_failed",
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
  return 1; // pre-commit / setup failed, offline, stuck, verify
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

/** The row says its agent asked a question in plain words (`asking.kind`
 * "question"). Read here rather than through lib/ask (which reads this file). */
function askedInWords(inst: Partial<Instance>): boolean {
  const a = (inst as { asking?: { kind?: unknown } | null }).asking;
  return !!a && typeof a === "object" && a.kind === "question";
}

function needsYou(reason: SessionReason, hint?: string): SessionState {
  return { state: "needs_you", reason, label: REASON_LABEL[reason], tone: "attention", inBell: true, restartable: false, hint };
}

/** "Perch: Approve the merge" — a flight perched on you, as the rail chip says
 * it (never "Perch: Perch: …" for a stop whose title already says so). */
export function flightYouLabel(stepTitle: string | null | undefined): string {
  return perchLabel(stepTitle);
}

/** Why a route halted, without the stop's own name in front (lib/flights,
 * where the pane strip's line reads it too). */
export { haltedWhy };

/** The tooltip of a halted route's chip: "Halted at CI checks — Failed: 2
 * of 14 checks red", then which route waits and where to act. */
export function haltedHint(fl: { plan_name?: string; reason?: string; step?: { title?: string } | null }): string {
  const at = String(fl.step?.title || "").trim();
  const why = haltedWhy(at, fl.reason);
  return (
    "Halted" + (at ? " at " + at : "") + (why ? " — " + why : "") +
    "\nRoute “" + (fl.plan_name || "") + "” waits for Retry or Skip on its strip, the Sky or the bell"
  );
}

function haltedState(fl: NonNullable<Instance["flight"]>): SessionState {
  return { ...needsYou("flight_halted", haltedHint(fl as Parameters<typeof haltedHint>[0])), tone: "broken" };
}

/** The rail chip of a session whose flight is paused: a calm idle state,
 * not a needs-you one — whoever paused it resumes it. */
export const PAUSED_FLIGHT_LABEL = "Paused route";

/** The rail's word for a held worker whose agent starts on its turn. */
export const HELD_WORKER_LABEL = "Waiting";

/** The rail's word for a session whose start waits for memory. */
export const WAITING_FOR_MEMORY_LABEL = "Waiting for memory";

function waitingState(w: NonNullable<Instance["waiting_for_memory"]>): SessionState {
  const hint =
    "Waiting for memory" +
    (w.reason ? " — " + w.reason : "") +
    (w.position > 1 ? " (" + w.position + " in line)" : "") +
    ". It starts by itself once there is room; Start anyway starts it now.";
  return { state: "starting", label: WAITING_FOR_MEMORY_LABEL, tone: "muted", inBell: false, restartable: false, waiting: true, hint };
}

/** The rail's word for a sleeping session. */
export const SLEEPING_LABEL = "Sleeping";

function sleepingState(info: NonNullable<Instance["sleeping"]>, now: number): SessionState {
  const since = Number(info.since) || 0;
  const freed = Number(info.freed_mb) || 0;
  let hint = "Asleep";
  if (since > 0) hint += " since " + fmtClock(since) + " (" + spanWord(now / 1000 - since) + ")";
  hint += info.reason === "memory" ? " — memory was low" : info.reason === "manual" ? "" : " — it was idle";
  if (freed > 0) hint += "; freed ~" + (freed >= 1024 ? (freed / 1024).toFixed(1) + " GB" : freed + " MB");
  hint += ". Opening it or sending it anything wakes it, resuming the conversation.";
  return { state: "idle", label: SLEEPING_LABEL, tone: "muted", inBell: false, restartable: false, hint };
}

// --- Verify ------------------------------------------------------------------

/** The row's verify, or null — an older server sends no `verify` at all. */
export function verifyOf(inst: Partial<Instance>): SessionVerify | null {
  const v = (inst as { verify?: SessionVerify | null }).verify;
  return v && typeof v === "object" && typeof v.state === "string" ? v : null;
}

/** "3 of 5 steps done" — the agent's own steps (a person's are not its to do). */
export function verifyProgress(v: Pick<SessionVerify, "steps">): string {
  const total = Number(v.steps?.total) || 0;
  const human = Number(v.steps?.human) || 0;
  const done = Number(v.steps?.agent_done) || 0;
  const mine = Math.max(0, total - human);
  if (!total) return "writing its steps";
  if (!mine) return "every step needs a person";
  return Math.min(done, mine) + " of " + mine + (mine === 1 ? " step" : " steps") + " done";
}

/** One bulleted line per step: "• Open the app — the banner shows". */
export function verifyStepLines(steps: ReadonlyArray<{ text: string; expect?: string; note?: string }>): string {
  return steps
    .map((s) => {
      const tail = String(s.expect || s.note || "").trim();
      return "• " + String(s.text || "").trim() + (tail ? " — " + tail : "");
    })
    .join("\n");
}

/** The one word of each verify state — the row's pill, and the Sky's Verify
 * tile (flights/SkyChain `behaviorsMarkOf`): Verifying, Confirm verified,
 * Verify failed, Verify blocked, Verified; "" for a state it doesn't know. */
export function verifyWord(state: string): string {
  switch (state) {
    case "verifying":
      return VERIFYING_LABEL;
    case "needs_you":
      return REASON_LABEL.verify_confirm;
    case "failed":
      return REASON_LABEL.verify_failed;
    case "blocked":
      return REASON_LABEL.verify_blocked;
    case "passed":
      return VERIFIED_LABEL;
  }
  return "";
}

/** The glyph in front of each verify state's line (the pane's verify row):
 * ✓ only once it is verified. */
export function verifyGlyph(state: string): string {
  switch (state) {
    case "verifying":
      return "…";
    case "needs_you":
      return "?";
    case "failed":
      return "✗";
    case "blocked":
      return "!";
    case "passed":
      return "✓";
  }
  return "";
}

/** A sign-off with no step left for a person: the agent did them all and
 * the person only confirms (Who: "Agent, then my sign-off"). */
function signOffOnly(v: Pick<SessionVerify, "steps" | "human_steps">): boolean {
  return !(v.human_steps || []).length && !(Number(v.steps?.human) || 0);
}

/** The tooltip (and the bell's line) of each verify state. */
export function verifyHint(v: SessionVerify): string {
  const human = Number(v.steps?.human) || 0;
  switch (v.state) {
    case "verifying":
      return (
        "Checking its work: " + verifyProgress(v) +
        (human ? " · " + human + (human === 1 ? " step needs" : " steps need") + " a person after" : "")
      );
    case "needs_you": {
      if (signOffOnly(v)) {
        const said = String(v.detail || "").trim();
        return "The agent did its steps" + (said ? " — " + said : "") + ". Press Confirm verified once you're sure it works (or Not verified…)";
      }
      const list = verifyStepLines(v.human_steps || []);
      const n = (v.human_steps || []).length || human;
      return (
        "The agent checked what it can. Do " + (n === 1 ? "this step" : "these steps") +
        ", then press Confirm verified (or Not verified…):" + (list ? "\n" + list : "")
      );
    }
    case "failed": {
      const list = verifyStepLines(v.failed_steps || []);
      return "Verify failed" + (list ? ":\n" + list : v.detail ? " — " + v.detail : "");
    }
    case "blocked":
      return "Verify couldn't go on" + (v.detail ? " — " + v.detail : "") + ". Open the session to see why";
    case "passed":
      return "Verified" + (v.detail ? " — " + v.detail : "");
  }
  return "";
}

/** The pane's verify row in one line: "Verifying — 3 of 5 steps done",
 * "Confirm verified — the agent did its steps; do yours, then confirm",
 * "Verify failed — 2 steps", "Verify blocked — <why>", "Verified". */
export function verifyLine(v: SessionVerify): string {
  const detail = String(v.detail || "").trim();
  switch (v.state) {
    case "verifying":
      return VERIFYING_LABEL + " — " + verifyProgress(v);
    case "needs_you":
      if (signOffOnly(v)) return REASON_LABEL.verify_confirm + " — " + (detail || "the agent did its steps; confirm it works");
      return REASON_LABEL.verify_confirm + " — the agent did its steps; do yours, then confirm";
    case "failed": {
      const n = (v.failed_steps || []).length;
      return REASON_LABEL.verify_failed + (n ? " — " + n + (n === 1 ? " step" : " steps") : detail ? " — " + detail : "");
    }
    case "blocked":
      return REASON_LABEL.verify_blocked + (detail ? " — " + detail : "");
    case "passed":
      return VERIFIED_LABEL + (detail ? " — " + detail : "");
  }
  return "";
}

function verifyNeedsYou(v: SessionVerify): SessionState | null {
  if (v.state === "needs_you") return needsYou("verify_confirm", verifyHint(v));
  if (v.state === "failed") return { ...needsYou("verify_failed", verifyHint(v)), tone: "broken" };
  if (v.state === "blocked") return { ...needsYou("verify_blocked", verifyHint(v)), tone: "broken" };
  return null;
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
  // Mid-move (lib/sessionActions moveSession → state/queries markMoving): the
  // agent is paused while its folder travels; it resumes there by itself.
  if ((inst as Instance & { moving?: boolean }).moving)
    return { state: "starting", label: "Moving…", tone: "muted", inBell: false, restartable: false, hint: "Moving the session's folder — it resumes there when the move is done" };
  if (inst.waiting_for_memory) return waitingState(inst.waiting_for_memory);
  if (inst.pending || inst.status === "loading")
    return { state: "starting", label: STATE_LABEL.starting, tone: "muted", inBell: false, restartable: false, hint: "Provisioning workspace" };
  if (inst.sleeping) return sleepingState(inst.sleeping, now);
  // Held offline after a crash (backend core/held.py): no agent, no memory,
  // and nothing starts it but a person's Start. Out of the bell — after a
  // crash that would be every session at once; the rail's one notice says it.
  if (isHeldOffline(inst))
    return { state: "offline", label: STATE_LABEL.offline, tone: "broken", inBell: false, restartable: true, held: true, hint: heldHint(inst.held_reason) };
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
  // A flight perched on you (a `you` stop): it waits for Done, whatever the
  // agent is doing — so ahead of Working.
  const fl = inst.flight;
  if (fl && fl.state === "waiting_you" && fl.step?.actor === "you")
    return {
      ...needsYou(
        "flight_you",
        flightYouLabel(fl.step.title) + " — route “" + fl.plan_name + "” needs you; press Done on its strip, the Sky or the bell"
      ),
      label: flightYouLabel(fl.step.title),
    };
  // A route halted at a stop until you Retry or Skip it: the rank of a Wait
  // for me stop (it waits on you whatever the agent does), in red — behind a
  // question in plain words too, so the session keeps its answer row.
  if (fl && fl.id && fl.state === "halted" && !askedInWords(inst)) return haltedState(fl);
  const bgHint = backgroundHint(inst);
  const verify = verifyOf(inst);
  // The agent checking its own work: its own pill, whether it is mid-turn or
  // between steps — a usage limit and a dead terminal still say so below.
  if (verify?.state === "verifying" && act !== "limit" && act !== "offline")
    return { state: "working", label: VERIFYING_LABEL, tone: "verifying", inBell: false, restartable: false, hint: verifyHint(verify) };
  if (act === "working")
    return { state: "working", label: STATE_LABEL.working, tone: "working", inBell: false, restartable: false, hint: bgHint || "Agent is working" };
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
  // A held worker (core.worker_order): created, its agent not started until
  // the workers before it finish — waiting, not down. Not restartable, so
  // "Start all" leaves it alone (its own Start still boots it early).
  if (act === "offline" && inst.order?.state === "held")
    return {
      state: "starting",
      label: HELD_WORKER_LABEL,
      tone: "muted",
      inBell: false,
      restartable: false,
      hint:
        "Waiting its turn" +
        (inst.order.detail ? " — " + inst.order.detail : "") +
        ". Its agent starts when the workers before it finish; Start starts it early.",
    };
  if (act === "offline") {
    // Killed for memory and coming back by itself: nothing to wait on you for
    // (out of the bell), and the hint says why it is down.
    const revive = inst.auto_revive;
    return {
      state: "offline",
      label: STATE_LABEL.offline,
      tone: "broken",
      inBell: revive?.state !== "pending",
      restartable: !inst.device,
      hint: revive?.reason || "The agent's terminal is gone — Restart relaunches it (resuming the conversation)",
    };
  }
  // Verify waits on a person (Confirm verified), or failed: once the agent's
  // turn is over — ahead of the Question its reply may end with, since the
  // reply IS the human steps and the confirm is what answers it.
  const verifyYou = verify && verifyNeedsYou(verify);
  if (verifyYou) return verifyYou;
  // The agent's reply ended with a question in plain words and nobody has
  // typed to it since (the row's `asking`, lib/ask.ts): the same Question as
  // a dialog — the chip, the toast and the bell all read this one state.
  if (askedInWords(inst))
    return needsYou("question", "The agent asked you a question in its reply — answer it from the pop-up, the bell or its pane");
  if (inst.stage === "interrupt") {
    const step = (inst.failed_step || "").trim();
    return needsYou("precommit_failed", step ? "Pre-commit failed at: " + step : "Pre-commit failed");
  }
  if (inst.check && inst.check.state === "failed" && !(inst.check as { stale?: boolean }).stale)
    return needsYou("checks_failing", "Checks failed — push is gated until they pass (or push anyway)");
  // A paused flight: calm, out of the bell, and said as what it is.
  if (inst.flight?.state === "paused")
    return {
      state: "idle",
      label: PAUSED_FLIGHT_LABEL,
      tone: "neutral",
      inBell: false,
      restartable: false,
      hint:
        "Route “" + inst.flight.plan_name + "” " +
        ((inst.flight as { reason?: string }).reason === "halt point" ? "reached its halt point at " : "is paused at ") +
        (inst.flight.step?.title || "its current stop") +
        " — Carry on from its strip or its stop on the Sky",
    };
  // Verified, calm, for the ten minutes the server keeps saying so.
  if (verify?.state === "passed")
    return { state: "idle", label: VERIFIED_LABEL, tone: "verified", inBell: false, restartable: false, hint: verifyHint(verify) };
  // Idle. In the bell only once it has sat on uncommitted work for a while:
  // an agent that stopped in the middle of something with nobody typing. A
  // committed branch is not unfinished work — git considers it done.
  const since = Number(inst.activity_since) || 0;
  const idleFor = since > 0 ? now / 1000 - since : 0;
  // Its chip says how long ("Idle 25m") in the attention tone: the bell counts
  // it, so the row must not read like any other idle one.
  if (idleFor > STUCK_IDLE_S && uncommittedWork(inst))
    return {
      state: "idle",
      reason: "stuck",
      label: STATE_LABEL.idle + " " + spanWord(idleFor),
      tone: "attention",
      inBell: true,
      restartable: false,
      hint: "Idle " + spanWord(idleFor) + " with unfinished work — possibly stuck",
    };
  return { state: "idle", label: STATE_LABEL.idle, tone: "neutral", inBell: false, restartable: false, hint: bgHint || "Agent is idle — waiting for input" };
}

/** What a "needs you" chip does when clicked — it is a door to where you act,
 * never a label: a Question opens the bell at its row (the answer strip is
 * there), a failed pre-commit hook the session's Terminal, a failed setup
 * re-runs it, failing checks open the PR (its checks) or, with no PR yet, the
 * Terminal. Null for every other state. `aria` says it for a screen reader. */
export type NeedsYouAct = "bell" | "terminal" | "setup" | "pr" | "verify";
export function needsYouAction(
  ss: SessionState,
  prUrl?: string | null
): { act: NeedsYouAct; aria: string } | null {
  if (ss.state !== "needs_you") return null;
  switch (ss.reason) {
    case "question":
      return { act: "bell", aria: "Question — answer it in the bell" };
    case "flight_you":
      return { act: "bell", aria: ss.label + " — press Done in the bell" };
    case "flight_halted":
      return { act: "bell", aria: "Halted — Retry or Skip in the bell" };
    case "precommit_failed":
      return { act: "terminal", aria: "Pre-commit failed — open its Terminal" };
    case "setup_failed":
      return { act: "setup", aria: "Setup failed — re-run worktree setup" };
    case "verify_confirm":
      return { act: "verify", aria: "Confirm verified — open the session to confirm" };
    case "verify_failed":
      return { act: "verify", aria: "Verify failed — open the session to see what failed" };
    case "verify_blocked":
      return { act: "verify", aria: "Verify blocked — open the session to see why" };
    case "checks_failing":
      return prUrl
        ? { act: "pr", aria: "Checks failing — open the PR" }
        : { act: "terminal", aria: "Checks failing — open its Terminal (output in .mindflock_check.log)" };
    default:
      return null;
  }
}

/** The bell row's words for a state that is in the bell: the label, or for a
 * stuck session the hint (its label is just "Idle"). */
export function attentionReason(ss: SessionState): string {
  return ss.reason === "stuck" && ss.hint ? ss.hint : ss.label;
}

// --- An agent that can't launch -------------------------------------------

/** What GET /api/setup says about each agent CLI (`logins`): enough to tell
 * a session whose agent isn't on this machine, or isn't signed in. */
export interface AgentInstallRow {
  name: string;
  installed: boolean;
  /** true = a login was found, false = none where the CLI keeps it, null =
   * can't tell (never read as signed out). */
  logged_in?: boolean | null;
}

/** The session's agent is not installed here: the CLI the session runs is
 * one the server knows and reports missing, and nothing says the agent is
 * running anyway. Its terminal printed `claude: not found` and exited — Idle
 * then Offline/Restart was a lie about a session that can never start; this
 * says so, and the row offers Install. Remote rows (another device's
 * install) and custom providers the server doesn't list never match. `null`
 * when the agent is there (or can't be told). */
export function agentMissing(
  inst: Partial<Instance>,
  logins: AgentInstallRow[] | null | undefined,
  activity: string = effectiveActivity(inst)
): string | null {
  if (!logins || !logins.length || inst.device || inst.pending) return null;
  if (activity === "working" || activity === "clarify" || activity === "limit") return null;
  const name = String(inst.provider || inst.program || "").trim();
  if (!name) return null;
  const row = logins.find((l) => l.name === name);
  return row && row.installed === false ? name : null;
}

/** The state a session with a missing agent shows instead of Idle/Offline:
 * "Failed: claude isn't installed", in the bell's broken tone. */
export function agentMissingState(name: string): SessionState {
  return {
    state: "offline",
    label: "Failed: " + name + " isn't installed",
    tone: "broken",
    inBell: true,
    restartable: false,
    hint: name + " isn't installed on this computer, so this session's agent couldn't start — install it, then Restart",
  };
}

/** Why a session's agent can't do its task: not installed here, or installed
 * but not signed in. `exited` = its terminal is gone (the agent quit at
 * launch), so the fix is sign in, then Restart. */
export interface AgentBlock {
  name: string;
  why: "missing" | "signed_out";
  exited: boolean;
}

/** `agentMissing`, plus the signed-out case: the CLI is installed, its login
 * probe found nothing (`logged_in === false` — "can't tell" never counts), the
 * session runs the ambient login (no account profile of its own), and the
 * agent is not working. A first-run Claude Code then sits at its theme
 * picker or login questions, which read as Idle or a Question — neither is
 * true, and the session can never start on its task until it is signed in. */
export function agentBlocked(
  inst: Partial<Instance>,
  logins: AgentInstallRow[] | null | undefined,
  activity: string = effectiveActivity(inst)
): AgentBlock | null {
  const missing = agentMissing(inst, logins, activity);
  if (missing) return { name: missing, why: "missing", exited: activity === "offline" };
  if (!logins || !logins.length || inst.device || inst.pending) return null;
  if (activity === "working" || activity === "limit") return null;
  if (inst.profile_effective) return null;
  const name = String(inst.provider || inst.program || "").trim();
  if (!name) return null;
  const row = logins.find((l) => l.name === name);
  if (!row || !row.installed || row.logged_in !== false) return null;
  return { name, why: "signed_out", exited: activity === "offline" };
}

/** The state a blocked session shows instead of Idle / Question / Offline. */
export function agentBlockedState(b: AgentBlock): SessionState {
  if (b.why === "missing") return agentMissingState(b.name);
  const who = agentName(b.name);
  return {
    state: "needs_you",
    label: "Not signed in",
    tone: "attention",
    inBell: true,
    restartable: false,
    hint: b.exited
      ? who + " isn't signed in, so its agent stopped — sign in, then it restarts"
      : who + " isn't signed in, so this session can't start on its task — finish signing in in its terminal",
  };
}

/** The row's sub-line for a blocked session. */
export function agentBlockedLine(b: AgentBlock): string {
  return b.why === "missing" ? "Failed: " + b.name + " isn't installed" : "Not signed in";
}
