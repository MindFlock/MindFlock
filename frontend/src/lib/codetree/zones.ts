/** Code tree — the server's zones as tree rules (pure).
 *
 * A zone is a pattern on the server (`re` is its compiled regex). On the tree
 * it becomes the folders it covers WHOLE (the largest such folders — a branch
 * band) plus any single files it matches outside them (a ring on the leaf).
 * Nothing is kept client-side: the rules are recomputed from every poll. */

import type { RedZone } from "../../api/types";
import { anchoredZonePath, isGreen } from "../codemap";
import { allNodes, shortName, type Model, type TFile, type TNode } from "./model";
import type { TZone } from "./types";

/** At most this many single-file rings per zone (a scattered glob). */
export const FILE_ZONE_CAP = 300;

function compile(z: RedZone, ci: boolean): RegExp | null {
  if (!z.re) return null;
  try {
    return new RegExp(z.re, ci ? "i" : "");
  } catch {
    return null;
  }
}

/** Tree rules for the server zones. Waived zones are kept (drawn faint). */
export function treeZones(M: Model, zones: RedZone[], ci = false): TZone[] {
  const out: TZone[] = [];
  const regions = [M.crown, M.roots, ...M.piles];
  for (const z of zones) {
    const rx = compile(z, ci);
    if (!rx) continue;
    const hit = new Set<number>();
    for (const f of M.files) if (!f.ghost && rx.test(f.path)) hit.add(f.id);
    if (!hit.size) continue;
    const type = isGreen(z) ? "only" : "keep";
    const label = z.name || shortName(z.pattern.replace(/^\/+/, "").replace(/\/(\*\*)?$/, ""));
    const covered = new Set<number>();
    // whole = every (real) file of this subtree matches, and it has some
    const whole = new Map<TNode, boolean>();
    const calc = (n: TNode): boolean => {
      let any = false,
        all = true;
      for (const f of n.files) {
        if (f.ghost) continue;
        any = true;
        if (!hit.has(f.id)) all = false;
      }
      for (const k of n.kids) {
        const w = calc(k);
        if (k.nFiles) {
          any = true;
          if (!w) all = false;
        }
      }
      const r = all && any;
      whole.set(n, r);
      return r;
    };
    const top = (n: TNode) => {
      if (n.depth >= 1 && whole.get(n)) {
        out.push({ type, node: n, file: null, z, waived: !!z.waived, label });
        for (const m of allNodes(n)) for (const f of m.files) covered.add(f.id);
        return;
      }
      for (const k of n.kids) top(k);
    };
    for (const r of regions) {
      if (r.kind === "pile") {
        if (r.files.length && r.files.every((f) => hit.has(f.id))) {
          out.push({ type, node: r, file: null, z, waived: !!z.waived, label });
          for (const f of r.files) covered.add(f.id);
        }
      } else {
        calc(r);
        top(r);
      }
    }
    let n = 0;
    for (const id of hit) {
      if (covered.has(id)) continue;
      if (++n > FILE_ZONE_CAP) break;
      out.push({ type, node: null, file: id, z, waived: !!z.waived, label });
    }
  }
  return out;
}

/** Is file f inside a rule that blocks edits? (`keep` = inside a keep-out,
 * `only` = outside every only-here). Waived zones do not block. */
export function zoneOfFile(f: TFile, zones: TZone[]): { type: "keep" | "only"; z: TZone } | null {
  const live = zones.filter((z) => !z.waived);
  if (!live.length) return null;
  const anc = new Set<TNode>();
  for (let n: TNode | null = f.node; n; n = n.parent) anc.add(n);
  const inZone = (z: TZone) => z.file === f.id || (!!z.node && anc.has(z.node));
  for (const z of live) if (z.type === "keep" && inZone(z)) return { type: "keep", z };
  const only = live.filter((z) => z.type === "only");
  if (only.length && !only.some(inZone)) return { type: "only", z: only[0] };
  return null;
}

/** The pattern that fences a folder or a file of the tree, or null with the
 * reason when one pattern cannot say it (tests grouped from several folders,
 * the repo-root pile). */
export function patternFor(M: Model, target: { node?: TNode | null; file?: TFile | null }): { pattern: string; label: string } | { error: string } {
  if (target.file) return { pattern: anchoredZonePath(target.file.path), label: target.file.path };
  const n = target.node;
  if (!n) return { error: "Click a folder name, a branch or a leaf." };
  if (n.depth === 0 && n.kind !== "pile") return { error: "Paint a branch, not the trunk — the whole repo cannot be fenced." };
  if (n.kind === "crown") return { pattern: anchoredZonePath(n.path), label: n.path };
  // roots and piles group files by what they test / where they lie: one
  // folder must hold exactly those files
  const files = allNodes(n).flatMap((m) => m.files);
  if (!files.length) return { error: "Nothing to fence here." };
  const dirs = files.map((f) => f.path.split("/").slice(0, -1));
  let common = dirs[0];
  for (const d of dirs) {
    let k = 0;
    while (k < common.length && k < d.length && common[k] === d[k]) k++;
    common = common.slice(0, k);
  }
  const dir = common.join("/");
  if (!dir) return { error: `${n.label || n.name} lies in the repo root — fence its files one by one (zoom in and click a leaf).` };
  const mine = new Set(files.map((f) => f.id));
  const others = M.files.some((f) => !mine.has(f.id) && f.path.startsWith(dir + "/"));
  if (others)
    return { error: `${n.label || n.name} share ${dir}/ with other files — fence that folder from search, or single files.` };
  return { pattern: anchoredZonePath(dir), label: dir };
}
