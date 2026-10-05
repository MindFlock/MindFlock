/** The New dialog's "Split across workers" box. It appears twice and shares
 * one `split` state: under the sentence on the Describe page, and under Plan
 * first in the form's Prompt fold.
 *
 * Ticking it makes the create send `playbook: "split"`. The server then adds
 * the split playbook to the launch prompt, and the agent uses its own
 * MindFlock tools to fork one worker per independent piece. It never ticks
 * itself. When the sentence looks splittable it shows a "suggested · …" pill
 * and leaves the choice to the user. Closed (with the reason) when the agent
 * in the form doesn't get the MindFlock tools. */

import { splitSuggestion, suggestionPill } from "../../lib/playbooks";

export function SplitCheck({
  id,
  split,
  onSplit,
  gate,
  text,
}: {
  id: string;
  split: boolean;
  onSplit(on: boolean): void;
  /** lib/playbooks.splitGate for the agent in the form. */
  gate: { ok: boolean; reason: string };
  /** The words the pill reads: the sentence, or the prompt. */
  text: string;
}) {
  const on = split && gate.ok;
  const sug = gate.ok ? splitSuggestion(text) : null;
  return (
    <div className={"nf-split" + (gate.ok ? "" : " disabled")}>
      <label
        className={"check" + (gate.ok ? "" : " disabled")}
        title={
          gate.ok
            ? "The agent forks one worker session per independent piece of the task, waits for " +
              "their reports, then merges them. Works with the CLIs that get the MindFlock tools."
            : gate.reason
        }
      >
        <input
          type="checkbox"
          id={id}
          checked={on}
          disabled={!gate.ok}
          onChange={(e) => onSplit(e.target.checked)}
        />
        Split across workers
        {sug && (
          <span className="nf-split-pill" title="Your sentence lists separate pieces. Nothing is ticked for you.">
            {suggestionPill(sug)}
          </span>
        )}
        {!gate.ok && <span className="muted"> ({gate.reason})</span>}
      </label>
      {on && (
        <p className="nf-git-nudge nf-split-nudge">
          The agent commits shared groundwork, starts one worker session per independent piece,
          waits for their reports, then merges. Workers appear under it in the rail. Runs in a new
          worktree. Each spawn asks your permission unless this agent skips permissions — answer
          from the rail.
        </p>
      )}
    </div>
  );
}
