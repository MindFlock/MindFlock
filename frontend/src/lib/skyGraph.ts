/** Round 18 — free-floating chains (docs/flights.md "Round 18"): the Sky draws
 * each flight as a CHAIN of free-floating tiles (n8n-style) instead of a row.
 * This module is the pure model: a chain's head + nodes with chain-local
 * positions (a step's saved `pos`, else `autoPlace`), its ports and wires, and
 * `wireIntent` — what dropping a wire somewhere means. SkyChain draws it; the
 * Sky (SkyView) places chain origins and runs the wire drags.
 *
 * Coordinates are chain-local plane px: (0, 0) is the head's top-left (with
 * no head, the slot's origin). A node's box is `NODE_W × NODE_H`; its tile
 * (`TILE` square) sits centred at the box's top, the label area under it.
 */

import { firstStepOf, loopSlotAfter, type Diagram, type DiagramEdge, type DiagramNode, type LoopMark } from "./flightDiagram";
import type { Pos, Rect } from "./skyLayout";

/** Tile metrics (plane px). The tile is the square; the node's box adds the
 * label area under it. */
export const TILE = 64;
export const NODE_W = 132;
export const NODE_H = 140;
/** Left→right pitch of auto-placed nodes, and the row gap for branches. */
export const PITCH_X = 150;
export const PITCH_Y = 150;
export const GRID_SNAP = 8;
/** The trailing "+" stub: its button's centre this far right of the last
 * node's out port, and the button's size. */
export const PLUS_REACH = 52;
export const PLUS_SIZE = 24;
/** A loop wire's run: this far under the lowest box it passes, each further
 * loop (`lane`) another `LOOP_LANE` lower; its hooks bulge this far out. */
export const LOOP_CLEAR = 16;
export const LOOP_LANE = 20;
export const LOOP_HOOK = 40;
/** The id every chain's head node goes by. */
export const HEAD_ID = "head";

/** What starts a chain. `session`: the session tile (its title in `session`);
 * `trigger`: another chain hands off to this one (`from`); `none`: nothing
 * yet — the first node's in port shows an empty socket. */
export type ChainHeadKind = "session" | "trigger" | "none";

export interface ChainHead {
  kind: ChainHeadKind;
  title: string;
  sub: string;
  /** The session it runs in (session head), or will run in (trigger head:
   * the handoff's `session`, "new" for a new one). */
  session?: string;
  /** `chainColor(session)`; neutral for none. */
  color: string;
  /** trigger head: the chain and stop that start this one. */
  from?: { flight: string; step: string; title: string };
  /** session head: the diagram's session node (its chip, state, bird). */
  node?: DiagramNode;
}

export interface GraphNode {
  /** The diagram node's id ("stop:s3", "group:g1", "child:x", or "head"). */
  id: string;
  /** The diagram node it draws (state, step, icon, sub, …); the head's is
   * `head.node` or a synthetic one. */
  node: DiagramNode;
  /** Chain-local top-left of the node's box (tile centred at the top). */
  x: number;
  y: number;
  w: number;
  h: number;
  /** It has a saved `pos` (else auto-placed). */
  pinned: boolean;
  /** Can be a wire's target / source (a stop that can still change, a
   * stops-only chain's first node, a head). */
  inPort: boolean;
  outPort: boolean;
}

export type GraphWireKind = "line" | "loop" | "fan" | "join" | "add" | "head";

export interface GraphWire {
  id: string;
  from: string;
  to: string;
  kind: GraphWireKind;
  /** SVG path, chain-local. */
  d: string;
  /** Where a pill sits (a loop's count). */
  mid: Pos;
  loop?: LoopMark;
  via?: DiagramNode;
  done?: boolean;
  /** A loop wire's lane (0 = nearest the tiles; nested loops go lower). */
  lane?: number;
}

export interface ChainGraph {
  /** The sky's line key (a session title, or the grounded key). */
  key: string;
  flightId: string;
  head: ChainHead;
  /** The head's own node (kind session/trigger), null for a none head. */
  headNode: GraphNode | null;
  nodes: GraphNode[];
  wires: GraphWire[];
  /** Every node's box (and the loop wires' reach), chain-local. */
  bbox: Rect;
  /** The diagram's `insertFrom`: a stop may go after a node whose next slot
   * is at or past it (a live flight: after its current stop). */
  insertFrom?: number;
  /** The trailing "+": the node it hangs off (`HEAD_ID` for a head with no
   * stops) and the button's centre; null when the diagram has no +. */
  plus?: { after: string; at: Pos } | null;
}

