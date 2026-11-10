/** The steps only a person can do, and the person's answer: "Confirm
 * verified" or "Not verified…" (with a note sent to the session's agent).
 * Drawn wherever a verify waits on a person — the pane's verify row, the
 * Behaviors card, the bell, a Verify stop parked on you.
 *
 *  - More than one step: each can be marked ✓ / ✗ (all ✓ to start) and the
 *    marks are sent per step; one ✗ needs a note saying what you saw.
 *  - A step the AGENT tried and couldn't do says why under it.
 *  - No person steps at all (a plain sign-off): the heading is the server's
 *    own `detail` and the button reads "Sign off".
 *  - `hideSteps`: the caller already lists the steps — they are not drawn
 *    twice (and so carry no per-step marks here).
 *
 * The note is an inline input, never `window.prompt()`: Electron has none —
 * prompt()/alert()/confirm() silently do nothing in the desktop app. Which
 * heading, which button and which body is `confirmView` / `confirmSteps`
 * (pure, pinned in tests). */

import { useState, type JSX } from "react";
import type { SessionVerify, SessionVerifyFailedStep, SessionVerifyHumanStep, SessionVerifyStepAnswer } from "../../api/types";
import { confirmVerified } from "../../lib/verifyActions";
import "./BehaviorsCard.css";

export type StepMarks = Record<string, SessionVerifyStepAnswer>;

/** The person's steps, numbered, each with what should happen — and, for a
 * step the agent couldn't do, why. `marks`/`onMark` add a ✓ / ✗ per step. */
