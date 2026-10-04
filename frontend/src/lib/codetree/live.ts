/** Code tree — the live poll as birds (pure, but stateful across polls).
 *
 * This session is the primary bird (the accent colour, ●); other live sessions
 * on the same repo (`live.others` — their recent edits) are the others, each
 * with its own colour and glyph. Per bird:
 *   reads  = files read (a dot on the leaf), from the tool feed
 *   edits  = files edited (the leaf takes the bird's colour): the feed's edits
 *            plus, for this session, everything changed on its branch
 *   plan   = planned files (buds); planned NEW files bud on their folder
 *   blocked= refused edits (a ✕ where it hit the rule)
 *   nest   = the deepest folder holding most of its recent edits
 * The bird perches on the leaf of its latest touch while that is fresh, else
 * sits in its nest. A new latest touch is a flight: its observation time (the
 * view clock) starts the animation, so history loads settled.
 *
 * Subagents (feed records carrying `agent`, see subagents.ts) are birds of
 * their own, listed right after their parent: smaller, a tint of the parent's
 * colour, the parent's glyph hollow + an index (○1, ○2), home = the parent's nest.
 * A helper born mid-view flies out from its parent; when done it flies back
 * into the parent's nest (the renderer fades it), and once it drops off its
 * work folds into the parent's. While it is listed, files it edited are not
 * also claimed as the parent's branch changes. */

import type { CodeMapLive, FeedRecord, PlanItem } from "../../api/types";
import { dirOf, type Model, type TFile, type TNode } from "./model";
import { BIRD_GLYPHS, birdColours, helperGlyph } from "./palette";
import { splitFeed, subColour, subagentsOf, type SubInfo } from "./subagents";
import type { AgentDef, AgentState, AgentStatus, BirdEv, EvType, TZone } from "./types";

/** A touch older than this (seconds) and the bird is back in its nest. */
export const PERCH_S = 75;
/** The nest follows this many most recent edits. */
export const NEST_RECENT = 5;

/** The deepest crown folder holding more than half (else half) of `ids`. */
export function deepestMajority(M: Model, ids: number[]): TNode {
  if (!ids.length) return M.crown;
  const cnt = new Map<TNode, number>();
  for (const id of ids) {
    let n: TNode | null = M.files[id]?.node || null;
    // a test's nest is the code it tests, a doc's the trunk
    if (n && n.kind === "root") n = M.files[id].tests || null;
    if (n && n.kind === "pile") n = null;
    while (n) {
      cnt.set(n, (cnt.get(n) || 0) + 1);
      n = n.parent;
    }
  }
  let best: TNode | null = null;
  for (const [n, c] of cnt) if (c * 2 > ids.length && n.kind === "crown" && (!best || n.depth > best.depth)) best = n;
  if (!best) for (const [n, c] of cnt) if (c * 2 >= ids.length && n.kind === "crown" && (!best || n.depth > best.depth)) best = n;
  return best || M.crown;
}

/** Nest = the deepest folder with most of the recent work: the last few
 * edits, else the plan, else the last few reads. */
export function nestFor(M: Model, A: Pick<AgentState, "edits" | "plan" | "reads" | "planNew">): TNode {
  const recent = [...A.edits.keys()].slice(-NEST_RECENT);
  if (recent.length) return deepestMajority(M, recent);
  if (A.plan.size) return deepestMajority(M, [...A.plan]);
  if (A.planNew.length) {
    const ids = A.planNew.map((p) => p.node.files[0]?.id).filter((x): x is number => x !== undefined);
    if (ids.length) return deepestMajority(M, ids);
  }
  const reads = [...A.reads.keys()].slice(-NEST_RECENT);
  return reads.length ? deepestMajority(M, reads) : M.crown;
}

/** The deepest existing crown folder on a path (where a new file will grow). */
export function folderFor(M: Model, path: string): TNode {
  let d = dirOf(path);
  while (d && !M.nodeOf.has(d)) d = dirOf(d);
  return (d && M.nodeOf.get(d)) || M.crown;
}

interface Touch {
  ts: number;
  type: EvType | "blocked";
  path: string;
  zoneId?: string | null;
  green?: boolean;
  /** a dropped-off helper's work, folded into its parent's (never its latest edit) */
  fold?: boolean;
}

/** The feed as an ordered list of touches (the feedState rules: a read at its
 * pre, an edit at its post, a refusal terminal). One touch per path per call. */