export interface ChainGraphOpts {
  head: ChainHead;
  /** The line key and its flight. */
  key: string;
  flightId: string;
}

// --- Building a chain ---------------------------------------------------------------

const isStopish = (n: DiagramNode) => n.kind === "stop" || n.kind === "group";
export const isBranchRow = (n: DiagramNode) => n.row === "branch" || n.row === "below";
const savedPos = (n: DiagramNode): Pos | null => {
  // A stop's own `pos` (a group's: its first sub-step's); a sub-session's,
  // kept per device (`node.pos`).
  const p = firstStepOf(n)?.pos ?? n.pos;
  return p && Number.isFinite(p.x) && Number.isFinite(p.y) ? { x: Math.round(p.x), y: Math.round(p.y) } : null;
};

/** A stop may go right after `n` (its next slot is open): a Loop drawn out
 * of it, a stop moved / spliced / picked after it. */
export function canFollow(n: Pick<DiagramNode, "index" | "kind" | "group">, insertFrom: number): boolean {
  if (n.kind !== "stop" && n.kind !== "group") return false;
  const slot = loopSlotAfter(n);
  return slot !== undefined && slot >= insertFrom;
}

/** A node a Loop can leave from / come back to: a stop (not a Loop) or a
 * group, with an index (FlywayCanvas's `loopable`). */
export function loopable(n: Pick<DiagramNode, "kind" | "step" | "index">): boolean {
  return (n.kind === "stop" ? !!n.step && n.step.kind !== "loop" : n.kind === "group") && n.index !== undefined;
}

function headDiagramNode(head: ChainHead): DiagramNode {
  if (head.node) return head.node;
  return {
    id: HEAD_ID,
    kind: "session",
    row: "main",
    title: head.title,
    sub: head.sub,
    state: head.kind === "session" ? "session" : "pending",
    icon: head.kind === "trigger" ? "handoff" : "session",
    ...(head.session ? { session: head.session } : {}),
  };
}

/** The chain of a diagram (sessionDiagram / groundedDiagram output, loops
 * lifted): the head, every main-row stop/group/branch node placed (saved
 * `step.pos` else `autoPlace`), the wires routed between ports. */
export function chainGraph(d: Diagram, opts: ChainGraphOpts): ChainGraph {
  const head = opts.head;
  const headNode: GraphNode | null =
    head.kind === "none" ? null : { id: HEAD_ID, node: headDiagramNode(head), x: 0, y: 0, w: NODE_W, h: NODE_H, pinned: true, inPort: false, outPort: true };
  const sessionIds = new Set(d.nodes.filter((n) => n.kind === "session").map((n) => n.id));
  const idOf = (id: string) => (sessionIds.has(id) ? HEAD_ID : id);
  const main = d.nodes.filter((n) => n.row === "main" && n.kind !== "session" && n.kind !== "add" && n.kind !== "parent");
  const branches = d.nodes.filter((n) => isBranchRow(n) && n.kind !== "parent");
  const firstId = main[0]?.id;
  const stopsOnly = head.kind !== "session";
  const insertFrom = d.insertFrom || 0;
  const toNode = (n: DiagramNode): GraphNode => {
    const pos = savedPos(n);
    const editable = isStopish(n) && !!n.editable;
    const first = stopsOnly && n.id === firstId;
    return {
      id: n.id,
      node: n.branch ? { ...n, branch: { ...n.branch, from: idOf(n.branch.from), to: idOf(n.branch.to) } } : n,
      x: pos ? pos.x : 0,
      y: pos ? pos.y : 0,
      w: NODE_W,
      h: NODE_H,
      pinned: !!pos,
      inPort: editable || first,
      outPort: canFollow(n, insertFrom),
    };
  };
  const placed = autoPlace([...(headNode ? [headNode] : []), ...main.map(toNode), ...branches.map(toNode)]);
  const hn = headNode ? placed[0] : null;
  const nodes = hn ? placed.slice(1) : placed;

  // The wires: the diagram's edges between drawn nodes (the session node is
  // the head), the head into the first stop when the diagram has none, the
  // + stub off the last node.
  const ids = new Set(placed.map((n) => n.id));
  const specs: WireSpec[] = [];
  const seen = new Set<string>();
  const push = (s: WireSpec) => {
    const k = s.from + ">" + s.to + ">" + s.kind + ">" + (s.via?.id || "");
    if (seen.has(k)) return;
    seen.add(k);
    specs.push(s);
  };
  let addFrom: string | null = null;
  for (const e of d.edges) {
    const from = idOf(e.from);
    const to = idOf(e.to);
    if (e.kind === "add") {
      if (ids.has(from)) addFrom = from;
      continue;
    }
    if (!ids.has(from) || !ids.has(to)) continue;
    const kind = wireKindOf(e, from);
    if (!kind) continue;
    push({ from, to, kind, ...(e.loop ? { loop: e.loop } : {}), ...(e.via ? { via: e.via } : {}), ...(e.done ? { done: true } : {}) });
  }
  if (hn && main.length && !specs.some((s) => s.from === HEAD_ID)) push({ from: HEAD_ID, to: main[0].id, kind: "head" });
  const hasAdd = d.nodes.some((n) => n.kind === "add");
  if (hasAdd && !addFrom) addFrom = main.length ? main[main.length - 1].id : hn ? HEAD_ID : null;
  if (addFrom) push({ from: addFrom, to: "add", kind: "add" });
  return assemble({ key: opts.key, flightId: opts.flightId, head, insertFrom }, hn, nodes, specs);
}

