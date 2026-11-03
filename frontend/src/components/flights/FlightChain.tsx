/** A session's flight as an n8n-style chain outside the Sky — the pane's
 * Flight tab (the owner: "the flight tab should also have the same style
 * blocks"). The same SkyChain the Sky draws: the session tile heads it, its
 * stops float at their saved spots (drag any tile; sub-sessions too), the
 * right-click menu, the Loop pill (click = the Loop, drag = re-aim it). Wires
 * are dragged within the chain (there is no sky to reach another one): onto a
 * later stop moves it right after, onto an earlier one (or itself) loops back
 * to it, onto empty space opens the palette after it. */
import { useMemo, useRef, useState, type JSX, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { birdPerch, lineWhere, type DiagramNode } from "../../lib/flightDiagram";
import { firstLine } from "../../lib/format";
import { deriveSessionState } from "../../lib/sessionState";
import { chainColor, chainGraph, wireIntent, HEAD_ID, type ChainHead, type GraphNode } from "../../lib/skyGraph";
import { windowName } from "../../lib/windowName";
import type { StepKindDef } from "../../lib/flights";
import { ReadyHead } from "./ReadyHead";
import { SkyChain } from "./SkyChain";
import { pillAimer, useChainMenu } from "./useChainMenu";
import { useNodeMoves } from "./useSkyWiring";
import type { useSessionLine } from "./useSessionLine";

type Line = ReturnType<typeof useSessionLine>;

export function FlightChain({ title, line, kinds, label }: { title: string; line: Line; kinds: readonly StepKindDef[]; label: string }): JSX.Element {
  const row = line.row;
  const ss = deriveSessionState(row || {});
  const flightId = line.flight?.id || "";
  const moves = useNodeMoves(line.flight?.id, line.graphDiagram, title);
  const sessionNode = useMemo(() => moves.diagram.nodes.find((n) => n.kind === "session" && n.row === "main"), [moves.diagram]);
  const where = row ? lineWhere(line.diagram) : "";
  // While ready, ReadyHead says so (and how it starts); the sub stays the
  // session's own line, never a third copy of it.
  const headSub = where || (row ? firstLine(row.task_headline || row.task || "") : "");
  const head: ChainHead = useMemo(
    () => ({ kind: "session", title: windowName(title), sub: headSub, session: title, color: chainColor(title), node: sessionNode }),
    [title, headSub, sessionNode]
  );
  const graph = useMemo(() => chainGraph(moves.diagram, { head, key: title, flightId }), [moves.diagram, head, title, flightId]);
  const gc = line.graphCanvas;
  const chainMenu = useChainMenu(gc, flightId);
  const onPillDown = pillAimer(graph, gc.onLoopTo);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [band, setBand] = useState<{ x1: number; y1: number; x2: number; y2: number; over: boolean } | null>(null);

  // A wire dragged out of a tile's port, within this chain.
  const onPortDown = (src: GraphNode, side: "in" | "out", e: ReactPointerEvent<HTMLElement>) => {
    if (side !== "out" || e.button !== 0) return;
    e.preventDefault();
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const x1 = r.left + r.width / 2;
    const y1 = r.top + r.height / 2;
    const sx = e.clientX;
    const sy = e.clientY;
    let moved = false;
    const nodeAt = (x: number, y: number): GraphNode | null => {
      const el = document.elementFromPoint(x, y)?.closest("[data-node]");
      if (!el || !rootRef.current?.contains(el)) return null;
      const id = el.getAttribute("data-node");
      return graph.nodes.find((n) => n.id === id) || null;
    };
    const intentFor = (x: number, y: number) => {
      const dst = nodeAt(x, y);
      return dst ? wireIntent({ chain: graph, node: src, side: "out" }, { chain: graph, node: dst, side: "in" }) : null;
    };
    const move = (ev: PointerEvent) => {
      if (!moved && Math.hypot(ev.clientX - sx, ev.clientY - sy) < 4) return;
      moved = true;
      const it = intentFor(ev.clientX, ev.clientY);
      setBand({ x1, y1, x2: ev.clientX, y2: ev.clientY, over: !!it && it.op !== "none" });
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", stop);
      setBand(null);
    };
    const up = (ev: PointerEvent) => {
      stop();
      if (!moved) return;
      const it = intentFor(ev.clientX, ev.clientY);
      if (!it) {
        // Empty space: a stop right after this one.
        if (src.id === HEAD_ID) line.togglePalette(true);
        else line.paletteAfter(src.node, null);
        return;
      }
      if (it.op === "loop") gc.onLoopFrom?.(it.from, it.to);
      else if (it.op === "move" && it.step.index !== undefined) {
        const from = it.step.index;
        const after = it.after?.index ?? -1;
        const to = after < from ? after + 1 : after;
        gc.onMove?.(from, to);
      }
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", stop);
  };

  const onOpen = (n: DiagramNode) => gc.onOpen?.(n);
  return (
    <div ref={rootRef} className="fl-chain-view sky-own-scroll" aria-label={label}>
      <SkyChain
        graph={graph}
        zoom={1}
        selected={gc.selected}
        bird={birdPerch(moves.diagram)}
        kinds={kinds}
        onSelect={gc.onSelect}
        onEdit={gc.onEdit}
        onOpen={onOpen}
        onRemove={gc.onRemove}
        onLoopPill={(via) => gc.onEdit?.(via)}
        onPillDown={onPillDown}
        onMenu={chainMenu.onMenu}
        onNodeMove={moves.move}
        onPortDown={onPortDown}
        onPlus={(after) => (after && after.id !== HEAD_ID ? line.paletteAfter(after.node, null) : line.togglePalette(true))}
        renderInBox={gc.renderInBox ? (n) => gc.renderInBox!(n) as ReactNode : undefined}
        renderHead={() =>
          line.flight?.state === "ready" ? (
            <ReadyHead flight={line.flight} />
          ) : (
          <span className={"sky-head-chip fl-box-chip tone-" + ss.tone} title={ss.hint}>
            {ss.label}
          </span>
        )}
      />
      {chainMenu.menu}
      {band &&
        createPortal(
          <svg className={"sky-connect" + (band.over ? " is-over" : "")} aria-hidden="true" focusable="false">
            <path d={`M ${band.x1} ${band.y1} C ${band.x1 + 60} ${band.y1}, ${band.x2 - 60} ${band.y2}, ${band.x2} ${band.y2}`} />
            <circle cx={band.x2} cy={band.y2} r={5} />
          </svg>,
          document.body
        )}
    </div>
  );
}
