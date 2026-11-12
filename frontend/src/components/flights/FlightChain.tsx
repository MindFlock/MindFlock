/** A session's flight as an n8n-style chain outside the Sky — the pane's
 * Flight tab (the owner: "the flight tab should also have the same style
 * blocks"). The same SkyChain the Sky draws: the session tile heads it, its
 * stops float at their saved spots (drag any tile; sub-sessions too), the
 * right-click menu, the Loop pill (click = the Loop, drag = re-aim it). Wires
 * are dragged within the chain (there is no sky to reach another one): onto a
 * later stop moves it right after, onto an earlier one (or itself) loops back
 * to it, onto empty space opens the palette after it.
 *
 * Round 20 (docs/flights.md "Round 20" A): drawn as the Sky draws it — the
 * session a FRAME round the chain (its bar: name · branch · state), the
 * agent the chain's first tile (its name, what it is doing, its chip, the
 * bird; a click or Open terminal goes to the pane's Agent tab; right-click:
 * Open terminal, Send a prompt…, Stop), sub-sessions small frames under it. */
import { useEffect, useMemo, useRef, useState, type JSX, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { birdPerch, lineWhere, whereBesideChip, type DiagramNode } from "../../lib/flightDiagram";
import { deriveSessionState } from "../../lib/sessionState";
import { chainColor, chainGraph, wireIntent, HEAD_ID, type ChainHead, type GraphNode } from "../../lib/skyGraph";
import { windowName } from "../../lib/windowName";
import type { StepKindDef } from "../../lib/flights";
import { ReadyHead } from "./ReadyHead";
import { SkyChain, agentDoing, agentTitle, hangFromAgent, subFrameBoxes, subFramesOf } from "./SkyChain";
import { agentMenuItems, pillAimer, useAgentMenu, useChainMenu } from "./useChainMenu";
import { useFlights, useInstances } from "../../state/queries";
import { selectSession } from "../../lib/sessionActions";
import { useUi } from "../../state/store";
import { useNodeMoves } from "./useSkyWiring";
import { currentNodeId } from "./FlightStrip";
import type { useSessionLine } from "./useSessionLine";

type Line = ReturnType<typeof useSessionLine>;

export function FlightChain({ title, line, kinds, label }: { title: string; line: Line; kinds: readonly StepKindDef[]; label: string }): JSX.Element {
  const row = line.row;
  const ss = deriveSessionState(row || {});
  const flightId = line.flight?.id || "";
  const moves = useNodeMoves(line.flight?.id, line.graphDiagram, title);
  const sessionNode = useMemo(() => moves.diagram.nodes.find((n) => n.kind === "session" && n.row === "main"), [moves.diagram]);
  const where = row ? lineWhere(line.diagram) : "";
  // The chip already says "Needs you: Review": no "needs you at Review" too.
  const shownWhere = whereBesideChip(where, ss);
  // While ready, ReadyHead says so; the sub never repeats it.
  const agent = agentTitle(row?.provider || row?.program, row?.program);
  const headSub = agentDoing(row);
  const head: ChainHead = useMemo(
    () => ({ kind: "session", title: agent, sub: headSub, session: title, color: chainColor(title), node: sessionNode }),
    [agent, title, headSub, sessionNode]
  );
  // Sub-sessions draw as small frames, as on the Sky.
  const rows = useInstances().data;
  const flights = useFlights().data;
  const subFrames = useMemo(() => subFramesOf(rows || [], title, flights, kinds), [rows, title, flights, kinds]);
  // The session's behaviors: the chain's last tile, as on the Sky
  // (docs/behaviors.md §1; on `line.graphDiagram`) — its card opens in the
  // tile's place here too.
  const behaviors = line.behaviors;
  const big = useMemo(() => ({ ...subFrameBoxes(moves.diagram.nodes, subFrames), ...behaviors.big(moves.diagram) }), [moves.diagram, subFrames, behaviors.big]);
  const graph = useMemo(() => chainGraph(hangFromAgent(moves.diagram), { head, key: title, flightId, big }), [moves.diagram, head, title, flightId, big]);
  const gc = line.graphCanvas;
  const chainMenu = useChainMenu(gc, flightId || line.landedId, { steps: line.flight?.steps, kinds, ended: line.landed });
  const agentMenu = useAgentMenu();
  const onPillDown = pillAimer(graph, gc.onLoopTo);
  const rootRef = useRef<HTMLDivElement | null>(null);
  // A stop waiting on you (or halted) comes into view — in a narrow pane the
  // chain scrolls to it when it starts waiting, so its ask is never off to
  // the side (parkedStopId: lib/flightDiagram's node id, "" when none).
  const parkedId = parkedStopId(line.flight, moves.diagram);
  useEffect(() => {
    if (!parkedId) return;
    const raf = requestAnimationFrame(() => scrollIntoChain(rootRef.current, parkedId));
    return () => cancelAnimationFrame(raf);
  }, [parkedId]);
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
        // Empty space: a stop beside this one's next when it may run in
        // parallel (round 20), else a stop right after this one.
        const sky = wireIntent({ chain: graph, node: src, side: "out" }, { sky: { x: 0, y: 0 } });
        if (sky.op === "parallel" && line.paletteParallel) line.paletteParallel(sky.with, null);
        else if (src.id === HEAD_ID) line.togglePalette(true);
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

  // The agent's terminal is this pane's Agent tab; a sub-session's, its own.
  const openTerminal = (t: string) => {
    if (t === title) useUi.getState().setLastTab(title, "agent");
    else selectSession(t);
  };
  const onOpen = (n: DiagramNode) => {
    if (behaviors.onOpen(n)) return;
    if (n.kind === "session") openTerminal(title);
    else gc.onOpen?.(n);
  };
  const onMenu = (n: DiagramNode, at: { clientX: number; clientY: number }) => {
    if (behaviors.onMenu(n, at)) return;
    const t = n.kind === "session" ? title : (n.kind === "child" || n.kind === "branch") && n.session ? n.session : "";
    if (!t) return chainMenu.onMenu(n, at);
    const r = t === title ? row : rows?.find((x) => x.title === t);
    agentMenu.open(t, at, agentMenuItems(r, { openTerminal: () => openTerminal(t), compose: () => agentMenu.compose(t, at) }));
  };
  // The frame holds every tile: one dragged left of / above the origin
  // moves the chain in by as much.
  const shift = { marginLeft: -Math.min(0, Math.floor(graph.bbox.x)), marginTop: -Math.min(0, Math.floor(graph.bbox.y)) };
  return (
    <div ref={rootRef} className={"fl-chain-view sky-own-scroll" + (behaviors.frameClass ? " " + behaviors.frameClass : "")} aria-label={label}>
      <div className="fl-chain-frame" style={{ ["--sky-chain-color" as string]: head.color }}>
        <div className="fl-chain-frame-bar">
          <div className="fl-chain-frame-bar-in">
          <span className="fl-chain-frame-name">{windowName(title)}</span>
          {row?.branch && (
            <span className="fl-chain-frame-branch" title={"Branch " + row.branch}>
              {row.branch}
            </span>
          )}
          {row && (
            <span className={"fl-box-chip fl-chain-frame-chip tone-" + ss.tone} title={ss.hint}>
              {ss.label}
            </span>
          )}
          {behaviors.mark?.tone === "you" && ss.tone !== "attention" && (
            <span className="fl-box-chip fl-chain-frame-chip tone-attention skl-bh-needs" title="A behavior waits on you — open the Behaviors stop to answer it">
              Needs you
            </span>
          )}
          {shownWhere && (
            <span className="fl-chain-frame-where" title={shownWhere}>
              {shownWhere}
            </span>
          )}
          </div>
        </div>
        <div className="fl-chain-frame-body" style={shift}>
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
            onMenu={onMenu}
            subFrames={subFrames}
            onNodeMove={moves.move}
            onPortDown={onPortDown}
            onPlus={(after) => (after && after.id !== HEAD_ID ? line.paletteAfter(after.node, null) : line.togglePalette(true))}
            renderOpen={behaviors.renderOpen}
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
        </div>
      </div>
      {chainMenu.menu}
      {agentMenu.menu}
      {behaviors.menu}
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

/** The node id of the stop a flight waits at on the person (or is halted
 * at), as `stateKey` too: "<id>:<state>" changes when it starts waiting
 * again; "" when it isn't parked. */
export function parkedStopId(flight: Parameters<typeof currentNodeId>[1], d: Parameters<typeof currentNodeId>[0]): string {
  if (!flight || (flight.state !== "waiting_you" && flight.state !== "halted")) return "";
  const id = currentNodeId(d, flight);
  return id ? id + ":" + flight.state + ":" + flight.cursor : "";
}

/** Scroll the chain view (its own scroller) just enough to show the tile of
 * `key` (parkedStopId's), with a margin — never the page or the pane. */
export function scrollIntoChain(root: HTMLElement | null, key: string): void {
  if (!root || !key) return;
  const id = key.split(":").slice(0, -2).join(":");
  const tile = Array.from(root.querySelectorAll<HTMLElement>("[data-diagram-node]")).find((el) => el.getAttribute("data-diagram-node") === id);
  if (!tile) return;
  const v = root.getBoundingClientRect();
  const r = tile.getBoundingClientRect();
  const pad = 16;
  let dx = 0;
  let dy = 0;
  if (r.right + pad > v.right) dx = r.right + pad - v.right;
  if (r.left - dx - pad < v.left) dx = r.left - pad - v.left;
  if (r.bottom + pad > v.bottom) dy = r.bottom + pad - v.bottom;
  if (r.top - dy - pad < v.top) dy = r.top - pad - v.top;
  if (dx || dy) root.scrollBy({ left: dx, top: dy, behavior: "smooth" });
}
