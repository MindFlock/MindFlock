/** Round 18 — one chain in the Sky, n8n-style (docs/flights.md "Round 18"):
 * its head tile, its stop tiles at their free positions, the wires between
 * their ports. Presentational plus node drags: the HOST (SkyView) runs every
 * wire drag (`onPortDown`) so a wire can reach another chain, and persists a
 * moved node (`onNodeMove`). SEAM: the props are the contract; the body is a
 * placeholder the graph worker replaces.
 */

import type { JSX, PointerEvent, ReactNode } from "react";
import type { BirdPerch, DiagramNode } from "../../lib/flightDiagram";
import type { ChainGraph, GraphNode } from "../../lib/skyGraph";
import type { Pos } from "../../lib/skyLayout";

export interface SkyChainProps {
  graph: ChainGraph;
  /** The sky's zoom (pills and handles stay screen-sized). */
  zoom: number;
  /** The selected node (a diagram node id) and the Sky's multi-selection. */
  selected?: string | null;
  highlighted?: ReadonlySet<string>;
  /** Nodes lit as a wire's possible drop (the host's drag under way), and
   * the one under the pointer. */
  targets?: ReadonlySet<string>;
  over?: string | null;
  bird?: BirdPerch | null;
  /** A tile pressed (select), double-clicked / Enter (edit), a head or a
   * child opened, a stop removed (Delete), a node's menu (right-click, at
   * the pointer in client px). */
  onSelect?(n: DiagramNode): void;
  onEdit?(n: DiagramNode): void;
  onOpen?(n: DiagramNode): void;
  onRemove?(n: DiagramNode): void;
  onMenu?(n: DiagramNode, at: { clientX: number; clientY: number }): void;
  /** A loop pill pressed (open the Loop) — `via` is the Loop node. */
  onLoopPill?(via: DiagramNode): void;
  /** A node dragged to a new spot (chain-local, snapped) — on release. */
  onNodeMove?(node: GraphNode, pos: Pos): void;
  /** A port (or the trailing +) pressed: the host takes the drag from here. */
  onPortDown?(node: GraphNode, side: "in" | "out", e: PointerEvent<HTMLElement>): void;
  /** The trailing + clicked without a drag: the palette after the last node. */
  onPlus?(after: GraphNode | null): void;
  /** Extra content in or under a tile (a perch's Done / Skip …). */
  renderInBox?(n: DiagramNode): ReactNode;
  /** The head tile's own content (the session's chip, ⋯) — the host's. */
  renderHead?(): ReactNode;
  className?: string;
}

/** SEAM placeholder: tiles at their spots, no wires, no drags. */
export function SkyChain(p: SkyChainProps): JSX.Element {
  const g = p.graph;
  const all = g.headNode ? [g.headNode, ...g.nodes] : g.nodes;
  return (
    <div className={"sky-chain" + (p.className ? " " + p.className : "")} style={{ position: "relative", width: g.bbox.w, height: g.bbox.h }} data-chain={g.key}>
      {all.map((n) => (
        <div key={n.id} className="sky-node" data-node={n.id} style={{ position: "absolute", transform: `translate(${n.x}px, ${n.y}px)`, width: n.w }} onClick={() => p.onSelect?.(n.node)}>
          {n.id === "head" ? p.renderHead?.() ?? g.head.title : n.node.title}
        </div>
      ))}
    </div>
  );
}
