/** Code tree — getting a laid-out model for a snapshot without ever blocking
 * the UI on the search:
 *   1. memory (module-level, survives tab switches and pane remounts);
 *   2. an IndexedDB record for this exact data → replay (milliseconds);
 *   3. the layout worker, warm-started from the repo's newest record (a small
 *      change keeps the tree where it stood), else cold;
 *   4. no Worker (or it failed) → the same build on the main thread, after a
 *      paint, so the "Growing the tree…" state shows first.
 * The page then replays the worker's placements to build its own model. */

import { loadRecords, saveRecord } from "./cache";
import { buildModel, dataSig, type LayoutRecord, type Model, type RawRepo } from "./model";

/** After this many warm builds in a row, lay out cold again (re-compact). */
export const WARM_RUN_MAX = 25;
const MODELS_MAX = 8;

const MODELS = new Map<string, Model>();
const INFLIGHT = new Map<string, Promise<Model>>();

export interface LayoutInfo {
  /** where it came from */
  how: "memory" | "replay" | "warm" | "cold" | "main-thread";
  ms: number;
  files: number;
}
export const LAST_INFO = new Map<string, LayoutInfo>();

export function cachedModel(repo: string, raw: RawRepo): Model | null {
  return MODELS.get(repo + "\u0000" + dataSig(raw)) || null;
}

let worker: Worker | null | undefined;
let nextId = 1;
const waiting = new Map<number, { ok: (r: { rec: LayoutRecord; ms: number }) => void; bad: (e: Error) => void; prog?: (k: number) => void }>();

function getWorker(): Worker | null {
  if (worker !== undefined) return worker;
  try {
    worker = new Worker(new URL("./layout.worker.ts", import.meta.url), { type: "module", name: "codetree-layout" });
    worker.onmessage = (e: MessageEvent) => {
      const d = e.data || {};
      const w = waiting.get(d.id);
      if (!w) return;
      if (typeof d.progress === "number") w.prog?.(d.progress);
      else {
        waiting.delete(d.id);
        if (d.rec) w.ok({ rec: d.rec, ms: d.ms || 0 });
        else w.bad(new Error(d.error || "layout failed"));
      }
    };
    worker.onerror = () => {
      for (const [, w] of waiting) w.bad(new Error("layout worker failed"));
      waiting.clear();
      worker = null; // fall back to the main thread from now on
    };
  } catch {
    worker = null;
  }
  return worker;
}

function inWorker(raw: RawRepo, warm: LayoutRecord | null, prog?: (k: number) => void): Promise<{ rec: LayoutRecord; ms: number }> {
  const w = getWorker();
  if (!w) return Promise.reject(new Error("no worker"));
  const id = nextId++;
  return new Promise((ok, bad) => {
    waiting.set(id, { ok, bad, prog });
    try {
      w.postMessage({ id, raw, warm });
    } catch (err) {
      waiting.delete(id);
      bad(err as Error);
    }
  });
}

const paint = () => new Promise<void>((r) => setTimeout(r, 30));

function remember(key: string, M: Model) {
  MODELS.delete(key);
  MODELS.set(key, M);
  while (MODELS.size > MODELS_MAX) MODELS.delete(MODELS.keys().next().value!);
}

/** A laid-out model for `raw` (repo = cache key: the repo id). */
export function layoutModel(repo: string, raw: RawRepo, onProgress?: (k: number) => void): Promise<Model> {
  const sig = dataSig(raw);
  const key = repo + "\u0000" + sig;
  const hit = MODELS.get(key);
  if (hit) {
    LAST_INFO.set(repo, { how: "memory", ms: 0, files: raw.files.length });
    return Promise.resolve(hit);
  }
  const fl = INFLIGHT.get(key);
  if (fl) return fl;
  const p = (async () => {
    const t0 = performance.now();
    const recs = await loadRecords(repo);
    const exact = recs.find((r) => r.sig === sig);
    if (exact) {
      const M = buildModel(raw, { quantise: true, replay: exact.rec });
      if (!M.replayMismatch) {
        LAST_INFO.set(repo, { how: "replay", ms: Math.round(performance.now() - t0), files: raw.files.length });
        remember(key, M);
        return M;
      }
    }
    const newest = recs.find((r) => r.rec && r.rec.warmRun < WARM_RUN_MAX) || null;
    const warm = newest ? newest.rec : null;
    let rec: LayoutRecord;
    let how: LayoutInfo["how"] = warm ? "warm" : "cold";
    try {
      rec = (await inWorker(raw, warm, onProgress)).rec;
    } catch {
      await paint();
      how = "main-thread";
      rec = buildModel(raw, { quantise: true, warm, tick: onProgress }).layoutRec;
    }
    const M = buildModel(raw, { quantise: true, replay: rec });
    LAST_INFO.set(repo, { how, ms: Math.round(performance.now() - t0), files: raw.files.length });
    remember(key, M);
    void saveRecord(repo, sig, rec);
    return M;
  })();
  INFLIGHT.set(key, p);
  p.finally(() => INFLIGHT.delete(key)).catch(() => undefined);
  return p;
}
