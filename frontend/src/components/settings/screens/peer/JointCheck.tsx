/** The joint checklist's slot on an active joint card: "do the halves
 * agree?". Renders `joint.check` when the server has one (wave 2 writes it,
 * runs each side's steps and adds Write / Ask / answer controls here); renders
 * nothing until then. Read-only on purpose: it never gates anything. */

import { jointVerdictText, type JointCheckData, type JointCheckResult } from "../../../../lib/peer";

const RESULT_TEXT: Record<string, string> = { pass: "Pass", fail: "Fail", blocked: "Can't check", "": "Not yet" };

function cell(r: JointCheckResult | undefined): string {
  return RESULT_TEXT[r?.result || ""] ?? "Not yet";
}

export function JointCheck({ check, peerName }: { check?: JointCheckData | null; peerName: string }) {
  if (!check) return null;
  const steps = check.steps || [];
  return (
    <div className="peer-joint-check" data-verdict={check.verdict}>
      <div className="peer-joint-check-head">
        <span className="set-label">Joint checklist</span>
        <span className={"peer-verdict peer-verdict-" + check.verdict}>{jointVerdictText(check.verdict)}</span>
      </div>
      {steps.length ? (
        <ol className="peer-joint-steps">
          {steps.map((s) => {
            const side = s.side === "theirs" ? check.theirs : check.mine;
            const r = side?.results?.[s.id];
            return (
              <li key={s.id} className={"peer-joint-step peer-joint-step-" + (r?.result || "blank")}>
                <span className="peer-joint-step-who">
                  {s.side === "theirs" ? peerName : "You"} · {s.actor === "human" ? "person" : "agent"}
                </span>
                <span className="peer-joint-step-text">{s.text}</span>
                <span className="peer-joint-step-result">{cell(r)}</span>
              </li>
            );
          })}
        </ol>
      ) : (
        <p className="set-hint">No steps yet.</p>
      )}
    </div>
  );
}
