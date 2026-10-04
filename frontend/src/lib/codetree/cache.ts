/** Code tree — layout cache. Records (layout.ts placements) are kept per repo
 * in IndexedDB, the few most recent per repo: an exact data match replays
 * instantly; a near one warm-starts the worker so a small change does not
 * reshuffle the tree. Everything is best-effort: a private window, blocked
 * storage or a quota error simply means no cache (try/catch throughout). */

import type { LayoutRecord } from "./model";

const DB = "mindflock-codetree";
const STORE = "layouts";
/** records kept per repo (several branches / worktrees of one repo) */
export const KEEP_PER_REPO = 3;
/** repos kept */
export const KEEP_REPOS = 24;

interface Row {
  repo: string;
  at: number;
  recs: Array<{ sig: string; at: number; rec: LayoutRecord }>;
}

let dbp: Promise<IDBDatabase | null> | null = null;
function db(): Promise<IDBDatabase | null> {
  if (dbp) return dbp;
  dbp = new Promise((res) => {
    try {
      if (typeof indexedDB === "undefined") return res(null);
      const rq = indexedDB.open(DB, 1);
      rq.onupgradeneeded = () => {
        try {
          rq.result.createObjectStore(STORE, { keyPath: "repo" });
        } catch {
          /* exists */
        }
      };
      rq.onsuccess = () => res(rq.result);
      rq.onerror = () => res(null);
      rq.onblocked = () => res(null);
    } catch {
      res(null);
    }
  });
  return dbp;
}

function tx<T>(mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T> | null): Promise<T | null> {
  return db().then(
    (d) =>
      new Promise<T | null>((res) => {
        if (!d) return res(null);
        try {
          const t = d.transaction(STORE, mode);
          const rq = fn(t.objectStore(STORE));
          if (!rq) return res(null);
          rq.onsuccess = () => res((rq.result as T) ?? null);
          rq.onerror = () => res(null);
        } catch {
          res(null);
        }
      })
  );
}

/** The repo's records, newest first. */
export async function loadRecords(repo: string): Promise<Array<{ sig: string; rec: LayoutRecord }>> {
  const row = await tx<Row>("readonly", (s) => s.get(repo));
  return row && Array.isArray(row.recs) ? row.recs.slice().sort((a, b) => b.at - a.at) : [];
}

export async function saveRecord(repo: string, sig: string, rec: LayoutRecord): Promise<void> {
  try {
    const row = (await tx<Row>("readonly", (s) => s.get(repo))) || { repo, at: 0, recs: [] };
    const now = Date.now();
    row.recs = [{ sig, at: now, rec }, ...(row.recs || []).filter((r) => r.sig !== sig)].slice(0, KEEP_PER_REPO);
    row.at = now;
    await tx("readwrite", (s) => s.put(row));
    // keep the store bounded: drop the least recently used repos
    const keys = await tx<IDBValidKey[]>("readonly", (s) => s.getAllKeys());
    if (keys && keys.length > KEEP_REPOS) {
      const rows = (await tx<Row[]>("readonly", (s) => s.getAll())) || [];
      rows.sort((a, b) => a.at - b.at);
      for (const r of rows.slice(0, rows.length - KEEP_REPOS)) await tx("readwrite", (s) => s.delete(r.repo));
    }
  } catch {
    /* no cache */
  }
}
