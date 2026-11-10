/** The pane's verify row (under its head, beside the route's strip): the
 * session's own agent checking its own work, one obvious step from the pane.
 *
 *  - Verifying: which step it is on ("Step 4 of 5: <text> · quiet 3 min"),
 *    or "Waiting for <session>'s turn to end" while queued; Steps and Stop
 *    verifying.
 *  - Needs you: the person's steps and Confirm verified / Not verified… (or
 *    Sign off when it has none).
 *  - Failed: what failed, "Let its agent fix it" (the session's own agent
 *    fixes it and verifies again), Verify again, Dismiss.
 *  - Blocked: why, Verify again, Dismiss.
 *  - Passed: "All 5 steps passed · Steps" for its ten minutes on the row.
 *
 * "Verify again" (and the ⋯ menu's Verify) opens the start form IN this row
 * (VerifyStartForm) — never a modal over the terminal. The first line keeps
 * one height in every state, so the terminal under it doesn't jump. Which
 * line and which buttons is `verifyStripView` (pure, pinned in tests). */

import { useEffect, useRef, useState, type JSX } from "react";
import type { Instance, SessionVerify } from "../../api/types";
import { openBehaviors } from "../../lib/behaviorsFocus";
import { verifyGlyph, verifyLine, verifyOf } from "../../lib/sessionState";
import { dismissVerify, fixInSession, openWalkingSession, stopVerifying } from "../../lib/verifyActions";
import { FailedSteps, VerifyConfirm } from "../behaviors/VerifyConfirm";
import { closeVerifyForm, markVerifyRowMounted, openVerifyForm, useVerifyForm, VerifyStartForm } from "../behaviors/VerifyStartForm";
import "./PaneVerifyStrip.css";

export type StripActionKey = "fix" | "again" | "stop" | "dismiss" | "steps" | "agent";

export interface StripAction {
  key: StripActionKey;
  label: string;
  title: string;
  primary?: boolean;
}

export interface StripView {
  /** lib/sessionState verifyGlyph: ✓ passed, ✗ failed, ! blocked, ? needs
   * you, … verifying. */
  glyph: string;
  line: string;
  /** The results file couldn't be read — one sentence, or "". */
  fileError: string;
  actions: StripAction[];
}

/** Quiet this long (s) before the line says so. */
export const QUIET_AFTER_S = 120;

/** "3 min", "1 h 30 min" — how long the agent has been quiet. */
export function quietWord(seconds: number): string {
  const m = Math.max(0, Math.floor(seconds / 60));
  if (m < 60) return m + " min";
  return Math.floor(m / 60) + " h" + (m % 60 ? " " + (m % 60) + " min" : "");
}

const A: Record<StripActionKey, Omit<StripAction, "key">> = {
  fix: { label: "Let its agent fix it", title: "Its agent gets the failed steps and what it saw, fixes them, then verifies again" },
  again: { label: "Verify again", title: "Its agent checks its work again — say what it should prove first, if you like" },
  stop: { label: "Stop verifying", title: "Its agent stops working the steps — the session keeps running" },
  dismiss: { label: "Dismiss", title: "Take this off the row — you've seen it" },
  steps: { label: "Steps", title: "The steps and their evidence" },
  agent: { label: "Open its agent", title: "The agent's terminal — where it works the steps and says what it did" },
};

const act = (key: StripActionKey, primary = false): StripAction => ({ key, ...A[key], ...(primary ? { primary: true } : {}) });

/** The line under way: "Step 4 of 5: <text> · quiet 3 min", "Waiting for
 * <session>'s turn to end", or the plain progress from an older server. */
function verifyingLine(v: SessionVerify, session: string, nowS: number): string {
  if (v.queued) return "Waiting for " + session + "'s turn to end";
  const total = Number(v.steps?.total) || 0;
  const human = Number(v.steps?.human) || 0;
  const mine = Math.max(0, total - human);
  const done = Number(v.steps?.agent_done) || 0;
  const text = String(v.current?.text || "").trim();
  let line: string;
  if (text && mine) line = "Step " + Math.min(done + 1, mine) + " of " + mine + ": " + text;
  else if (text) line = "Verifying: " + text;
  else line = verifyLine(v);
  const at = Number(v.progress_at) || 0;
  const quiet = at > 0 ? nowS - at : 0;
  if (quiet >= QUIET_AFTER_S) line += " · quiet " + quietWord(quiet);
  return line;
}