export function touchesOf(feed: FeedRecord[]): Touch[] {
  const out: Touch[] = [];
  const recs = feed.slice().sort((a, b) => (a.ts || 0) - (b.ts || 0));
  for (const r of recs) {
    const ts = r.ts || 0;
    if (r.deny) {
      if (r.deny.path && !r.deny.push) out.push({ ts, type: "blocked", path: r.deny.path, zoneId: r.deny.zone_id, green: r.deny.kind === "green" });
      continue;
    }
    if (r.kind === "plan" && r.ev !== "pre") {
      out.push({ ts, type: "plan", path: "" });
      continue;
    }
    if (r.ev === "pre") {
      if (r.kind === "read") for (const p of r.reads || []) out.push({ ts, type: "read", path: p });
      continue;
    }
    if (r.ev === "post") {
      for (const p of r.writes || []) out.push({ ts, type: r.tool === "Write" ? "create" : "edit", path: p });
      // a Read lit up at its pre; a Bash (cat, grep …) reports its reads at the post
      if (r.kind !== "edit" && r.kind !== "read") for (const p of r.reads || []) out.push({ ts, type: "read", path: p });
    }
  }
  return out;
}

export interface LiveInput {
  feed: FeedRecord[];
  live: CodeMapLive | null;
  /** this session's title */
  title: string;
  /** colour for this session's bird (the accent, tuned for the theme) */
  accent: string;
  /** server clock now (epoch seconds) */
  serverNow: number;
  /** view clock now (seconds) */
  viewNow: number;
  zones: TZone[];
  /** the light theme is on (subagent tints press darker instead of lifting) */
  light?: boolean;
}

/** Keeps each bird's object (and its flight) across polls. */
export class LiveBirds {
  private byKey = new Map<string, AgentState>();
  private lastKey = new Map<string, string>();
  private M: Model | null = null;
  /** subagent id -> its stable 1-based index */
  private subIdx = new Map<string, number>();
  /** subagent bird key -> its stable numeric id */
  private subIds = new Map<string, number>();
  private nextId = 100;
  /** a poll has been drawn: a NEW helper from here on flies out (the first look is settled) */
  private primed = false;
  agents: AgentState[] = [];

  update(M: Model, inp: LiveInput): AgentState[] {
    const fresh = this.M !== M;
    if (fresh) this.primed = false;
    this.M = M;
    const live = inp.live;
    const others = new Map<string, Array<{ path: string; ts: number }>>();
    for (const o of live?.others || []) {
      if (!o || !o.session || o.session === inp.title) continue;
      const a = others.get(o.session) || [];
      a.push({ path: o.path, ts: o.ts || 0 });
      others.set(o.session, a);
    }
    // this session's own calls vs its subagents' (each its own bird while listed;
    // a helper that dropped off folds its work into the parent's)
    const { main } = splitFeed(inp.feed);
    const subs = subagentsOf(inp.feed, inp.serverNow, live?.activity || "", this.subIdx);
    const listed = subs.filter((s) => s.listed);
    const own = touchesOf(main);
    const foldT = touchesOf(subs.filter((s) => !s.listed).flatMap((s) => s.recs)).map((t) => ({ ...t, fold: true }));
    const claimed = new Set<string>();
    const subTouches = new Map<string, Touch[]>();
    for (const s of listed) {
      const ts = touchesOf(s.recs);
      subTouches.set(s.id, ts);
      for (const t of ts) if (t.type === "edit" || t.type === "create") claimed.add(t.path);
    }
    const keys = [inp.title, ...[...others.keys()].sort()];
    const colours = birdColours(inp.accent, keys.length);
    const out: AgentState[] = [];
    const seen = new Set<string>();
    keys.forEach((key, i) => {
      const ag: AgentDef = { id: i, key, name: key, color: colours[i], glyph: BIRD_GLYPHS[i % BIRD_GLYPHS.length], primary: i === 0 };
      let A = this.byKey.get(key);
      if (!A || fresh) {
        A = emptyAgent(ag);
        this.byKey.set(key, A);
      } else A.ag = ag;
      seen.add(key);
      if (i === 0) {
        const all = foldT.length ? own.concat(foldT).sort((a, b) => a.ts - b.ts) : own;
        fill(M, A, all, { prim: inp, viewNow: inp.viewNow, zones: inp.zones, exclude: claimed });
        this.place(M, A, own, inp, live?.activity || "", false);
        out.push(A);
        for (const s of listed) {
          const S = this.subBird(M, A, s, subTouches.get(s.id) || [], inp, fresh);
          seen.add(S.ag.key);
          out.push(S);
        }
        return;
      }
      const touches = (others.get(key) || []).slice().sort((a, b) => a.ts - b.ts).map((e) => ({ ts: e.ts, type: "edit" as EvType, path: e.path }));
      fill(M, A, touches, { prim: null, viewNow: null, zones: inp.zones });
      this.place(M, A, touches, inp, "working", false);
      out.push(A);
    });
    for (const k of [...this.byKey.keys()])
      if (!seen.has(k)) {
        this.byKey.delete(k);
        this.lastKey.delete(k);
      }
    this.agents = out;
    this.primed = true;
    return out;
  }