interface WireSpec {
  from: string;
  to: string;
  kind: GraphWireKind;
  loop?: LoopMark;
  via?: DiagramNode;
  done?: boolean;
}

function wireKindOf(e: DiagramEdge, from: string): GraphWireKind | null {
  switch (e.kind) {
    case "line":
      return from === HEAD_ID ? "head" : "line";
    case "fan":
    case "branch":
      return "fan";
    case "join":
      return "join";
    case "loop":
      return "loop";
    default:
      return null;
  }
}

/** Route the wires between placed nodes and measure the chain. */
function assemble(base: Pick<ChainGraph, "key" | "flightId" | "head" | "insertFrom">, headNode: GraphNode | null, nodes: GraphNode[], specs: readonly WireSpec[]): ChainGraph {
  const all = headNode ? [headNode, ...nodes] : nodes;
  const byId = new Map(all.map((n) => [n.id, n]));
  const order = new Map(all.map((n, i) => [n.id, i]));
  const wires: GraphWire[] = [];
  let plus: ChainGraph["plus"] = null;
  // Loops nest: a loop whose span holds another runs a lane lower.
  const loops = specs.filter((s) => s.kind === "loop");
  const spanOf = (s: WireSpec) => {
    const a = order.get(s.from) ?? 0;
    const b = order.get(s.to) ?? 0;
    return [Math.min(a, b), Math.max(a, b)] as const;
  };
  const laneOf = new Map<WireSpec, number>();
  [...loops]
    .sort((p, q) => spanOf(p)[1] - spanOf(p)[0] - (spanOf(q)[1] - spanOf(q)[0]))
    .forEach((s, k, sorted) => {
      const [lo, hi] = spanOf(s);
      let lane = 0;
      for (const t of sorted.slice(0, k)) {
        const [l2, h2] = spanOf(t);
        if (l2 <= hi && h2 >= lo) lane = Math.max(lane, (laneOf.get(t) ?? 0) + 1);
      }
      laneOf.set(s, lane);
    });
  for (const s of specs) {
    const a = byId.get(s.from);
    if (!a) continue;
    if (s.kind === "add") {
      const o = outPortOf(a);
      const at = { x: o.x + PLUS_REACH, y: o.y };
      plus = { after: a.id, at };
      wires.push({ id: a.id + ">add", from: a.id, to: "add", kind: "add", d: `M ${o.x} ${o.y} H ${at.x - PLUS_SIZE / 2}`, mid: { x: (o.x + at.x) / 2, y: o.y } });
      continue;
    }
    const b = byId.get(s.to);
    if (!b) continue;
    const extra = { ...(s.loop ? { loop: s.loop } : {}), ...(s.via ? { via: s.via } : {}), ...(s.done ? { done: true } : {}) };
    if (s.kind === "loop") {
      const [lo, hi] = spanOf(s);
      const between = all.filter((n) => {
        const i = order.get(n.id)!;
        return i > lo && i < hi && !isBranchRow(n.node);
      });
      const lane = laneOf.get(s) ?? 0;
      const r = loopWirePathG(a, b, between, lane);
      wires.push({ id: "loop:" + (s.via?.id || s.from + ">" + s.to), from: a.id, to: b.id, kind: "loop", d: r.d, mid: r.mid, lane, ...extra });
      continue;
    }
    // Sub-sessions hang under their parent the n8n way: a dashed bus down from
    // the parent's tile and a short drop into each one's top — no curve out of
    // the parent's port, and no curve back into the stop after (the Join's
    // "waiting for k of n" says what it waits for). The owner: "too much
    // weird verticality".
    if (s.kind === "join" && isBranchRow(a.node)) continue;
    if (s.kind === "fan" && isBranchRow(b.node)) {
      const ax = a.x + a.w / 2;
      const ay = a.y + TILE;
      const bx = b.x + b.w / 2;
      const bus = b.y - 18;
      const d = ax === bx ? `M ${ax} ${ay} V ${b.y - 2}` : `M ${ax} ${ay} V ${bus} H ${bx} V ${b.y - 2}`;
      wires.push({ id: a.id + ">" + b.id + ":fan", from: a.id, to: b.id, kind: "fan", d, mid: { x: (ax + bx) / 2, y: bus }, ...extra });
      continue;
    }
    const o = outPortOf(a);
    const i = inPortOf(b);
    wires.push({ id: a.id + ">" + b.id + (s.kind === "fan" || s.kind === "join" ? ":" + s.kind : ""), from: a.id, to: b.id, kind: s.kind, d: wirePath(o, i), mid: { x: (o.x + i.x) / 2, y: (o.y + i.y) / 2 }, ...extra });
  }
  const g: ChainGraph = { ...base, headNode, nodes, wires, plus, bbox: { x: 0, y: 0, w: 0, h: 0 } };
  g.bbox = chainBBox(g);
  return g;
}

