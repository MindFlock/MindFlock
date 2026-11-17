/** Closing sessions from the sky, and putting them back (docs/flights.md
 * "Round 14" → "Undo"): shared by a line's own close (SkyLine) and the
 * selection bar's Remove (SkySelection). Plain functions, no component — so
 * the selection bar can be used outside the Sky (the pane's Route tab)
 * without pulling SkyLine and its sheets in ahead of their place in the
 * stylesheet. */

import type { Instance } from "../api/types";
import { queryClient } from "../state/queries";
import { undoLastClose } from "./sessionActions";

/** The session is still in the instance list (not closed, or reopened). */
export const sessionOpen = (title: string) => (queryClient.getQueryData<Instance[]>(["instances"]) || []).some((r) => r.title === title);

/** Put closed sessions back from Recently closed, the last closed first
 * (`undoLastClose`, the Ctrl+Shift+T road); one already open again is left be. */
export async function reopenClosed(titles: readonly string[]) {
  for (const t of [...titles].reverse()) if (!sessionOpen(t)) await undoLastClose();
}

/** What a running session is doing, for the question that names it. */
export const runningWord = (row: { activity?: string } | undefined) => (row?.activity === "working" ? "is working" : "is asking you something");
