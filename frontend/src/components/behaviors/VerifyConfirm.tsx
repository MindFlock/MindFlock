/** The steps only a person can do, and the person's one answer: "Confirm
 * verified" or "Not verified…" (with a note the agent gets). Drawn wherever a
 * verify waits on a person — the pane's verify row, the Behaviors card, a
 * Verify stop parked on you.
 *
 * The note is an inline input, never `window.prompt()`: Electron has none —
 * prompt()/alert()/confirm() silently do nothing in the desktop app. */

import { useState, type JSX } from "react";
import type { SessionVerify, SessionVerifyFailedStep, SessionVerifyHumanStep } from "../../api/types";
import { confirmVerified } from "../../lib/verifyActions";
import "./BehaviorsCard.css";

/** The person's steps, numbered, each with what should happen. */
export function HumanSteps({ steps }: { steps: readonly SessionVerifyHumanStep[] }): JSX.Element | null {
  if (!steps.length) return null;
  return (
    <ol className="vc-steps" aria-label="Steps for you">
      {steps.map((st) => (
        <li key={st.id} className="vc-step">
          <span className="vc-step-text">{st.text}</span>
          {st.expect ? <span className="vc-step-expect">Expect: {st.expect}</span> : null}
        </li>
      ))}
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

export interface VerifyConfirmProps {
  verify: Pick<SessionVerify, "plan" | "human_steps">;
  /** Leave the steps out (the caller already lists them). */
  hideSteps?: boolean;
}

export function VerifyConfirm({ verify, hideSteps }: VerifyConfirmProps): JSX.Element {
  const [busy, setBusy] = useState<"" | "yes" | "no">("");
  const [asking, setAsking] = useState(false);
  const [note, setNote] = useState("");
  const send = async (verified: boolean) => {
    if (busy) return;
    setBusy(verified ? "yes" : "no");
    try {
      if (await confirmVerified(verify.plan, verified, verified ? "" : note)) {
        setAsking(false);
        setNote("");
      }
    } finally {
      setBusy("");
    }
  };
  return (
    <div className="vc-confirm" role="group" aria-label="Confirm verified" data-no-drag onMouseDown={(e) => e.stopPropagation()}>
      {hideSteps ? null : <HumanSteps steps={verify.human_steps || []} />}
      {asking ? (
        <div className="vc-note-row">
          <input
            type="text"
            className="fl-input vc-note"
            placeholder="What did you see? The agent gets this"
            aria-label="What wasn't verified"
            autoComplete="off"
            autoFocus
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
                setAsking(false);
              }
            }}
          />
          <button type="button" className="btn-primary vc-send" disabled={!!busy} onClick={() => void send(false)}>
            {busy === "no" ? "…" : "Not verified"}
          </button>
          <button type="button" className="test-btn" disabled={!!busy} onClick={() => setAsking(false)}>
            Cancel
          </button>
        </div>
      ) : (
        <div className="vc-acts">
          <button
            type="button"
            className="btn-primary vc-yes"
            title="You did the steps above and they behaved as expected — verification is complete"
            disabled={!!busy}
            onClick={() => void send(true)}
          >
            {busy === "yes" ? "…" : "Confirm verified"}
          </button>
          <button type="button" className="test-btn vc-no" title="Something didn't behave — say what, and the agent gets it" disabled={!!busy} onClick={() => setAsking(true)}>
            Not verified…
          </button>
        </div>
      )}
    </div>
  );
}