/** Spots for the nodes that have no saved `pos` (contract: left→right after
 * the previous node at `PITCH_X`, branches `PITCH_Y` below their split).
 * `nodes` in line order (the head first, then the main row, then branches —
 * a branch's split is `node.branch.from`); pinned nodes keep their spot, the
 * rest follow the node before them, so a tail with no `pos` trails a moved
 * node. Each branch off the same split stacks one more row down, half a
 * pitch right of the split (between it and the node it joins). */
/** How many branches sit side by side under their parent before a new row
 * starts (the owner: a session's sub-sessions were "too much weird
 * verticality" one under another). */
export const BRANCH_COLS = 4;
/** How far below its parent a row of branches starts: a pitch, plus room for
 * a loop's wire that runs under the main row. */
export const BRANCH_Y = PITCH_Y + 50;

export function autoPlace(nodes: readonly GraphNode[]): GraphNode[] {
  const out: GraphNode[] = [];
  const byId = new Map<string, GraphNode>();
  const rows = new Map<string, number>();
  // Branches spread in rows of BRANCH_COLS under their parent, instead of
  // one tall column.
  let prev: GraphNode | null = null;
  for (const n of nodes) {
    let next: GraphNode;
    const split = n.node.branch ? byId.get(n.node.branch.from) : undefined;
    if (isBranchRow(n.node)) {
      const from = n.node.branch?.from || "";
      const k = rows.get(from) ?? 0;
      rows.set(from, k + 1);
      const at = split || prev;
      // Left-aligned under the parent (centring would push a head's first
      // children left of the chain's origin, outside its slot).
      const row = Math.floor(k / BRANCH_COLS);
      const col = k % BRANCH_COLS;
      const x = (at ? at.x : 0) + PITCH_X / 2 + col * PITCH_X;
      next = n.pinned ? { ...n } : { ...n, x, y: (at ? at.y : 0) + BRANCH_Y + PITCH_Y * row };
    } else {
      next = n.pinned ? { ...n } : { ...n, x: prev ? prev.x + PITCH_X : 0, y: prev ? prev.y : 0 };
      prev = next;
    }
    byId.set(next.id, next);
    out.push(next);
  }
  return out;
}

