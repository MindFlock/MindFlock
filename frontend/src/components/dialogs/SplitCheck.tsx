/** The New dialog's "Split a big line into parallel pieces first" box, under
 * the describe box on page 1.
 *
 * Ticking it makes the start a SPLIT run (`POST /api/runs` with
 * `split: true`): MindFlock creates a lead session in its own worktree, the
 * lead proposes the pieces with separate paths, the user approves the plan in
 * its Thread tab, and the server starts the workers, fences each to its
 * paths and merges them back. Nothing is pasted into an agent.
 *
 * It applies to exactly one task line — a list is already parallel — and to
 * an agent that gets the MindFlock tools (the lead proposes the plan with
 * them). It never ticks itself: a sentence that reads splittable gets a
 * "suggested · …" pill and the choice stays the user's.
 *
 * A server that cannot split at all gets no box: a greyed-out option whose only
 * message is "update your server" is noise to everyone who never asked for it.
 * Every other reason (the agent, the shape of the box) still draws the box
 * disabled with that reason, because those are things the user can change. */

import { SERVER_NO_SPLIT } from "../../lib/laneActions";
import { splitSuggestion, suggestionPill } from "../../lib/playbooks";

export function SplitCheck({
  id,
  split,
  onSplit,
  gate,
  shapeReason,
  text,
}: {
  id: string;
  split: boolean;
  onSplit(on: boolean): void;
  /** lib/playbooks.splitGate for the agent in the form. */
  gate: { ok: boolean; reason: string };
  /** Why what is in the box can't be split ("" = it can). */
  shapeReason: string;
  /** The words the pill reads. */
  text: string;
}) {
  if (!gate.ok && gate.reason === SERVER_NO_SPLIT) return null;
  const why = gate.ok ? shapeReason : gate.reason;
  const ok = !why;
  const on = split && ok;
  const sug = ok ? splitSuggestion(text) : null;
  return (
    <div className={"nf-split" + (ok ? "" : " disabled")}>
      <label
        className={"check" + (ok ? "" : " disabled")}
        title={
          ok
            ? "MindFlock starts a lead session that proposes the pieces, each with its own paths. " +
              "You approve the split; MindFlock starts the workers and merges them back."
            : why
        }
      >
        <input
          type="checkbox"
          id={id}
          checked={on}
          disabled={!ok}
          onChange={(e) => onSplit(e.target.checked)}
        />
        Split a big line into parallel pieces first
        {sug && (
          <span
            className="nf-split-pill"
            title="Your sentence lists separate pieces. Nothing is ticked for you."
          >
            {suggestionPill(sug)}
          </span>
        )}
        {why && <span className="muted"> ({why})</span>}
      </label>
      {/* What a tick does is said once, in the sentence under the choices
          (RunOptions' summary), not again here. */}
    </div>
  );
}