/** What the row says and offers for a verify (pure). `nowS` is epoch seconds. */
export function verifyStripView(v: SessionVerify, session: string, nowS: number = Date.now() / 1000): StripView {
  const fileError = String(v.file_error || "").trim();
  const detail = String(v.detail || "").trim();
  switch (v.state) {
    case "verifying":
      return { glyph: verifyGlyph(v.state), line: verifyingLine(v, session, nowS), fileError, actions: [act("steps"), act("agent"), act("stop")] };
    case "needs_you": {
      const n = (v.human_steps || []).length || Number(v.steps?.human) || 0;
      const line = n
        ? "Needs you — the agent did its steps; do " + (n === 1 ? "yours" : "your " + n) + ", then confirm"
        : "Needs you — sign off on what the agent verified";
      return { glyph: verifyGlyph(v.state), line, fileError, actions: [act("steps"), act("agent")] };
    }
    case "failed":
      return { glyph: verifyGlyph(v.state), line: verifyLine(v), fileError, actions: [act("fix", true), act("again"), act("steps"), act("dismiss")] };
    case "blocked":
      return { glyph: verifyGlyph(v.state), line: verifyLine(v), fileError, actions: [act("again", true), act("steps"), act("agent"), act("dismiss")] };
    case "passed": {
      const total = Number(v.steps?.total) || 0;
      const line = total ? "All " + total + (total === 1 ? " step" : " steps") + " passed" : "Verified" + (detail ? " — " + detail : "");
      return { glyph: verifyGlyph(v.state), line, fileError, actions: [act("steps")] };
    }
  }
  return { glyph: verifyGlyph(v.state), line: "", fileError, actions: [] };
}

export function PaneVerifyStrip({ inst }: { inst: Pick<Instance, "title" | "verify"> & Partial<Pick<Instance, "repo">> }): JSX.Element | null {
  const title = inst.title;
  const v = verifyOf(inst);
  const formOpen = useVerifyForm((s) => s.open === title);
  const [busy, setBusy] = useState<StripActionKey | "">("");
  const rowRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => markVerifyRowMounted(title), [title]);
  // Under way, the quiet clock has to move without a new row from the server.
  const [, tick] = useState(0);
  const verifying = v?.state === "verifying" && !!v.progress_at;
  useEffect(() => {
    if (!verifying) return;
    const t = setInterval(() => tick((n) => n + 1), 30_000);
    return () => clearInterval(t);
  }, [verifying]);

  if (!v && !formOpen) return null;
  const firstButton = () => rowRef.current?.querySelector<HTMLElement>("button") || null;
  const close = () => closeVerifyForm(firstButton());
  if (!v)
    return (
      <div className="pv-strip is-form" role="group" aria-label="Verify" data-no-drag onMouseDown={(e) => e.stopPropagation()}>
        <VerifyStartForm title={title} repo={inst.repo} onClose={close} />
      </div>
    );

  const view = verifyStripView(v, title);
  const run = (key: StripActionKey, el: HTMLElement) => {
    if (busy) return;
    const go = (p: Promise<unknown>) => {
      setBusy(key);
      void p.finally(() => setBusy(""));
    };
    if (key === "again") openVerifyForm(title, el);
    else if (key === "steps") openBehaviors({ planId: v.plan, title });
    else if (key === "agent") openWalkingSession(title);
    else if (key === "stop") go(stopVerifying(v.plan, title));
    else if (key === "dismiss") go(dismissVerify(v.plan, title));
    else if (key === "fix") go(fixInSession(v.plan, title));
  };
  return (
    <div className={"pv-strip is-" + v.state} role="group" aria-label="Verify" data-no-drag onMouseDown={(e) => e.stopPropagation()}>
      <div className="pv-row" ref={rowRef}>
        <span className={"pv-glyph" + (v.state === "verifying" ? " is-live" : "")} aria-hidden="true">
          {view.glyph}
        </span>
        <span className="pv-text" title={view.line} aria-live="polite">
          {view.line}
        </span>
        {view.actions.map((a) => (
          <button
            key={a.key}
            type="button"
            className={(a.primary ? "btn-primary" : "test-btn") + " pv-btn pv-" + a.key}
            title={a.title}
            disabled={!!busy || (a.key === "again" && formOpen)}
            aria-busy={busy === a.key || undefined}
            aria-expanded={a.key === "again" ? formOpen : undefined}
            onClick={(e) => run(a.key, e.currentTarget)}
          >
            {a.label}
          </button>
        ))}
      </div>
      {view.fileError ? (
        <p className="pv-file-error" role="note">
          {view.fileError}
        </p>
      ) : null}
      {formOpen ? <VerifyStartForm title={title} repo={inst.repo} onClose={close} /> : null}
      {v.state === "needs_you" ? <VerifyConfirm verify={v} session={title} /> : null}
      {v.state === "failed" ? <FailedSteps steps={v.failed_steps || []} /> : null}
    </div>
  );
}
