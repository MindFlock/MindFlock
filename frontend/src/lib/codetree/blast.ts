/** Code tree — blast radius and its badges (pure).
 *
 * Gold leaves = the DIRECT importers of the files a bird edited, cumulative per
 * bird; the badges count them per folder ("web · 12 depend on server.py") and
 * always sum to the bird's "affects N": a folder's count that would make a
 * pill too small or too many pills is folded into its top-level limb, never
 * dropped. */

import { S0, shortName, type Model, type TNode } from "./model";
import type { AgentDef, AgentState, Badge, BadgeSet, TreeView } from "./types";

/** Direct importers only: the same set the activity row, the leaf card and the
 * badges all count. */
export function blastOf(M: Model, fid: number): { h1: Set<number> } {
  const h1 = new Set(M.files[fid].usedBy);
  h1.delete(fid);
  return { h1 };
}

/** Everything an agent's edits so far could break: the union of direct
 * importers over its edits; `by` = which (latest) edited file each depends on. */
export function agentBlast(M: Model, A: Pick<AgentState, "edits">, cache: Map<number, { h1: Set<number> }>) {
  const ids = new Set<number>();
  const by = new Map<number, number>();
  for (const [fid] of A.edits) {
    let b = cache.get(fid);
    if (!b) {
      b = blastOf(M, fid);
      cache.set(fid, b);
    }
    for (const id of b.h1) {
      ids.add(id);
      by.set(id, fid);
    }
  }
  return { ids, by };
}

/** One blast's files into per-(folder, owner) badge sets at the view's depth.
 * `shown(id)` = the leaf is counted now (the gold ripple has reached it). */
export function addBadgeSets(
  v: Pick<TreeView, "M" | "cam" | "badgeSets">,
  ids: Set<number>,
  by: Map<number, number> | null,
  owner: { ag: AgentDef | null; hover: boolean; fid: number | null },
  alpha: number,
  shown: (id: number) => boolean = () => true
) {
  const M = v.M;
  const lp = v.cam.z * S0;
  const deep = lp > 24 ? 3 : lp > 11 ? 2 : 1;
  const who = owner.ag ? "a" + owner.ag.id : owner.hover ? "h" : "p";
  const sets = v.badgeSets || (v.badgeSets = new Map());
  for (const id of ids) {
    const ff = M.files[id];
    if (!ff || !ff.leaf || !shown(id)) continue;
    let n: TNode | null = ff.node;
    if (!n) continue;
    if (n.kind !== "pile") {
      while (n && n.depth > deep) n = n.parent;
      while (n && n.vHidden) n = n.parent;
    }
    if (!n) continue;
    const key = n.id + "|" + who;
    let e = sets.get(key);
    if (!e) sets.set(key, (e = { n, ids: new Set(), files: new Set(), alpha: 0, ag: owner.ag, pin: !owner.ag, hover: owner.hover, who }));
    e.ids.add(id);
    e.files.add(by ? by.get(id)! : owner.fid!);
    e.alpha = Math.max(e.alpha, alpha * (owner.hover ? 0.9 : 1));
  }
}

/** Badge sets → placed badges: merge the smallest into their top-level folder
 * when there are too many, so the visible numbers always sum to the count. */
export function collectBadges(v: Pick<TreeView, "M" | "cam" | "badgeSets" | "badges">): Badge[] {
  const M = v.M;
  const CAP = 12;
  const lp = v.cam.z * S0;
  const sets = [...(v.badgeSets || new Map<string, BadgeSet>()).values()].filter((e) => e.ids.size);
  const byWho = new Map<string, BadgeSet[]>();
  for (const e of sets) {
    let a = byWho.get(e.who);
    if (!a) byWho.set(e.who, (a = []));
    a.push(e);
  }
  const mergeUp = (list: BadgeSet[], victim: BadgeSet) => {
    let p: TNode | null = victim.n;
    while (p && p.depth > 1) p = p.parent;
    const top = p || victim.n;
    let host = list.find((e) => e.n === top);
    if (!host) {
      host = { n: top, ids: new Set(), files: new Set(), alpha: victim.alpha, ag: victim.ag, pin: victim.pin, hover: victim.hover, who: victim.who };
      list.push(host);
    }
    for (const id of victim.ids) host.ids.add(id);
    for (const f of victim.files) host.files.add(f);
    host.alpha = Math.max(host.alpha, victim.alpha);
    return list.filter((e) => e !== victim);
  };
  const out: BadgeSet[] = [];
  for (const [, arr] of byWho) {
    let list = arr;
    // zoomed out, a count of one or two on a sub-folder is a pill fighting for pixels: fold it into its limb
    if (lp < 14) for (const e of list.slice()) if (e.ids.size < 3 && e.n.depth > 1 && e.n.kind !== "pile" && list.includes(e)) list = mergeUp(list, e);
    while (list.length > CAP) {
      list.sort((a, b) => a.ids.size - b.ids.size);
      const victim = list.find((e) => e.n.depth > 1 && e.n.kind !== "pile");
      if (!victim) break;
      list = mergeUp(list, victim);
    }
    out.push(...list);
  }
  // a pinned / hovered file's badge duplicates a live one when a bird edited that very file: one badge per folder
  const live = out.filter((e) => e.ag);
  const dedup = out.filter((e) => e.ag || !live.some((L) => L.n === e.n && [...e.ids].every((id) => L.ids.has(id))));
  const badges: Badge[] = [];
  for (const e of dedup) {
    const n = e.n,
      c = e.ids.size;
    let P: [number, number];
    if (n.kind === "pile") P = [n.cx, M.groundY - (n.h || 0) * 0.8];
    else if (n.depth === 0) P = n === M.roots ? [0, M.groundY + S0 * 2] : [0, -S0 * 1.5];
    else if (n.depth === 1) P = n.F || [n.cx, n.cy]; // a limb's badge sits where the limb forks, next to its name
    else P = n.branch ? [n.branch.pts[7][0], n.branch.pts[7][1]] : n.F || [n.cx, n.cy];
    const one = e.files.size === 1 ? M.files[[...e.files][0]] : null;
    // a helper's badge says its short name ("explore#1's edits"), not its whole task
    const what = one ? one.name : e.ag ? `${e.ag.short || e.ag.name}'s edits` : "it";
    // the badge names the folder it sits on, so "what might break" reads without a branch label in view
    const where =
      n.kind === "pile" ? n.label! : n.depth === 0 ? (n === M.roots ? "tests" : "root files") : n.kind === "root" ? n.label! : n.disp || shortName(n.name);
    const text =
      n.kind === "root" || n === M.roots
        ? `${where} · ${c} ${c === 1 ? "test depends" : "tests depend"} on ${what}`
        : `${where} · ${c} ${c === 1 ? "depends" : "depend"} on ${what}`;
    badges.push({ x: P[0], y: P[1], text, short: `${where} · ${c}`, alpha: e.alpha, n: c, ag: e.ag, pin: e.pin, node: n, files: e.files, ids: e.ids, what });
  }
  badges.sort((a, b) => b.n - a.n);
  v.badges = badges;
  return badges;
}
