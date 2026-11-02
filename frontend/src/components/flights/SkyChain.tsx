/** Round 18 — one chain in the Sky, n8n-style (docs/flights.md "Round 18"):
 * its head tile, its stop tiles at their free positions, the wires between
 * their ports. Presentational plus node drags: the HOST (SkyView) runs every
 * wire drag (`onPortDown`) so a wire can reach another chain, and persists a
 * moved node (`onNodeMove`).
 *
 * The root is positioned by the host at the chain's origin; nodes sit at
 * their chain-local spots (lib/skyGraph), so a node dragged left of / above
 * the origin overflows the root (`overflow: visible`) — `graph.bbox` is the
 * true extent. Every tile is a `role=listitem` (focusable: Enter edits,
 * Delete removes, the context-menu key opens its menu); its ports carry
 * `data-port="in|out"` and `data-node` so the host can hit-test a drop.
 *
 * A node drag: pointer capture on the tile, moved 4 px (screen) before it
 * counts; the preview follows the pointer at chain-local delta = client
 * delta / zoom, snapped to `GRID_SNAP` (Alt: free); release calls
 * `onNodeMove`, Esc cancels. A press without movement is a click
 * (`onSelect`; a head / child / flown tile: `onOpen`). */

import { useMemo, useRef, useState, type JSX, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import type { BirdPerch, DiagramNode } from "../../lib/flightDiagram";
import { loopIdlePill, loopPill, loopPillTitle } from "../../lib/flightDiagram";
import type { StepKindDef } from "../../lib/flights";
import { HEAD_ID, TILE, inPortOf, moveNode, snapPos, type ChainGraph, type GraphNode, type GraphWire } from "../../lib/skyGraph";
import type { Pos } from "../../lib/skyLayout";
import { haltMark } from "../../lib/stopEditing";
import { riskTitle } from "../../lib/stopRisk";
import { BirdMarker } from "./Bird";
import { TileIcon, nodeFamily } from "./StopIcons";
import "./FlywayCanvas.css";
import "./SkyChain.css";

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
  /** The kinds list (`GET /api/flights/kinds`): a tile's family colour
   * follows the server's `family`; without it, StopIcons' own map. */
  kinds?: readonly StepKindDef[] | null;
  /** A loop pill pressed down (before its click): the host may start a
   * re-aim drag from here (`onLoopTo` on the classic canvas). */
  onPillDown?(via: DiagramNode, e: PointerEvent<HTMLElement>): void;
}

const DRAG_MIN = 4;

const STATE_WORD: Record<string, string> = {
  done: "done",
  current: "flying now",
  perched: "waiting on you",
  halted: "halted",
  paused: "paused",
  pending: "to come",
  skipped: "skipped",
  failed: "failed",
  edit: "",
  session: "",
};

const isStopish = (n: DiagramNode) => n.kind === "stop" || n.kind === "group";

/** A tile's second line: a group's "k of n" (and the sub-step it is at), a
 * flown stop's receipt, else the node's summary. */
export function tileSub(n: DiagramNode): string {
  if (n.kind === "group" && n.group) return n.group.done + " of " + n.group.total + (n.group.current ? " · " + n.group.current.title : "");
  if (n.receipt) return n.receipt;
  return n.sub;
}

/** What a screen reader says for a tile. */
export function tileLabel(n: DiagramNode, head = false): string {
  if (head) return (n.title || "Session") + (n.chip ? " — " + n.chip.label : "") + (n.sub ? ", " + n.sub : "");
  const word = STATE_WORD[n.state] ?? "";
  const sub = tileSub(n);
  return n.title + (word ? " — " + word : "") + (sub ? ", " + sub : "") + (n.reason ? " (" + n.reason + ")" : "");
}

interface Drag {
  id: string;
  pointer: number;
  sx: number;
  sy: number;
  x0: number;
  y0: number;
  live: boolean;
  pos: Pos;
}

