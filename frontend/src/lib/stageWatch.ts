/** One-shot fresh read of a single session's row, merged into the query cache.
 *
 * Why this exists rather than another `refreshInstances()`: the server serves
 * GET /api/instances from its published tick snapshot for up to 10s and
 * deliberately never rebuilds the expensive probes inline, so a post-action
 * invalidate provably returns the same stale row — the old
 * `setTimeout(refreshInstances, 1000)` after a commit could not observe anything
 * and merely reset the 4s poll timer, delaying the first useful poll.
 * `/api/instances/{title}/stage` recomputes ONE session and publishes it through.
 *
 * There is deliberately NO client-side fast-poll loop here. Completion is covered
 * server-side by the `live_stage` edge watcher plus the `session.stage_changed`
 * cache patch, so the client needs exactly one call: at press time.
 */

import { instApi } from "../api/client";
import type { Instance } from "../api/types";
import { patchInstance } from "../state/queries";

/** In-flight reads per title, so overlapping callers share one request. */
const inflight = new Map<string, Promise<Instance | null>>();

export function freshStage(title: string): Promise<Instance | null> {
  if (!title) return Promise.resolve(null);
  const live = inflight.get(title);
  if (live) return live;
  const p = instApi<Instance>(title, "/stage")
    .then((row) => {
      if (row && row.title) patchInstance(title, row);
      return row ?? null;
    })
    // A 404 (session deleted mid-action) or 409 (workspace not ready) must never
    // break the caller — this is a freshness nicety layered on a completed action.
    .catch(() => null)
    .finally(() => {
      inflight.delete(title);
    });
  inflight.set(title, p);
  return p;
}
