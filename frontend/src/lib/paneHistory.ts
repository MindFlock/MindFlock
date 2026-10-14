/** A pane's whole history (its full tmux scrollback, or the provider
 * transcript for an alt-screen TUI) as plain text, and the one way to put it
 * on the clipboard. Shared by the History overlay (its load and **Copy all**)
 * and the pane header's copy button / ⋯ **Copy entire history**, so both copy
 * the same text and say the same thing. */

import { apiText } from "../api/client";
import { agentCopyText } from "./agentCopy";
import { copyText } from "./clipboard";
import { peekTerm } from "./terminals";
import { toast } from "./toast";

export type HistoryPane = "agent" | "shell";

/** GET /api/instances/{title}/history?pane=… — the pane's full scrollback. */
export function fetchPaneHistory(title: string, pane: HistoryPane): Promise<string> {
  return apiText(`/api/instances/${encodeURIComponent(title)}/history?pane=${pane}`);
}

/** The text as it should land on the clipboard: an agent pane running Claude
 * Code copies without its markers, gutter and hard wraps (lib/agentCopy);
 * a shell copies verbatim. */
export function historyForClipboard(text: string, title: string, pane: HistoryPane): string {
  return pane === "agent" ? agentCopyText(text, title, peekTerm(title, "agent")?.term.cols || 80) : text;
}

/** Copy the pane's whole history and toast the outcome: "Copied full agent
 * history (N chars)", "No history to copy", "Copy failed", or why the fetch
 * failed. `loaded` is history already in hand (the overlay's), so it is not
 * fetched twice. Resolves whether anything was copied. */
export function copyPaneHistory(title: string, pane: HistoryPane, loaded?: string | null): Promise<boolean> {
  const got = loaded != null ? Promise.resolve(loaded) : fetchPaneHistory(title, pane);
  return got
    .then((all) => {
      if (!all.trim()) {
        toast("No history to copy");
        return false;
      }
      return copyText(historyForClipboard(all, title, pane)).then((ok) => {
        toast(ok ? `Copied full ${pane} history (${all.length} chars)` : "Copy failed");
        return ok;
      });
    })
    .catch((err) => {
      toast("History copy failed: " + (err as Error).message);
      return false;
    });
}