export function SkyChain(p: SkyChainProps): JSX.Element {
  const [drag, setDrag] = useState<Drag | null>(null);
  const dragRef = useRef<Drag | null>(null);
  const justDragged = useRef(false);
  const plusPress = useRef<{ pointer: number; sx: number; sy: number; sent: boolean } | null>(null);

  const g = useMemo(() => (drag?.live ? moveNode(p.graph, drag.id, drag.pos) : p.graph), [p.graph, drag]);
  const head = g.head;
  const all = g.headNode ? [g.headNode, ...g.nodes] : g.nodes;
  const byId = new Map(all.map((n) => [n.id, n]));
  const zoom = p.zoom > 0 ? p.zoom : 1;
  const headNodeId = head.node?.id;
  const isSel = (n: GraphNode) => !!p.selected && (p.selected === n.id || (n.id === HEAD_ID && p.selected === headNodeId));
  const firstStop = g.nodes.find((n) => n.node.row === "main") || null;

  const activate = (n: GraphNode) => {
    if (justDragged.current) return;
    if (n.id !== HEAD_ID && isStopish(n.node)) p.onSelect?.(n.node);
    else p.onOpen?.(n.node);
  };
  const edit = (n: GraphNode) => (p.onEdit ? p.onEdit(n.node) : p.onSelect?.(n.node));

  // --- Node drag ---------------------------------------------------------------
  const draggable = (n: GraphNode) => !!p.onNodeMove && n.id !== HEAD_ID && isStopish(n.node);
  const endDrag = (commit: boolean) => {
    const d = dragRef.current;
    dragRef.current = null;
    setDrag(null);
    if (!d) return;
    if (d.live) {
      justDragged.current = true;
      setTimeout(() => (justDragged.current = false), 0);
      const n = p.graph.nodes.find((x) => x.id === d.id);
      if (commit && n && (d.pos.x !== n.x || d.pos.y !== n.y)) p.onNodeMove?.(n, d.pos);
    }
  };
  const onDown = (e: PointerEvent<HTMLDivElement>, n: GraphNode) => {
    if (e.button !== 0 || !draggable(n)) return;
    if ((e.target as HTMLElement).closest("button, input, textarea, a, [data-port], [data-no-drag]")) return;
    e.stopPropagation();
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* a synthetic event has no pointer to capture */
    }
    const d: Drag = { id: n.id, pointer: e.pointerId, sx: e.clientX, sy: e.clientY, x0: n.x, y0: n.y, live: false, pos: { x: n.x, y: n.y } };
    dragRef.current = d;
    setDrag(d);
  };
  const onMove = (e: PointerEvent<HTMLDivElement>) => {
    const d = dragRef.current;
    if (!d || d.pointer !== e.pointerId) return;
    const dx = e.clientX - d.sx;
    const dy = e.clientY - d.sy;
    if (!d.live && Math.hypot(dx, dy) < DRAG_MIN) return;
    const next: Drag = { ...d, live: true, pos: snapPos({ x: d.x0 + dx / zoom, y: d.y0 + dy / zoom }, e.altKey) };
    dragRef.current = next;
    setDrag(next);
  };
  const onUp = (e: PointerEvent<HTMLDivElement>) => {
    const d = dragRef.current;
    if (!d || d.pointer !== e.pointerId) return;
    endDrag(e.type === "pointerup");
  };

  const onKey = (e: KeyboardEvent<HTMLDivElement>, n: GraphNode) => {
    if (e.key === "Escape" && dragRef.current) {
      e.preventDefault();
      endDrag(false);
      return;
    }
    if (e.target !== e.currentTarget) return;
    if (e.key === "Enter") {
      e.preventDefault();
      if (n.id !== HEAD_ID && isStopish(n.node)) edit(n);
      else p.onOpen?.(n.node);
    } else if (e.key === " ") {
      e.preventDefault();
      activate(n);
    } else if ((e.key === "Delete" || e.key === "Backspace") && n.id !== HEAD_ID && isStopish(n.node) && p.onRemove) {
      e.preventDefault();
      p.onRemove(n.node);
    }
  };

  const portDown = (e: PointerEvent<HTMLElement>, n: GraphNode, side: "in" | "out") => {
    if (e.button !== 0) return;
    e.stopPropagation();
    p.onPortDown?.(n, side, e);
  };

  // --- Pieces --------------------------------------------------------------------
  const port = (n: GraphNode, side: "in" | "out", wired: boolean) => {
    const live = side === "in" ? n.inPort : n.outPort;
    if (!live && !wired) return null;
    const cls = "sky-port sky-port-" + side + (live ? "" : " sky-port-off");
    const title = side === "in" ? "Drag from here to pull this stop and the ones after it off into a chain of their own" : "Drag from here to wire it to a stop, another chain or empty sky";
    return live ? (
      <span className={cls} data-port={side} data-node={n.id} title={title} onPointerDown={(e) => portDown(e, n, side)} />
    ) : (
      <span className={cls} aria-hidden="true" />
    );
  };

  const wiredIn = new Set(g.wires.filter((w) => w.kind !== "add").map((w) => w.to));
  const wiredOut = new Set(g.wires.map((w) => w.from));

  const birdOn = (() => {
    const b = p.bird;
    if (!b) return null;
    const id = b.nodeId === headNodeId ? HEAD_ID : b.nodeId;
    return byId.get(id) || null;
  })();

  const tile = (n: GraphNode) => {
    const isHead = n.id === HEAD_ID;
    const dn = n.node;
    const fam = nodeFamily(dn, p.kinds);
    const halt = isHead ? null : haltMark(dn);
    const sub = isHead ? head.sub : tileSub(dn);
    const cls = [
      "sky-node",
      isHead ? "sky-head sky-head-" + head.kind : "sky-node-" + dn.kind,
      "st-" + dn.state,
      "sky-fam-" + fam,
      isHead && dn.chip ? "tone-" + dn.chip.tone : "",
      dn.kind === "group" ? "is-group" : "",
      dn.perch ? "is-perch" : "",
      n.inPort || n.outPort ? "" : "is-fixed",
      isSel(n) ? "is-selected" : "",
      p.highlighted?.has(n.id) || (isHead && headNodeId && p.highlighted?.has(headNodeId)) ? "is-highlighted" : "",
      p.targets?.has(n.id) ? "is-target" : "",
      p.over === n.id ? "is-over" : "",
      drag?.live && drag.id === n.id ? "is-dragging" : "",
      draggable(n) ? "is-draggable" : "",
    ]
      .filter(Boolean)
      .join(" ");
    const said = tileLabel(isHead ? { ...dn, title: head.title, sub: head.sub } : dn, isHead);
    return (
      <div
        key={n.id}
        role="listitem"
        tabIndex={0}
        className={cls}
        data-node={n.id}
        data-diagram-node={dn.id}
        aria-label={said}
        aria-current={isSel(n) ? "true" : undefined}
        title={isHead ? undefined : dn.reason ? dn.title + " — " + dn.reason : sub ? dn.title + " — " + sub : dn.title}
        style={{ transform: `translate(${n.x}px, ${n.y}px)`, width: n.w, height: n.h, ...(isHead ? { ["--sky-head-color" as string]: head.color } : {}) }}
        onPointerDown={(e) => onDown(e, n)}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        onClick={(e) => {
          if ((e.target as HTMLElement).closest("[data-port], [data-no-drag], button")) return;
          activate(n);
        }}
        onDoubleClick={(e) => {
          if (isHead || !isStopish(dn) || !p.onEdit) return;
          if ((e.target as HTMLElement).closest("button, input, textarea, a, [data-port]")) return;
          e.preventDefault();
          edit(n);
        }}
        onKeyDown={(e) => onKey(e, n)}
        onContextMenu={(e) => {
          if (!p.onMenu) return;
          e.preventDefault();
          e.stopPropagation();
          p.onMenu(dn, { clientX: e.clientX, clientY: e.clientY });
        }}
      >
        <div className="sky-tile" style={{ width: TILE, height: TILE }}>
          <span className="sky-tile-icon">
            <TileIcon kind={isHead ? (head.kind === "trigger" ? "handoff" : "session") : dn.perch ? "perch" : dn.icon} size={28} />
          </span>
          {isHead && head.kind !== "none" && <span className="sky-head-dot" aria-hidden="true" />}
          {!isHead && dn.state === "done" && (
            <span className="sky-badge sky-badge-done" aria-hidden="true">
              ✓
            </span>
          )}
          {!isHead && dn.state === "failed" && (
            <span className="sky-badge sky-badge-failed" aria-hidden="true">
              !
            </span>
          )}
          {halt && (
            <span className="sky-badge sky-badge-halt" title={halt.title} aria-label={halt.title} role="img">
              {halt.glyph}
            </span>
          )}
          {!isHead && dn.when && (
            <span className="sky-badge sky-badge-if" title={"Only when " + dn.when} aria-label={"Only when " + dn.when} role="img">
              if
            </span>
          )}
          {port(n, "in", wiredIn.has(n.id))}
          {port(n, "out", wiredOut.has(n.id))}
          {!isHead && dn.risk && dn.risk.length > 0 && (
            <span className="sky-risk" title={riskTitle(dn.risk)} role="img" aria-label={"Touches: " + riskTitle(dn.risk)} data-risk={dn.risk.join(" ")}>
              {dn.risk.map((t) => (
                <i key={t} className={"sky-risk-" + t} />
              ))}
            </span>
          )}
        </div>
        <div className="sky-node-title">{isHead ? head.title : dn.title}</div>
        {sub && <div className="sky-node-sub">{sub}</div>}
        {isHead ? (
          <div className="sky-head-extra" data-no-drag>
            {p.renderHead ? p.renderHead() : dn.chip ? <span className={"sky-head-chip tone-" + dn.chip.tone}>{dn.chip.label}</span> : null}
          </div>
        ) : (
          p.renderInBox && <div className="sky-node-extra" data-no-drag>{p.renderInBox(dn)}</div>
        )}
      </div>
    );
  };

  const wireClass = (w: GraphWire) => {
    const to = byId.get(w.to)?.node;
    const live = (w.kind === "line" || w.kind === "head" || w.kind === "fan") && to && (to.state === "current" || to.state === "perched");
    return ["sky-wire", "sky-wire-" + w.kind, w.done ? "is-done" : "", live ? "is-live" : "", w.loop ? "sky-loop-" + w.loop.state : ""].filter(Boolean).join(" ");
  };
  const tinted = (w: GraphWire) => w.kind === "line" || w.kind === "head";

  const b = g.bbox;
  const plusAfter = g.plus ? byId.get(g.plus.after) || null : null;
  const socket = head.kind === "none" && firstStop ? inPortOf(firstStop) : null;

  return (
    <div
      className={"sky-chain" + (p.className ? " " + p.className : "") + (drag?.live ? " is-dragging-node" : "")}
      style={{ position: "relative", width: Math.max(0, b.x + b.w), height: Math.max(0, b.y + b.h), ["--sky-chain-color" as string]: head.color }}
      data-chain={g.key}
      data-flight={g.flightId || undefined}
      data-head={head.kind}
      role="list"
      aria-label={(head.kind === "session" ? "Chain in " + head.title : head.kind === "trigger" ? head.title : "Stops with no session yet") + " — drag a tile to move it, drag a port to wire it"}
    >
      <svg className="sky-wires" aria-hidden="true" focusable="false" style={{ left: b.x, top: b.y }} width={Math.max(1, b.w)} height={Math.max(1, b.h)} viewBox={`${b.x} ${b.y} ${Math.max(1, b.w)} ${Math.max(1, b.h)}`}>
        {g.wires.map((w) => (
          <path key={w.id} className={wireClass(w)} d={w.d} data-wire={w.kind} data-from={w.from} data-to={w.to} style={tinted(w) ? { stroke: head.color } : undefined} />
        ))}
        {socket && <path className="sky-wire sky-wire-socket" d={`M ${socket.x - 16} ${socket.y} H ${socket.x}`} />}
      </svg>
      {all.map(tile)}
      {socket && firstStop && (
        <span
          className={"sky-socket" + (p.targets?.has(firstStop.id) ? " is-target" : "") + (p.over === firstStop.id ? " is-over" : "")}
          style={{ left: socket.x - 24, top: socket.y }}
          data-port="in"
          data-node={firstStop.id}
          data-socket=""
          role="img"
          aria-label="Wire a session's out port here to run it"
          title="Wire a session's out port here to run it"
        />
      )}
      {g.plus && (
        <button
          type="button"
          className="sky-plus fl-icon-btn"
          style={{ left: g.plus.at.x, top: g.plus.at.y }}
          data-port="out"
          data-node={g.plus.after}
          data-plus=""
          aria-label="Add a stop after this one — or drag to wire it"
          title="Add a stop (drag to wire it somewhere)"
          onPointerDown={(e) => {
            if (e.button !== 0) return;
            e.stopPropagation();
            plusPress.current = { pointer: e.pointerId, sx: e.clientX, sy: e.clientY, sent: false };
          }}
          onPointerMove={(e) => {
            const pr = plusPress.current;
            if (!pr || pr.sent || pr.pointer !== e.pointerId || !plusAfter) return;
            if (Math.hypot(e.clientX - pr.sx, e.clientY - pr.sy) < DRAG_MIN) return;
            pr.sent = true;
            p.onPortDown?.(plusAfter, "out", e as unknown as PointerEvent<HTMLElement>);
          }}
          onPointerUp={() => {
            const pr = plusPress.current;
            if (pr?.sent) {
              justDragged.current = true;
              setTimeout(() => (justDragged.current = false), 0);
            }
            plusPress.current = null;
          }}
          onPointerCancel={() => (plusPress.current = null)}
          onClick={(e) => {
            e.stopPropagation();
            if (justDragged.current) return;
            p.onPlus?.(plusAfter);
          }}
        >
          <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true" focusable="false">
            <path d="M6 1.5v9M1.5 6h9" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
          </svg>
        </button>
      )}
      {g.wires.map((w) => {
        if (w.kind !== "loop" || !w.loop) return null;
        const m = w.loop;
        const v = w.via;
        const text = loopPill(m) || loopIdlePill(m, v);
        const title = (m.state === "pending" && m.runs === 0 ? "Loop: " + (v?.sub || text) : loopPillTitle(m)) + (v ? " — click to change it" : "");
        const cls = ["sky-loop-pill", "sky-loop-" + m.state, v ? "st-" + v.state : "", v && p.selected === v.id ? "is-selected" : ""].filter(Boolean).join(" ");
        const style = { left: w.mid.x, top: w.mid.y };
        return v ? (
          // A span with a button's role, as the classic canvas's pill is.
          <span
            key={"pill:" + w.id}
            role="button"
            tabIndex={0}
            className={cls}
            style={style}
            data-node={v.id}
            data-pill=""
            title={title}
            aria-label={title}
            onPointerDown={(e) => {
              if (e.button !== 0) return;
              e.stopPropagation();
              p.onPillDown?.(v, e);
            }}
            onClick={(e) => {
              e.stopPropagation();
              p.onLoopPill?.(v);
            }}
            onContextMenu={(e) => {
              if (!p.onMenu) return;
              e.preventDefault();
              e.stopPropagation();
              p.onMenu(v, { clientX: e.clientX, clientY: e.clientY });
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                p.onLoopPill?.(v);
              } else if ((e.key === "Delete" || e.key === "Backspace") && p.onRemove) {
                e.preventDefault();
                p.onRemove(v);
              }
            }}
          >
            {text}
          </span>
        ) : (
          <span key={"pill:" + w.id} className={cls} style={style} title={title} aria-label={title} role="img">
            {text}
          </span>
        );
      })}
      {birdOn && p.bird && <BirdMarker tone={p.bird.tone} x={birdOn.x + birdOn.w / 2} y={birdOn.y} size={22} label={"The flight is at “" + (birdOn.id === HEAD_ID ? head.title : birdOn.node.title) + "”"} className="sky-chain-bird" />}
    </div>
  );
}
