/** The pane's verify row (under its head, beside the route's strip): the
 * session's own agent checking its work. While it works, one line — how far
 * it is ("Verifying — 3 of 5 steps done") and a way to its Agent tab, where
 * it works the steps and says what it did. When a person's steps are left,
 * those steps and Confirm verified / Not verified…. Failed or blocked: what
 * failed, and Verify again. Passed says nothing here — the pill does. */

import type { JSX } from "react";
import type { Instance } from "../../api/types";
import { verifyLine, verifyOf } from "../../lib/sessionState";
import { openWalkingSession, startSessionVerify } from "../../lib/verifyActions";
import { FailedSteps, VerifyConfirm } from "../behaviors/VerifyConfirm";
import "./PaneVerifyStrip.css";

export function PaneVerifyStrip({ inst }: { inst: Pick<Instance, "title" | "verify"> }): JSX.Element | null {
  const v = verifyOf(inst);
  if (!v || v.state === "passed") return null;
  const title = inst.title;
  const done = v.state === "failed" || v.state === "blocked";
  return (
    <div className={"pv-strip is-" + v.state} role="group" aria-label="Verify" data-no-drag onMouseDown={(e) => e.stopPropagation()}>
      <div className="pv-row">
        <span className="pv-shield" aria-hidden="true">
          ✓
        </span>
        <span className="pv-text" title={verifyLine(v)}>
          {verifyLine(v)}
        </span>
        <button type="button" className="tb-item pv-open" title="The agent's terminal — where it works the steps and says what it did" onClick={() => openWalkingSession(title)}>
          Open its agent
        </button>
        {done ? (
          <button type="button" className="test-btn pv-again" title="Its agent checks its work again" onClick={() => void startSessionVerify(title)}>
            Verify again
          </button>
        ) : null}
      </div>
      {v.state === "needs_you" ? <VerifyConfirm verify={v} /> : null}
      {v.state === "failed" ? <FailedSteps steps={v.failed_steps || []} /> : null}
    </div>
  );
}