export function HumanSteps({
  steps,
  marks,
  onMark,
  disabled,
}: {
  steps: readonly SessionVerifyHumanStep[];
  marks?: StepMarks;
  onMark?(id: string, answer: SessionVerifyStepAnswer): void;
  disabled?: boolean;
}): JSX.Element | null {
  if (!steps.length) return null;
  return (
    <ol className="vc-steps" aria-label="Steps for you">
      {steps.map((st) => {
        const mark = marks?.[st.id] || "pass";
        return (
          <li key={st.id} className={"vc-step" + (onMark && mark === "fail" ? " is-fail" : "")}>
            <span className="vc-step-text">{st.text}</span>
            {st.expect ? <span className="vc-step-expect">Expect: {st.expect}</span> : null}
            {st.why ? <span className="vc-step-why">The agent couldn't do this: {st.why}</span> : null}
            {onMark ? (
              <span className="vc-step-mark" role="group" aria-label={"Did it behave? " + st.text}>
                <button
                  type="button"
                  className="test-btn vc-mark-pass"
                  aria-pressed={mark === "pass"}
                  title="It behaved as expected"
                  disabled={disabled}
                  onClick={() => onMark(st.id, "pass")}
                >
                  ✓
                </button>
                <button
                  type="button"
                  className="test-btn vc-mark-fail"
                  aria-pressed={mark === "fail"}
                  title="It didn't behave — say what you saw below"
                  disabled={disabled}
                  onClick={() => onMark(st.id, "fail")}
                >
                  ✗
                </button>
              </span>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}

/** What failed, each with the agent's note on what it saw. */
export function FailedSteps({ steps }: { steps: readonly SessionVerifyFailedStep[] }): JSX.Element | null {
  if (!steps.length) return null;
  return (
    <ol className="vc-steps vc-failed" aria-label="What failed">
      {steps.map((st) => (
        <li key={st.id} className="vc-step">
          <span className="vc-step-text">{st.text}</span>
          {st.note ? <span className="vc-step-expect">{st.note}</span> : null}
        </li>
      ))}
    </ol>
  );
}

export interface ConfirmView {
  /** A plain sign-off: no step is the person's. */
  signOff: boolean;
  /** The line above the buttons ("" = none; the caller says it). */
  heading: string;
  yesLabel: string;
  /** ✓ / ✗ per step. */
  perStep: boolean;
  /** At least one step is marked ✗. */
  anyFail: boolean;
}

/** Which heading, button and marks a confirm shows (pure). */
export function confirmView(
  verify: Pick<SessionVerify, "human_steps"> & Partial<Pick<SessionVerify, "detail">>,
  marks: StepMarks = {},
  hideSteps = false,
): ConfirmView {
  const steps = verify.human_steps || [];
  const signOff = steps.length === 0;
  const perStep = !hideSteps && steps.length > 1;
  return {
    signOff,
    heading: signOff ? String(verify.detail || "").trim() || "The agent verified its work — sign off if it's what you wanted" : "",
    yesLabel: signOff ? "Sign off" : "Confirm verified",
    perStep,
    anyFail: perStep && steps.some((s) => marks[s.id] === "fail"),
  };
}

/** The per-step answers to send, or null for none (pure): every step on a
 * confirm; on Not verified only when a step is marked ✗ (all ✓ with "not
 * verified" would contradict itself). */
export function confirmSteps(steps: readonly SessionVerifyHumanStep[], marks: StepMarks, perStep: boolean, verified: boolean): StepMarks | null {
  if (!perStep) return null;
  const out: StepMarks = {};
  for (const s of steps) out[s.id] = verified ? "pass" : marks[s.id] || "pass";
  if (!verified && !Object.values(out).includes("fail")) return null;
  return out;
}

export interface VerifyConfirmProps {
  verify: Pick<SessionVerify, "plan" | "human_steps"> & Partial<Pick<SessionVerify, "detail">>;
  /** The session, for "Sent to <session>'s agent" (the plan id otherwise). */
  session?: string;
  /** Leave the steps out (the caller already lists them). */
  hideSteps?: boolean;
}

export function VerifyConfirm({ verify, session, hideSteps }: VerifyConfirmProps): JSX.Element {
  const [busy, setBusy] = useState<"" | "yes" | "no">("");
  const [asking, setAsking] = useState(false);
  const [note, setNote] = useState("");
  const [marks, setMarks] = useState<StepMarks>({});
  const steps = verify.human_steps || [];
  const view = confirmView(verify, marks, hideSteps);
  // One ✗ asks what you saw, without a second press.
  const noteOpen = asking || view.anyFail;
  const noteMissing = view.anyFail && !note.trim();
  const send = async (verified: boolean) => {
    if (busy || (!verified && noteMissing)) return;
    setBusy(verified ? "yes" : "no");
    try {
      const ok = await confirmVerified(verify.plan, verified, verified ? "" : note, {
        steps: confirmSteps(steps, marks, view.perStep, verified),
        session,
      });
      if (ok) {
        setAsking(false);
        setNote("");
        setMarks({});
      }
    } finally {
      setBusy("");
    }
  };
  const closeNote = () => {
    setAsking(false);
    if (view.anyFail) setMarks({});
  };
  return (
    <div className="vc-confirm" role="group" aria-label={view.signOff ? "Sign off" : "Confirm verified"} data-no-drag onMouseDown={(e) => e.stopPropagation()}>
      {view.heading ? <p className="vc-heading">{view.heading}</p> : null}
      {hideSteps ? null : (
        <HumanSteps
          steps={steps}
          marks={view.perStep ? marks : undefined}
          onMark={view.perStep ? (id, a) => setMarks((m) => ({ ...m, [id]: a })) : undefined}
          disabled={!!busy}
        />
      )}
      {noteOpen ? (
        <div className="vc-note-row">
          <input
            type="text"
            className="fl-input vc-note"
            placeholder={view.anyFail ? "What did you see? (needed for a ✗)" : "What did you see?"}
            aria-label="What wasn't verified"
            aria-required={view.anyFail || undefined}
            autoComplete="off"
            autoFocus={asking}
            value={note}
            disabled={!!busy}
            onChange={(e) => setNote(e.target.value)}
            onKeyDown={(e) => {
              e.stopPropagation();
              if (e.key === "Enter") {
                e.preventDefault();
                void send(false);
              } else if (e.key === "Escape") {
                e.preventDefault();
                closeNote();
              }
            }}
          />
          <button
            type="button"
            className="btn-primary vc-send"
            disabled={!!busy || noteMissing}
            aria-busy={busy === "no" || undefined}
            title={noteMissing ? "Say what you saw first" : "Send what you saw to the session's agent"}
            onClick={() => void send(false)}
          >
            Not verified
          </button>
          <button type="button" className="test-btn" disabled={!!busy} onClick={closeNote}>
            Cancel
          </button>
        </div>
      ) : (
        <div className="vc-acts">
          <button
            type="button"
            className="btn-primary vc-yes"
            title={
              view.signOff
                ? "You're satisfied with what the agent verified — verification is complete"
                : "You did the steps above and they behaved as expected — verification is complete"
            }
            disabled={!!busy}
            aria-busy={busy === "yes" || undefined}
            onClick={() => void send(true)}
          >
            {view.yesLabel}
          </button>
          <button type="button" className="test-btn vc-no" title="Something didn't behave — say what you saw" disabled={!!busy} onClick={() => setAsking(true)}>
            Not verified…
          </button>
        </div>
      )}
    </div>
  );
}