  /** A subagent's bird: smaller, in its parent's colour family, home = the
   * parent's nest. Born mid-view it flies out from where the parent is. */
  private subBird(M: Model, P: AgentState, s: SubInfo, touches: Touch[], inp: LiveInput, fresh: boolean): AgentState {
    const key = P.ag.key + "::" + s.id;
    let id = this.subIds.get(key);
    if (id === undefined) {
      id = this.nextId++;
      this.subIds.set(key, id);
    }
    const ag: AgentDef = {
      id, key, name: s.name, short: s.short, color: subColour(P.ag.color, s.index, !!inp.light), glyph: helperGlyph(P.ag.glyph, s.index), primary: false, parent: P.ag.key, sub: s.index,
    };
    let A = this.byKey.get(key);
    const born = !A || fresh;
    if (!A || fresh) {
      A = emptyAgent(ag);
      this.byKey.set(key, A);
      this.lastKey.delete(key);
      // it leaves from its parent: the first flight starts where the parent is
      if (P.cur) A.evs = [{ ...P.cur, t: inp.viewNow - 100 }];
    } else A.ag = ag;
    A.subInfo = s;
    fill(M, A, touches, { prim: null, viewNow: inp.viewNow, zones: inp.zones });
    A.nest = P.nest;
    A.nestSince = P.nestSince;
    this.place(M, A, touches, inp, s.done ? "idle" : "working", born && this.primed);
    A.task = s.desc;
    return A;
  }

  /** Status, current touch and the flight. `flyIn`: a bird first seen now
   * flies (a new helper leaving its parent) instead of appearing settled. */
  private place(M: Model, A: AgentState, touches: Touch[], inp: LiveInput, activity: string, flyIn: boolean) {
    let last: Touch | null = null;
    for (let k = touches.length - 1; k >= 0; k--) {
      const t = touches[k];
      if (t.type === "plan" || M.byPath.has(t.path)) {
        last = t;
        break;
      }
    }
    const age = last ? inp.serverNow - last.ts : Infinity;
    const perched = !!last && last.type !== "plan" && age < PERCH_S && activity !== "idle" && activity !== "offline";
    let status: AgentStatus;
    if (!touches.length && !A.edits.size && !A.plan.size) status = activity === "working" ? "thinking" : activity === "idle" && A.ag.parent ? "done" : "waiting";
    else if (activity === "idle" || activity === "offline") status = "done";
    else if (activity === "clarify") status = "waiting";
    else if (perched && last)
      status = last.type === "blocked" ? "blocked" : last.type === "read" ? "reading" : last.type === "create" ? "creating" : "editing";
    else if (A.plan.size && last && last.type === "plan") status = "planning";
    else status = "thinking";
    A.status = status;
    A.done = status === "done";
    A.activity = activity;
    A.file = perched && last ? M.byPath.get(last.path) || null : null;
    const evKey = perched && last ? last.type + "|" + last.path + "|" + last.ts : "nest|" + (A.nest ? A.nest.path : "");
    const prevKey = this.lastKey.get(A.ag.key);
    if (prevKey !== evKey) {
      // a new perch (or back to the nest): fly — unless this is the first look
      const t = prevKey === undefined && !flyIn ? inp.viewNow - 100 : inp.viewNow;
      const type: EvType = perched && last ? (last.type === "blocked" ? "edit" : (last.type as EvType)) : "nest";
      const f = perched && last ? M.byPath.get(last.path) || null : null;
      const blocked = perched && last && last.type === "blocked" ? A.blocked[A.blocked.length - 1]?.z || null : null;
      const ev: BirdEv = { t, type, f, blocked, nestNode: A.nest };
      A.evs = [...A.evs.slice(-1), ev];
      A.cur = ev;
      if (perched && last && last.type === "blocked" && A.blocked.length) A.blocked[A.blocked.length - 1].t = t;
      this.lastKey.set(A.ag.key, evKey);
    } else if (A.cur) A.cur.nestNode = A.nest;
    if (!A.cur) {
      const ev: BirdEv = { t: inp.viewNow - 100, type: "nest", f: null, blocked: null, nestNode: A.nest };
      A.evs = [ev];
      A.cur = ev;
    }
  }
}

const EDIT_SEEN = new WeakMap<AgentState, { key: string; t: number }>();

function emptyAgent(ag: AgentDef): AgentState {
  return {
    ag, reads: new Map(), edits: new Map(), plan: new Set(), planNew: [], created: new Set(), blocked: [], nest: null, nestSince: 0, evs: [],
    cur: null, lastEdit: null, status: "waiting", file: null, done: false, activity: "", task: "",
  };
}

