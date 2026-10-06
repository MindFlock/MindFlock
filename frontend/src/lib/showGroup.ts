/** Show a group of sessions started together, wherever it lives now — the
 * click of a run toast or a bell history row that names no session.
 *
 * Its header on the rail first (lib/revealGroup.ts: scrolled to and pulsed).
 * A group with no header — a split or one-for-all group (a family under its
 * lead), a group of one, a finished group whose rows were closed — falls back
 * to the lead's Thread tab, else to a member row (lib/runs.ts `groupLanding`).
 * With nothing of the group left on the rail it does nothing: the caller has
 * already closed whatever it came from. */

import type { Instance } from "../api/types";
import { queryClient } from "../state/queries";
import { groupLanding, type RunInfo } from "./runs";
import { revealGroup } from "./revealGroup";
import { openThread } from "./flockActions";
import { selectSession } from "./sessionActions";

export function showGroup(runId: string | null | undefined): void {
  if (!runId || revealGroup(runId)) return;
  const to = groupLanding(
    runId,
    queryClient.getQueryData<RunInfo[] | null>(["runs"]),
    queryClient.getQueryData<Instance[]>(["instances"])
  );
  if (!to) return;
  if ("lead" in to) openThread(to.lead);
  else selectSession(to.row);
}
