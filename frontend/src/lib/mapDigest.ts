/** "What could break", away from the Map: the session's blast radius as a few
 * numbers, for places that only have room for one line (Make PR, Verify).
 *
 * The same set the Map's Session panel lists under its blast radius: the
 * DIRECT importers of what this session changes — the plan's files while the
 * Map would be in plan mode (a plan, nothing edited since), else the branch's
 * changes plus the feed's edits — grouped by folder, tests counted apart. Built
 * from the Map's own pure helpers (lib/codemap), so the numbers agree with what
 * "See on map" then shows. */

import { useQuery } from "@tanstack/react-query";
import type { CodeMapLive, CodeMapSnapshot } from "../api/types";
import { fetchLive, fetchSnapshot } from "./codemapApi";
import {
  autoMode,
  blastFrom,
  blastRows,
  effectiveTests,
  feedState,
  isTestPath,
  reverseIndex,
  type BlastRow,
} from "./codemap";

export interface MapDigest {
  /** Files the blast grows from (planned, or changed/edited). */
  changed: number;
  /** Non-test files that import one of them. */
  dependents: number;
  /** Folders those dependents sit in. */
  folders: number;
  /** Tests among the importers. */
  tests: number;
  /** Per folder, biggest first (BlastRow.paths holds a few file names). */
  rows: BlastRow[];
  /** False until both reads answered (and on any failure). */
  ready: boolean;
}

export const EMPTY_DIGEST: MapDigest = { changed: 0, dependents: 0, folders: 0, tests: 0, rows: [], ready: false };

/** The digest of one snapshot + live poll. Pure. */
export function digestOf(
  snap: Pick<CodeMapSnapshot, "files" | "edges"> | null | undefined,
  live: Pick<CodeMapLive, "changed" | "feed" | "plan" | "now" | "activity"> | null | undefined
): MapDigest {
  if (!snap || !live) return EMPTY_DIGEST;
  const files = snap.files || [];
  const index = new Map<string, number>();
  files.forEach((f, i) => index.set(String(f[0] || ""), i));
  const rev = reverseIndex(snap.edges || [], files.length);
  const tests = effectiveTests(snap);
  const isTest = (p: string) => {
    const i = index.get(p);
    return i !== undefined ? tests.has(i) : isTestPath(p);
  };
  const fs = feedState(live.feed || [], live.now || 0, live.activity || "");
  const plan = live.plan && live.plan.items && live.plan.items.length ? live.plan : null;
  const seeds = new Set<string>();
  if (plan && autoMode(plan, fs.lastEditTs) === "plan") for (const it of plan.items) seeds.add(it.path);
  else {
    for (const c of live.changed || []) seeds.add(c.path);
    for (const [p, f] of fs.files) if (f.kind === "edit" && f.lastTs) seeds.add(p);
  }
  const idx = [...seeds].filter((p) => index.has(p)).map((p) => index.get(p)!);
  const dependents = new Map<string, number>();
  for (const [i, hops] of blastFrom(idx, rev, 1)) {
    const f = files[i];
    if (f) dependents.set(f[0], hops);
  }
  const rows = blastRows(dependents, null, isTest);
  return {
    changed: seeds.size,
    dependents: rows.reduce((s, r) => s + r.count, 0),
    folders: rows.filter((r) => r.count > 0).length,
    tests: rows.reduce((s, r) => s + r.tests, 0),
    rows,
    ready: true,
  };
}

/** "9 files in 3 folders (2 tests)" — "" when nothing imports the changes. */
export function digestLine(d: MapDigest): string {
  if (!d.dependents && !d.tests) return "";
  const n = (k: number, one: string) => k + " " + one + (k === 1 ? "" : "s");
  const files = d.dependents ? n(d.dependents, "file") + " in " + n(d.folders, "folder") : "";
  const tests = d.tests ? n(d.tests, "test") : "";
  return files && tests ? files + " (" + tests + ")" : files || tests;
}

/** The digest for `title`, read once per open (not polled — the Map polls;
 * this is a glance). `enabled` false skips both reads. */
export function useMapDigest(title: string, enabled = true): MapDigest {
  const q = useQuery({
    queryKey: ["map-digest", title],
    queryFn: async () => {
      const [snap, live] = await Promise.all([fetchSnapshot(title), fetchLive(title, 0)]);
      return digestOf("files" in snap ? snap : null, live);
    },
    enabled: enabled && !!title,
    staleTime: 30_000,
    retry: false,
  });
  return q.data || EMPTY_DIGEST;
}
