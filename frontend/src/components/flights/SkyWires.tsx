/** Round 18 — what the Sky draws BETWEEN chains (docs/flights.md "Round
 * 18"), on one SVG layer in plane px over the lines: the rubber band of a
 * wire being drawn (useSkyWiring), and the HANDOFF wires — dashed, in the
 * source session's colour — from a Handoff stop's out port to the head of
 * the chain it starts (the stops-only chain it names; once started, the
 * started flight's chain). A chain's own wires are SkyChain's.
 *
 * `ChainPanels`: a line's palette or stop dialog, floating beside the node
 * it was opened from (or at the spot a wire was dropped), not under a row. */

import type { JSX, ReactNode } from "react";
import { chainColor, NODE_W, TILE, wirePath, type ChainGraph, type GraphNode } from "../../lib/skyGraph";
import type { HandoffLink } from "../../lib/groundedLines";
import type { Pos } from "../../lib/skyLayout";
import { chainNode, chainNodes, portOnPlane, posStepOf, type PlacedChain, type WireBand } from "./useSkyWiring";

/** One handoff wire, plane px. */
export interface HandoffWire {
  id: string;
  d: string;
  color: string;
  started: boolean;
  from: Pos;
  to: Pos;
}

/** The node of a chain a step is drawn in: its own tile, or its group's. */
export function nodeOfStep(g: Pick<ChainGraph, "headNode" | "nodes">, stepId: string): GraphNode | null {
  for (const n of g.nodes) {
    if (posStepOf(n.node)?.id === stepId) return n;
    if (n.node.group?.steps.some((s) => s.id === stepId)) return n;
  }
  return null;
}

/** Where a chain is entered: its head's in port side (the head's left
 * middle), else its first node's in port. */
function entryOf(c: PlacedChain): Pos | null {
  const n = c.graph.headNode || c.graph.nodes.find((x) => x.node.row === "main") || c.graph.nodes[0];
  return n ? portOnPlane(c, n, "in") : null;
}

/** Every handoff wire whose two ends are on the plane. */
export function handoffWires(links: readonly HandoffLink[], chains: Iterable<PlacedChain>): HandoffWire[] {
  const byFlight = new Map<string, PlacedChain>();
  for (const c of chains) if (c.graph.flightId) byFlight.set(c.graph.flightId, c);
  const out: HandoffWire[] = [];
  for (const l of links) {
    const src = byFlight.get(l.fromFlight);
    const dst = byFlight.get(l.target);
    if (!src || !dst || src === dst) continue;
    const n = nodeOfStep(src.graph, l.fromStep);
    const to = entryOf(dst);
    if (!n || !to) continue;
    const from = portOnPlane(src, n, "out");
    out.push({ id: l.fromFlight + ":" + l.fromStep, d: wirePath(from, to), color: chainColor(l.fromSession), started: l.started, from, to });
  }
  return out;
}

export interface SkyWiresProps {
  band: WireBand | null;
  wires: readonly HandoffWire[];
}

/** The layer: overflow visible from the plane's origin, never in the way of
 * a press. */
export function SkyWires({ band, wires }: SkyWiresProps): JSX.Element | null {
  if (!band && !wires.length) return null;
  return (
    <svg className="sky-xwires" aria-hidden="true" focusable="false" width={1} height={1}>
      {wires.map((w) => (
        <g key={w.id} className={"sky-xwire" + (w.started ? " is-started" : "")} data-handoff={w.id} style={{ color: w.color }}>
          <path className="sky-xwire-path" d={w.d} />
          <circle className="sky-xwire-end" cx={w.to.x} cy={w.to.y} r={3.5} />
        </g>
      ))}
      {band && (
        <g className={"sky-band" + (band.over ? " is-over" : "") + (band.side === "in" ? " is-split" : "")}>
          <path className="sky-band-path" d={band.side === "in" ? wirePath(band.to, band.from) : wirePath(band.from, band.to)} />
          <circle className="sky-band-end" cx={band.to.x} cy={band.to.y} r={5} />
          {band.hint && (
            // By the pointer, below-right of it (em: screen-sized with the
            // zoom-counter-scaled font, SkyChain.css `.sky-band-hint`).
            <text className="sky-band-hint" x={band.to.x} y={band.to.y} dx="1.1em" dy="2.1em" data-hint="">
              {band.hint}
            </text>
          )}
        </g>
      )}
    </svg>
  );
}

/** Where a line's open palette / dialog floats, chain-local: right of its
 * node's tile (the end of the line for "", the spot asked for when given). */
export function panelSpot(g: Pick<ChainGraph, "headNode" | "nodes">, anchor: string, pos: Pos | null): Pos {
  if (pos) return { x: pos.x + 24, y: pos.y - TILE / 2 };
  const all = chainNodes(g);
  const n = anchor ? chainNode(g, anchor) || all.find((x) => x.node.id === anchor) : all[all.length - 1];
  if (!n) return { x: 0, y: 0 };
  return { x: n.x + (n.w + TILE) / 2 + (anchor ? 16 : 52), y: n.y - 6 };
}

export interface ChainPanelsProps {
  graph: Pick<ChainGraph, "headNode" | "nodes">;
  /** lib: the line hook's `anchor` (null: nothing open). */
  anchor: string | null;
  anchorPos: Pos | null;
  children: ReactNode;
}

/** The floating panel beside a node. */
export function ChainPanels({ graph, anchor, anchorPos, children }: ChainPanelsProps): JSX.Element | null {
  if (anchor === null) return null;
  const at = panelSpot(graph, anchor, anchorPos);
  return (
    <div className="skl-panels skl-hang sky-own-scroll" data-no-drag data-anchor={anchor || "end"} style={{ left: at.x, top: at.y, minWidth: NODE_W * 3 }}>
      {children}
    </div>
  );
}