/** The graph with node `id` at `pos` (pinned there): the nodes after it that
 * have no `pos` follow, the wires re-route, the bbox follows — the drag's
 * preview, and what the chain looks like once the host saves the spot. */
export function moveNode(g: ChainGraph, id: string, pos: Pos): ChainGraph {
  const all = g.headNode ? [g.headNode, ...g.nodes] : g.nodes;
  if (!all.some((n) => n.id === id) || id === HEAD_ID) return g;
  const placed = autoPlace(all.map((n) => (n.id === id ? { ...n, x: Math.round(pos.x), y: Math.round(pos.y), pinned: true } : n)));
  const hn = g.headNode ? placed[0] : null;
  const specs: WireSpec[] = g.wires.map((w) => ({ from: w.from, to: w.to, kind: w.kind, ...(w.loop ? { loop: w.loop } : {}), ...(w.via ? { via: w.via } : {}), ...(w.done ? { done: true } : {}) }));
  return assemble({ key: g.key, flightId: g.flightId, head: g.head, insertFrom: g.insertFrom }, hn, hn ? placed.slice(1) : placed, specs);
}

/** A drag's spot snapped to the grid (`GRID_SNAP`), or free (Alt held). */
export function snapPos(p: Pos, free = false): Pos {
  if (free) return { x: Math.round(p.x), y: Math.round(p.y) };
  return { x: Math.round(p.x / GRID_SNAP) * GRID_SNAP, y: Math.round(p.y / GRID_SNAP) * GRID_SNAP };
}

/** Every node's box, the + button and the loop wires' reach (their run under
 * the boxes, the pill, the hooks' bulge), chain-local. */
