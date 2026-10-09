/** Which copies get {@link cleanClaudeSelection}: text copied out of an
 * AGENT pane whose session runs Claude Code (or a custom provider built on
 * it). Shell tabs and other agents copy verbatim. */

import type { Instance } from "../api/types";
import { queryClient } from "../state/queries";
import { cleanClaudeSelection } from "./claudeCopy";

export function runsClaudeCode(session?: string): boolean {
  if (!session) return false;
  try {
    const inst = (queryClient.getQueryData<Instance[]>(["instances"]) || []).find(
      (i) => i.title === session
    );
    // Both fields: a custom provider that launches Claude Code resolves to
    // its own name (or "generic"), and only its program says "claude".
    return `${inst?.provider || ""} ${inst?.program || ""}`.toLowerCase().includes("claude");
  } catch {
    return false;
  }
}

/** ``text`` as it should land on the clipboard when copied from ``session``'s
 * agent pane drawn ``cols`` wide (``startCol``: where the selection starts on
 * its first row). */
export function agentCopyText(text: string, session: string | undefined, cols: number, startCol = 0): string {
  return text && runsClaudeCode(session) ? cleanClaudeSelection(text, cols, startCol) : text;
}
