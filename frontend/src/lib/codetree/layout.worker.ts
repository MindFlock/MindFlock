/** The layout worker: runs the tree search off the UI thread and posts back
 * the placements (a LayoutRecord); the page replays them (fast) to build its
 * own model with canvas geometry. Messages:
 *   in : { id, raw: RawRepo, warm: LayoutRecord | null }
 *   out: { id, progress } … then { id, rec, ms, stats } or { id, error } */

import { buildModel, type LayoutRecord, type RawRepo } from "./model";

const post = (m: unknown) => (self as unknown as { postMessage(m: unknown): void }).postMessage(m);

self.onmessage = (e: MessageEvent<{ id: number; raw: RawRepo; warm: LayoutRecord | null }>) => {
  const { id, raw, warm } = e.data;
  let last = 0;
  try {
    const t0 = performance.now();
    const M = buildModel(raw, {
      quantise: true,
      warm,
      tick: (k) => {
        const now = performance.now();
        if (now - last > 90) {
          last = now;
          post({ id, progress: k });
        }
      },
    });
    post({ id, rec: M.layoutRec, ms: Math.round(performance.now() - t0), stats: M.buildStats });
  } catch (err) {
    post({ id, error: String((err as Error)?.message || err) });
  }
};
