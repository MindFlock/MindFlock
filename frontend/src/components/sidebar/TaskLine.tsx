import type { Instance } from "../../api/types";
import { firstLine } from "../../lib/format";

type TaskSource = Partial<
  Pick<Instance, "task_headline" | "task_headline_prompt" | "last_prompt" | "last_prompt_full" | "task">
>;

/** What a rail row says the session is working on, and what its hover shows.
 *
 * The text is the server's task headline — the newest MEANINGFUL prompt as a
 * short verb phrase ("Fix pin bar below terminal"; backend/task_headline.py),
 * which a "continue" or "yes" does not replace. An older server sends none:
 * then the newest prompt's first line, then the create task's. The tooltip is
 * what was actually typed for THAT headline (`task_headline_prompt`, the
 * prompt it was read from), so a later "continue" never becomes the hover of
 * a real task; the newest prompt stays on the pane's pinned bar. "" for no
 * task. */
export function taskLineOf(inst: TaskSource): { text: string; title: string } {
  const text = String(inst.task_headline || inst.last_prompt || firstLine(inst.task) || "").trim();
  const title = String(
    inst.task_headline_prompt || inst.last_prompt_full || inst.last_prompt || inst.task || text,
  ).trim();
  return { text, title };
}

/** The row's task line (the `.lineage.task-line` slot under the name). */
export function TaskLine({ inst }: { inst: TaskSource }) {
  const { text, title } = taskLineOf(inst);
  if (!text) return null;
  return (
    <span className="lineage task-line" title={title}>
      {text}
    </span>
  );
}