interface FillCtx {
  /** the primary bird's input (its branch changes and plan), else null */
  prim: LiveInput | null;
  /** view clock for new edits' gold ripple (null: no ripple — another session) */
  viewNow: number | null;
  zones: TZone[];
  /** branch changes a live subagent made: its edits, not the parent's */
  exclude?: Set<string>;
}

/** Reads, edits, plan, blocks and the nest from the touches (+ the primary's
 * branch changes and plan). */
function fill(M: Model, A: AgentState, touches: Touch[], ctx: FillCtx) {
  const { prim, zones } = ctx;
  const reads = new Map<number, number>(),
    edits = new Map<number, number>(),
    created = new Set<number>();
  const blocked: AgentState["blocked"] = [];
  const oldBlocked = new Map(A.blocked.map((b) => [b.path + "|" + b.kind, b.t]));
  let lastEdit: AgentState["lastEdit"] = null;
  const live = prim?.live || null;
  if (prim && live) {
    // the branch's changes are this session's edits too (made before the map armed, or by hand)
    for (const c of live.changed || []) {
      if (ctx.exclude && ctx.exclude.has(c.path)) continue;
      const f = M.byPath.get(c.path);
      if (f) edits.set(f.id, 0);
    }
  }
  for (const t of touches) {
    const f = M.byPath.get(t.path) || null;
    if (t.type === "read" && f) {
      reads.delete(f.id);
      reads.set(f.id, t.ts);
    } else if ((t.type === "edit" || t.type === "create") && f) {
      edits.delete(f.id);
      edits.set(f.id, t.ts);
      if (t.type === "create") created.add(f.id);
      if (!t.fold) lastEdit = { f, t: t.ts };
    } else if (t.type === "blocked") {
      const z = zoneById(zones, t.zoneId, f, !!t.green);
      const kind = t.green ? "only" : "keep";
      blocked.push({ t: oldBlocked.get(t.path + "|" + kind) ?? -100, f, z, kind, path: t.path });
    }
  }
  // the gold ripple's clock: a NEW latest edit (seen arriving) ripples from now;
  // one that was already there when the map opened shows its whole blast at once
  if (lastEdit) {
    const key = lastEdit.f.id + "|" + lastEdit.t;
    const prev = EDIT_SEEN.get(A);
    lastEdit.t = prev && prev.key === key ? prev.t : prev && ctx.viewNow !== null ? ctx.viewNow : -100;
    EDIT_SEEN.set(A, { key, t: lastEdit.t });
  } else if (!EDIT_SEEN.has(A)) EDIT_SEEN.set(A, { key: "", t: -100 });
  A.reads = reads;
  A.edits = edits;
  A.created = created;
  A.blocked = blocked.slice(-12);
  A.lastEdit = lastEdit;
  // plan: buds on the files it will touch that it has not touched since
  const plan = new Set<number>();
  const planNew: AgentState["planNew"] = [];
  const items: PlanItem[] = (prim && live?.plan?.items) || [];
  const pts = (prim && live?.plan?.ts) || 0;
  for (const it of items) {
    if (!it || !it.path) continue;
    const f = M.byPath.get(it.path);
    if (f) {
      const e = edits.get(f.id);
      if (e === undefined || (e > 0 && e < pts) || (e === 0 && !pts)) plan.add(f.id);
    } else planNew.push({ path: it.path, node: folderFor(M, it.path) });
  }
  A.plan = plan;
  A.planNew = planNew;
  const nest = nestFor(M, A);
  if (nest !== A.nest) {
    A.nest = nest;
    A.nestSince = ctx.viewNow !== null ? ctx.viewNow : 0;
  }
}

function zoneById(zones: TZone[], id: string | null | undefined, f: TFile | null, green: boolean): TZone | null {
  if (id) {
    const z = zones.find((z) => z.z.id === id && (!f || z.file === f.id || (!!z.node && isUnderNode(f.node, z.node))));
    if (z) return z;
    const any = zones.find((z) => z.z.id === id);
    if (any) return any;
  }
  if (green) return zones.find((z) => z.type === "only") || null;
  return null;
}
function isUnderNode(n: TNode | null, anc: TNode): boolean {
  for (let q = n; q; q = q.parent) if (q === anc) return true;
  return false;
}

/** For the agent card: "editing server.py" / "⛔ tried x — keep out" … */
export const STATUS_TXT: Record<AgentStatus, string> = {
  waiting: "waiting",
  planning: "planning",
  reading: "reading",
  editing: "editing",
  creating: "creating",
  blocked: "blocked",
  thinking: "thinking",
  done: "done",
};
