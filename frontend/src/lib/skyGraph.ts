/** Round 18 — free-floating chains (docs/flights.md "Round 18"): the Sky draws
 * each flight as a CHAIN of free-floating tiles (n8n-style) instead of a row.
 * This module is the pure model: a chain's head + nodes with chain-local
 * positions (a step's saved `pos`, else `autoPlace`), its ports and wires, and
 * `wireIntent` — what dropping a wire somewhere means. SEAM: the types are the
 * contract; the function bodies are placeholders the graph worker replaces.
 */

import type { Diagram, DiagramNode, LoopMark } from "./flightDiagram";
import type { Pos, Rect } from "./skyLayout";

/** Tile metrics (plane px). The tile is the square; the node's box adds the
 * label area under it. */
export const TILE = 64;
export const NODE_W = 132;
export const NODE_H = 118;
/** Left→right pitch of auto-placed nodes, and the row gap for branches. */
export const PITCH_X = 150;
export const PITCH_Y = 150;
export const GRID_SNAP = 8;

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
}

export interface ChainGraphOpts {
  head: ChainHead;
  /** The line key and its flight. */
  key: string;
  flightId: string;
}

/** The chain of a diagram (sessionDiagram / groundedDiagram output, loops
 * lifted): the head, every main-row stop/group/branch node placed (saved
 * `step.pos` else `autoPlace`), the wires routed between ports. */
export function chainGraph(d: Diagram, opts: ChainGraphOpts): ChainGraph {
  // SEAM placeholder: left→right, no ports logic, straight wires.
  const headNode: GraphNode | null =
    opts.head.kind === "none"
      ? null
      : { id: "head", node: opts.head.node || ({ id: "head", kind: "session", row: "main", title: opts.head.title, sub: opts.head.sub, state: "pending", icon: "session" } as DiagramNode), x: 0, y: 0, w: NODE_W, h: NODE_H, pinned: false, inPort: false, outPort: true };
  const stops = d.nodes.filter((n) => n.row === "main" && n.kind !== "session" && n.kind !== "add");
  const start = headNode ? 1 : 0;
  const nodes: GraphNode[] = stops.map((n, i) => {
    const pos = n.step?.pos;
    return { id: n.id, node: n, x: pos ? pos.x : (i + start) * PITCH_X, y: pos ? pos.y : 0, w: NODE_W, h: NODE_H, pinned: !!pos, inPort: true, outPort: true };
  });
  const all = headNode ? [headNode, ...nodes] : nodes;
  const wires: GraphWire[] = [];
  for (let i = 1; i < all.length; i++) {
    const a = all[i - 1];
    const b = all[i];
    wires.push({ id: a.id + ">" + b.id, from: a.id, to: b.id, kind: i === 1 && headNode ? "head" : "line", d: wirePath(outPortOf(a), inPortOf(b)), mid: { x: (a.x + b.x) / 2, y: a.y } });
  }
  const r = Math.max(0, ...all.map((n) => n.x + n.w));
  const btm = Math.max(0, ...all.map((n) => n.y + n.h));
  return { key: opts.key, flightId: opts.flightId, head: opts.head, headNode, nodes, wires, bbox: { x: 0, y: 0, w: r, h: btm } };
}

/** Spots for the nodes that have no saved `pos` (contract: left→right after
 * the previous node at `PITCH_X`, branches `PITCH_Y` below their split). */
export function autoPlace(nodes: readonly GraphNode[]): GraphNode[] {
  return nodes.slice();
}

/** A node's out port (right middle of its tile) and in port (left middle). */
export function outPortOf(n: Pick<GraphNode, "x" | "y" | "w">): Pos {
  return { x: n.x + (n.w + TILE) / 2, y: n.y + TILE / 2 };
}
export function inPortOf(n: Pick<GraphNode, "x" | "y" | "w">): Pos {
  return { x: n.x + (n.w - TILE) / 2, y: n.y + TILE / 2 };
}

/** A wire out→in: a cubic with horizontal tangents. */
export function wirePath(a: Pos, b: Pos): string {
  const dx = Math.max(40, Math.abs(b.x - a.x) / 2);
  return `M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`;
}

/** A loop's wire from `a`'s out port back to `b`'s in port, under both
 * tiles (and everything between, `others`). */
export function loopWirePathG(a: GraphNode, b: GraphNode, others: readonly GraphNode[]): { d: string; mid: Pos } {
  void others;
  const o = outPortOf(a);
  const i = inPortOf(b);
  const y = Math.max(a.y, b.y) + TILE + 30;
  return { d: `M ${o.x} ${o.y} H ${o.x + 16} V ${y} H ${i.x - 16} V ${i.y} H ${i.x}`, mid: { x: (o.x + i.x) / 2, y } };
}

/** A session's colour: one of 8 hues, stable per title; neutral for "". */
export function chainColor(session: string | null | undefined): string {
  if (!session) return "var(--muted)";
  const hues = [265, 200, 150, 30, 330, 95, 180, 0];
  let h = 0;
  for (const c of session) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return `hsl(${hues[h % hues.length]} 70% 60%)`;
}

/** The node whose box holds `p` (chain-local), else null. */
export function nodeAt(g: Pick<ChainGraph, "nodes" | "headNode">, p: Pos): GraphNode | null {
  const all = g.headNode ? [g.headNode, ...g.nodes] : g.nodes;
  return all.find((n) => p.x >= n.x && p.x <= n.x + n.w && p.y >= n.y && p.y <= n.y + n.h) || null;
}

/** The port within `slop` px of `p` (chain-local), else null. */
export function portAt(g: Pick<ChainGraph, "nodes" | "headNode">, p: Pos, slop = 10): { node: GraphNode; side: "in" | "out" } | null {
  const all = g.headNode ? [g.headNode, ...g.nodes] : g.nodes;
  for (const n of all) {
    const o = outPortOf(n);
    if (n.outPort && Math.hypot(p.x - o.x, p.y - o.y) <= slop) return { node: n, side: "out" };
    const i = inPortOf(n);
    if (n.inPort && Math.hypot(p.x - i.x, p.y - i.y) <= slop) return { node: n, side: "in" };
  }
  return null;
}

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

/** What dropping a wire from `src` on `dst` does. */
export function wireIntent(src: WireEnd, dst: WireEnd, ctx: WireIntentCtx = {}): WireIntent {
  void src;
  void dst;
  void ctx;
  return { op: "none" };
}