export function chainBBox(g: Pick<ChainGraph, "nodes" | "headNode" | "wires" | "plus">): Rect {
  const all = g.headNode ? [g.headNode, ...g.nodes] : g.nodes;
  if (!all.length) return { x: 0, y: 0, w: 0, h: 0 };
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  const take = (x: number, y: number, x2: number, y2: number) => {
    x0 = Math.min(x0, x);
    y0 = Math.min(y0, y);
    x1 = Math.max(x1, x2);
    y1 = Math.max(y1, y2);
  };
  for (const n of all) take(n.x, n.y, n.x + n.w, n.y + n.h);
  if (g.plus) take(g.plus.at.x - PLUS_SIZE / 2, g.plus.at.y - PLUS_SIZE / 2, g.plus.at.x + PLUS_SIZE / 2, g.plus.at.y + PLUS_SIZE / 2);
  const byId = new Map(all.map((n) => [n.id, n]));
  for (const w of g.wires) {
    if (w.kind !== "loop") continue;
    const a = byId.get(w.from);
    const b = byId.get(w.to);
    if (!a || !b) continue;
    const o = outPortOf(a);
    const i = inPortOf(b);
    // The pill (~12 px half height) and the hooks (a cubic bulges ¾ of its handle).
    take(Math.min(i.x - LOOP_HOOK * 0.75, w.mid.x - 40), w.mid.y - 12, Math.max(o.x + LOOP_HOOK * 0.75, w.mid.x + 40), w.mid.y + 12);
  }
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

// --- Ports and wires ------------------------------------------------------------------

/** A node's out port (right middle of its tile) and in port (left middle). */
export function outPortOf(n: Pick<GraphNode, "x" | "y" | "w">): Pos {
  return { x: n.x + (n.w + TILE) / 2, y: n.y + TILE / 2 };
}
export function inPortOf(n: Pick<GraphNode, "x" | "y" | "w">): Pos {
  return { x: n.x + (n.w - TILE) / 2, y: n.y + TILE / 2 };
}
/** A port's spot by side (the host's rubber band starts there). */
export function portPos(n: Pick<GraphNode, "x" | "y" | "w">, side: "in" | "out"): Pos {
  return side === "in" ? inPortOf(n) : outPortOf(n);
}

/** A wire out→in: a cubic with horizontal tangents. */
export function wirePath(a: Pos, b: Pos): string {
  const dx = Math.max(40, Math.abs(b.x - a.x) / 2);
  return `M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`;
}

/** A loop's wire from `a`'s out port back to `b`'s in port, under both
 * tiles (and everything between, `others`): it leaves the out port to the
 * right, hooks down and back under the lowest of those boxes (labels
 * included), runs to under `b` and hooks up into its in port. `lane` drops
 * a nested loop's run `LOOP_LANE` px lower per level. The pill sits in the
 * middle of the run (`mid`). */
export function loopWirePathG(a: GraphNode, b: GraphNode, others: readonly GraphNode[], lane = 0): { d: string; mid: Pos } {
  const o = outPortOf(a);
  const i = inPortOf(b);
  // The session tile's text runs taller than its box (its chip, the Ready
  // controls): a loop round it (a Loop first on the chain) clears that too.
  const headRoom = a.id === HEAD_ID || b.id === HEAD_ID ? 60 : 0;
  const low = Math.max(a.y + a.h, b.y + b.h, ...others.map((n) => n.y + n.h)) + headRoom;
  const y = low + LOOP_CLEAR + lane * LOOP_LANE;
  const k = LOOP_HOOK;
  return {
    d: `M ${o.x} ${o.y} C ${o.x + k} ${o.y}, ${o.x + k} ${y}, ${o.x} ${y} L ${i.x} ${y} C ${i.x - k} ${y}, ${i.x - k} ${i.y}, ${i.x} ${i.y}`,
    mid: { x: (o.x + i.x) / 2, y },
  };
}

/** A session's colour: one of 8 hues, stable per title; neutral for "". */
export const CHAIN_HUES: readonly number[] = [265, 200, 150, 30, 330, 95, 180, 0];
export function chainColor(session: string | null | undefined): string {
  if (!session || session === "new") return "var(--muted)";
  let h = 0;
  for (const c of session) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return `hsl(${CHAIN_HUES[h % CHAIN_HUES.length]} 70% 60%)`;
}

const allOf = (g: Pick<ChainGraph, "nodes" | "headNode">) => (g.headNode ? [g.headNode, ...g.nodes] : g.nodes);

/** The node whose box holds `p` (chain-local), else null — the topmost
 * (last drawn) when boxes overlap. */
export function nodeAt(g: Pick<ChainGraph, "nodes" | "headNode">, p: Pos): GraphNode | null {
  const all = allOf(g);
  for (let k = all.length - 1; k >= 0; k--) {
    const n = all[k];
    if (p.x >= n.x && p.x <= n.x + n.w && p.y >= n.y && p.y <= n.y + n.h) return n;
  }
  return null;
}

/** The port within `slop` px of `p` (chain-local), else null; the nearest
 * when two are in reach. Only live ports (`inPort` / `outPort`). */
export function portAt(g: Pick<ChainGraph, "nodes" | "headNode">, p: Pos, slop = 10): { node: GraphNode; side: "in" | "out" } | null {
  let best: { node: GraphNode; side: "in" | "out" } | null = null;
  let bestD = Infinity;
  for (const n of allOf(g)) {
    for (const side of ["out", "in"] as const) {
      if (side === "out" ? !n.outPort : !n.inPort) continue;
      const q = portPos(n, side);
      const dd = Math.hypot(p.x - q.x, p.y - q.y);
      if (dd <= slop && dd < bestD) {
        best = { node: n, side };
        bestD = dd;
      }
    }
  }
  return best;
}

// --- Wiring ------------------------------------------------------------------------------

/** One end of a wire drag: a node of a chain, or a spot on the sky. */
export type WireEnd =
  | { chain: ChainGraph; node: GraphNode; side: "in" | "out" }
  | { sky: Pos };

/** What a dropped wire does (docs/flights.md "Round 18" → "Wiring"). Steps
 * are named by the diagram node (flight indexes: `node.node.index`). */
export type WireIntent =
  | { op: "move"; chain: string; step: DiagramNode; after: DiagramNode | null }
  | { op: "loop"; chain: string; from: DiagramNode; to: DiagramNode }
  | { op: "merge"; chain: string; after: DiagramNode | null; from: string }
  | { op: "handoff"; chain: string; from: DiagramNode; target: string }
  | { op: "attach"; session: string; chain: string }
  | { op: "palette"; chain: string; after: DiagramNode | null; at: Pos }
  | { op: "split"; chain: string; step: DiagramNode; at: Pos }
  | { op: "none" };

export interface WireIntentCtx {
  /** Shift held: a drop on another chain's head asks for a handoff. */
  shift?: boolean;
}

const NONE: WireIntent = { op: "none" };
const isHead = (n: GraphNode) => n.id === HEAD_ID;
const firstNodeOf = (c: ChainGraph) => c.nodes.find((n) => !isBranchRow(n.node)) || null;
const stopsOnlyChain = (c: ChainGraph) => c.head.kind !== "session";
/** The node / socket that starts a stops-only chain (its first node, or its
 * trigger tile) — where another chain's wire lands to take it over. */
const isChainStart = (c: ChainGraph, n: GraphNode) => isHead(n) || n.id === firstNodeOf(c)?.id;
const insertFromOf = (c: ChainGraph) => c.insertFrom ?? 0;

/** What dropping a wire from `src` on `dst` does. Every intent names chains
 * by their KEY (`ChainGraph.key`: a session title or `"flight:<id>"`); a
 * handoff's `target` is `"flight:<id>"` (the API's own form). Only targets
 * that can still change light: a moved stop must be `editable`, a slot after
 * a node must be open (`canFollow`), a split's stops must all be pending. */
export function wireIntent(src: WireEnd, dst: WireEnd, ctx: WireIntentCtx = {}): WireIntent {
  if ("sky" in src) return NONE;
  const A = src.chain;
  const X = src.node;

  // An in port's wire end pulled off into empty sky: split there.
  if (src.side === "in") {
    if (!("sky" in dst) || isHead(X) || !isStopish(X.node) || !X.node.editable) return NONE;
    if (stopsOnlyChain(A) && X.id === firstNodeOf(A)?.id) return NONE; // the whole chain: nothing to split
    return { op: "split", chain: A.key, step: X.node, at: dst.sky };
  }

  // From an out port.
  if (!X.outPort) return NONE;
  if (isHead(X)) {
    if (A.head.kind !== "session") return NONE;
    if ("sky" in dst) return { op: "palette", chain: A.key, after: null, at: dst.sky };
    const B = dst.chain;
    if (B.key === A.key || !stopsOnlyChain(B) || !isChainStart(B, dst.node) || !A.head.session) return NONE;
    return { op: "attach", session: A.head.session, chain: B.key };
  }
  if (!canFollow(X.node, insertFromOf(A))) return NONE;
  if ("sky" in dst) return { op: "palette", chain: A.key, after: X.node, at: dst.sky };

  const B = dst.chain;
  const Y = dst.node;
  if (B.key === A.key) {
    if (isHead(Y) || Y.node.index === undefined || X.node.index === undefined || isBranchRow(Y.node)) return NONE;
    if (Y.node.index > X.node.index) {
      // Y right after X: a stop that can change, not already there.
      if (Y.node.kind !== "stop" || !Y.node.editable) return NONE;
      if (Y.node.index === loopSlotAfter(X.node)) return NONE;
      return { op: "move", chain: A.key, step: Y.node, after: X.node };
    }
    // At or before X (X itself too): a Loop after X back to Y.
    if (!loopable(X.node) || !loopable(Y.node)) return NONE;
    return { op: "loop", chain: A.key, from: X.node, to: Y.node };
  }

  // Another chain: only a stops-only chain, by where it starts.
  if (!stopsOnlyChain(B) || !isChainStart(B, Y)) return NONE;
  const asks = !!ctx.shift || B.head.kind === "trigger";
  if (asks) {
    if (A.head.kind !== "session" || !B.flightId) return NONE;
    return { op: "handoff", chain: A.key, from: X.node, target: "flight:" + B.flightId };
  }
  if (isHead(Y)) return NONE;
  return { op: "merge", chain: A.key, after: X.node, from: B.key };
}

/** Every node of `chains` a wire from `src` could land on (`wireIntent` not
 * `none`), by chain key — the host lights them (each chain's set is its
 * `SkyChainProps.targets`) while it drags. Chains with none are left out. */
export function wireTargets(src: WireEnd, chains: readonly ChainGraph[], ctx: WireIntentCtx = {}): Map<string, Set<string>> {
  const out = new Map<string, Set<string>>();
  for (const c of chains)
    for (const n of allOf(c)) {
      if (wireIntent(src, { chain: c, node: n, side: "in" }, ctx).op === "none") continue;
      if (!out.has(c.key)) out.set(c.key, new Set());
      out.get(c.key)!.add(n.id);
    }
  return out;
}
