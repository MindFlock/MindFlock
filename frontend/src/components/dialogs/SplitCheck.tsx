/** The New dialog's "Auto-split into up to N sessions if it's worth it" box,
 * under the describe box on page 1. Off by default.
 *
 * Ticking it makes the start an OPTIONAL split run (`POST /api/runs` with
 * `split: true, split_optional: true, max_pieces: N`): MindFlock creates one
 * session in its own worktree, and its agent decides. Not worth splitting →
 * it says so (`propose_run_plan` with no pieces), the group dissolves and that
 * session just does the task. Worth it → it proposes up to N pieces with
 * separate paths, the user approves the plan in its Thread tab, and the
 * server starts the workers, fences each to its paths and merges them back.
 * Nothing is pasted into an agent.
 *
 * The whole box is the task, however many lines it holds — the agent judges
 * the split, never the line breaks. It needs an agent that gets the MindFlock
 * tools (the lead answers with them). It never ticks itself: a sentence that
 * reads splittable gets a "suggested · …" pill and the choice stays the
 * user's.
 *
 * A server that cannot split at all gets no box: a greyed-out option whose only
 * message is "update your server" is noise to everyone who never asked for it.
 * The agent's reason still draws the box disabled, because the user can
 * change the agent. */

import { SERVER_NO_SPLIT } from "../../lib/laneActions";
import { splitSuggestion, suggestionPill } from "../../lib/playbooks";
import { SPLIT_MIN } from "../../lib/runStart";

export function SplitCheck({
  id,
  split,
  onSplit,
  gate,
  maxPieces,
  onMaxPieces,
  limit,
  text,
}: {
  id: string;
  split: boolean;
  onSplit(on: boolean): void;
  /** lib/playbooks.splitGate for the agent in the form. */
  gate: { ok: boolean; reason: string };
  /** N: the most sessions the work may be split into. */
  maxPieces: number;
  onMaxPieces(n: number): void;
  /** The server's cap on N (caps.team_runs.max_pieces). */
  limit: number;
  /** The words the pill reads. */
  text: string;
}) {
  if (!gate.ok && gate.reason === SERVER_NO_SPLIT) return null;
  const why = gate.ok ? "" : gate.reason;
  const ok = !why;
  const on = split && ok;
  const sug = ok ? splitSuggestion(text) : null;
  return (
    <div className={"nf-split" + (ok ? "" : " disabled")}>
      <label
        className={"check" + (ok ? "" : " disabled")}
        htmlFor={id}
        title={
          ok
            ? "Its agent reads the code first and decides. Not worth it: it just does the task. " +
              "Worth it: it proposes the pieces, each with its own paths — you approve, MindFlock " +
              "starts the workers and merges them back."
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
        Auto-split into up to
      </label>{" "}
      {/* Outside the <label>: a click on − or + must not toggle the box. */}
      <span className="rt-step nf-split-n" role="group" aria-label="Most sessions">
        <button
          type="button"
          aria-label="Fewer sessions"
          disabled={!ok || maxPieces <= SPLIT_MIN}
          onClick={() => onMaxPieces(maxPieces - 1)}
        >
          −
        </button>
        <span id={id + "-n"} aria-live="polite">
          {maxPieces}
        </span>
        <button
          type="button"
          aria-label="More sessions"
          disabled={!ok || maxPieces >= limit}
          onClick={() => onMaxPieces(maxPieces + 1)}
        >
          +
        </button>
      </span>{" "}
      <span className={ok ? "" : "muted"}>sessions if it's worth it</span>
      {sug && (
        <span
          className="nf-split-pill"
          title="Your sentence lists separate pieces. Nothing is ticked for you."
        >
          {suggestionPill(sug)}
        </span>
      )}
      {why && <span className="muted"> ({why})</span>}
      {/* What a tick does is said once, in the sentence under the choices
          (RunOptions' summary), not again here. */}
    </div>
  );
}
