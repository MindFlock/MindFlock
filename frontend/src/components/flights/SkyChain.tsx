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
 * (`onSelect`; a head / child / flown tile: `onOpen`).
 *
 * Round 20 (docs/flights.md "Round 20" A, docs/flights-r20-frames.md): a
 * session is a FRAME (SkyLine draws it round the chain), and the head tile is
 * the session's AGENT — the agent icon, its name ("Claude", "Codex", else
 * "Agent"), what it is doing now, its state chip, the bird. A sub-session
 * (a `child` / `branch` node) draws as a small frame of its own: its name on
 * the frame's bar, its agent tile inside (state, bird), and its own chain as
 * a strip of mini stops when it flies one (`subFrames`). The graph gives each
 * sub-frame its box (`subFrameSize` through `ChainGraphOpts.big`). */

import { useEffect, useMemo, useRef, useState, type JSX, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import type { Instance } from "../../api/types";
import type { BirdPerch, Diagram, DiagramEdge, DiagramNode } from "../../lib/flightDiagram";
import { leftWord, loopIdlePill, loopPill, loopPillTitle, sessionDiagram } from "../../lib/flightDiagram";
import type { Flight, StepKindDef } from "../../lib/flights";
import { agentName } from "../../lib/agentNames";
import { firstLine } from "../../lib/format";
import { windowName } from "../../lib/windowName";
import { HEAD_ID, NODE_W, TILE, chainColor, inPortOf, isBranchRow, moveNode, snapPos, type ChainGraph, type GraphNode, type GraphWire } from "../../lib/skyGraph";
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
  /** A chain with no session (n8n's trigger button): ▶ on the left of its
   * first stop runs it; while it runs the same button stops it. */
  run?: { running: boolean; label: string; onRun(): void; onStop(): void } | null;
  /** Extra content in or under a tile (a perch's Done / Skip …). */
  renderInBox?(n: DiagramNode): ReactNode;
  /** A node drawn open instead of as a tile (an open sub-session's card):
   * its content, else null for the tile. The graph gives it the room
   * (`ChainGraphOpts.big`). */
  renderOpen?(n: DiagramNode): ReactNode;
  /** The head tile's own content (the session's chip, ⋯) — the host's. */
  renderHead?(): ReactNode;
  className?: string;
  /** The kinds list (`GET /api/flights/kinds`): a tile's family colour
   * follows the server's `family`; without it, StopIcons' own map. */
  kinds?: readonly StepKindDef[] | null;
  /** A loop pill pressed down (before its click): the host may start a
   * re-aim drag from here (`onLoopTo` on the classic canvas). */
  onPillDown?(via: DiagramNode, e: PointerEvent<HTMLElement>): void;
  /** Round 20: each sub-session's frame (by its title): its agent's name and
   * its own chain's stops (`subFramesOf`). Without one, the frame still draws
   * ("Agent", no strip). */
  subFrames?: Readonly<Record<string, SubFrame>>;
}

// --- Round 20: agents and frames -------------------------------------------------

/** The agent's name on its tile: the provider's product name, short
 * ("Claude", "Codex"), else "Agent". */
export function agentTitle(provider?: string | null, program?: string | null): string {
  const p = String(provider || "").trim();
  // A session with no agent (a plain shell — the default session when no stop
  // needs one): it is a place, the tile says "Shell".
  const shell = (s: string) => /^(bash|zsh|sh|fish|dash|ksh)$/.test(s.trim().split(/\s+/)[0]?.split("/").pop() || "");
  if (shell(String(program || "")) || shell(p)) return "Shell";
  return p ? agentName(p).replace(/ Code$/, "") : "Agent";
}

/** What the agent is doing now (the agent tile's summary): the task
 * headline (read from its newest meaningful prompt), else the create task,
 * else the last prompt — one line. */
export function agentDoing(row: Pick<Instance, "task_headline" | "last_prompt" | "task"> | null | undefined): string {
  if (!row) return "";
  return firstLine(row.task_headline || row.task || "") || firstLine(row.last_prompt || "");
}

/** One stop of a sub-session's own chain, drawn small inside its frame. */
export interface MiniStop {
  id: string;
  title: string;
  state: string;
  icon: string;
}

export interface SubFrame {
  /** The agent tile's title (`agentTitle`). */
  agent: string;
  /** Its own chain's stops (empty: it flies none). */
  stops: MiniStop[];
}

/** A sub-frame's bar (its name), the gap under it, a mini stop's square and
 * the strip it sits in, and how many mini stops show before "+n". */
export const SUB_BAR = 22;
export const SUB_PAD = 8;
export const MINI = 18;
export const MINI_GAP = 6;
export const MINI_STRIP = 30;
export const MINI_MAX = 8;
/** A sub-frame without a chain: its bar, the agent tile, name, summary, chip. */
export const SUB_W = NODE_W + 24;
export const SUB_H = SUB_BAR + SUB_PAD + 152;

/** The box a sub-session's frame takes on its parent's chain (plane px): the
 * room for its agent tile, and for its own chain's strip when it flies one. */
export function subFrameSize(sf?: SubFrame | null): { w: number; h: number } {
  const n = Math.min(sf?.stops.length || 0, MINI_MAX + 1);
  if (!n) return { w: SUB_W, h: SUB_H };
  return { w: Math.max(SUB_W, 2 * SUB_PAD + n * MINI + (n - 1) * MINI_GAP), h: SUB_H + MINI_STRIP };
}

const MINI_KINDS: ReadonlySet<string> = new Set(["stop", "group", "rung", "flown"]);

/** Every sub-session's frame under `parent` (rows from /api/instances): its
 * agent's name and its own chain (lib/flightDiagram `sessionDiagram` of the
 * child alone: its flight's stops, or its fast-track's rungs). */
export function subFramesOf(rows: readonly Instance[], parent: string, flights?: readonly Flight[] | null, kinds?: readonly StepKindDef[] | null): Record<string, SubFrame> {
  const out: Record<string, SubFrame> = {};
  for (const k of rows) {
    if (k.parent !== parent || k.title === parent) continue;
    const d = sessionDiagram(rows, k.title, { family: false, flights: flights || undefined, kinds: kinds || undefined });
    const stops = d.mode === "none" ? [] : d.nodes.filter((n) => n.row === "main" && MINI_KINDS.has(n.kind)).map((n) => ({ id: n.id, title: n.title, state: n.state, icon: n.perch ? "perch" : n.icon }));
    out[k.title] = { agent: agentTitle(k.provider || k.program, k.program), stops };
  }
  return out;
}

/** Every sub-session node's box for `ChainGraphOpts.big` (by node id): its
 * frame, or `open[title]` (an open card's box) when it is open. */
export function subFrameBoxes(nodes: readonly DiagramNode[], frames: Readonly<Record<string, SubFrame>>, open: Readonly<Record<string, { w: number; h: number }>> = {}): Record<string, { w: number; h: number }> {
  const out: Record<string, { w: number; h: number }> = {};
  for (const n of nodes) {
    if (!isSubSession(n)) continue;
    out[n.id] = open[n.session!] || subFrameSize(frames[n.session!]);
  }
  return out;
}

/** Round 20: a session's sub-sessions hang from its AGENT tile (the dashed
 * bus from the agent to each small frame), not from wherever its flight is:
 * every `below` child's split becomes the session node, and the line it was
 * hung across (the stop it hung at → the next) is drawn straight again. A
 * worker spawned at one of the flight's stops (`branch`) keeps its stop —
 * that is where it came from. A rendering view: lib/flightDiagram's own
 * diagram is untouched. */
export function hangFromAgent(d: Diagram): Diagram {
  const me = d.nodes.find((n) => n.kind === "session" && n.row === "main");
  const below = d.nodes.filter((n) => n.kind === "child" && n.row === "below" && n.branch && n.branch.from !== me?.id);
  if (!me || !below.length) return d;
  const moved = new Set(below.map((n) => n.id));
  const hang = below[0].branch!.from;
  const next = below[0].branch!.to;
  const nodes = d.nodes.map((n) => (moved.has(n.id) ? { ...n, branch: { ...n.branch!, from: me.id } } : n));
  const edges: DiagramEdge[] = d.edges.map((e) => (e.kind === "branch" && moved.has(e.to) ? { ...e, from: me.id } : e));
  const nextNode = d.nodes.find((n) => n.id === next);
  if (nextNode && !edges.some((e) => e.from === hang && e.to === next && (e.kind === "line" || e.kind === "add"))) edges.push({ from: hang, to: next, kind: nextNode.kind === "add" ? "add" : "line" });
  return { ...d, nodes, edges };
}

/** A sub-session (or a worker branched off a stop): a small frame. */
export const isSubSession = (n: Pick<DiagramNode, "kind" | "session">) => (n.kind === "child" || n.kind === "branch") && !!n.session;

const MINI_WORD: Record<string, string> = { done: "done", current: "flying now", perched: "waiting on you", halted: "halted", paused: "paused", failed: "failed", skipped: "skipped", pending: "to come" };

/** A sub-session's own chain, small: one square per stop, a line through them. */
function MiniChain({ stops }: { stops: readonly MiniStop[] }): JSX.Element {
  const shown = stops.length > MINI_MAX + 1 ? stops.slice(0, MINI_MAX) : stops;
  const more = stops.length - shown.length;
  const at = stops.findIndex((s) => s.state === "current" || s.state === "perched" || s.state === "halted" || s.state === "paused");
  const said = stops.length + (stops.length === 1 ? " stop" : " stops") + (at >= 0 ? ", at “" + stops[at].title + "”" : "");
  return (
    <div className="sky-mini-chain" role="img" aria-label={"Its own chain: " + said} title={stops.map((s) => s.title + (MINI_WORD[s.state] ? " — " + MINI_WORD[s.state] : "")).join("\n")}>
      {shown.map((s) => (
        <span key={s.id} className={"sky-mini st-" + s.state} data-mini={s.id}>
          <TileIcon kind={s.icon} size={11} />
        </span>
      ))}
      {more > 0 && <span className="sky-mini sky-mini-more">+{more}</span>}
    </div>
  );
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
  if (head) return (n.title || "Agent") + (n.chip ? " — " + n.chip.label : "") + (n.sub ? ", " + n.sub : "");
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
  // Every tile moves (the owner: "I should be able to drag every block
  // around") — stops, groups, and sub-sessions / workers hung under them.
  const draggable = (n: GraphNode) => !!p.onNodeMove && n.id !== HEAD_ID && (isStopish(n.node) || isBranchRow(n.node));
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
    // The head of a session's chain is its AGENT (round 20); a sub-session is
    // a small frame round its own agent tile.
    const isAgent = isHead && head.kind === "session";
    const sub_ = !isHead && isSubSession(dn);
    const frame = sub_ ? p.subFrames?.[dn.session!] : undefined;
    const fam = nodeFamily(dn, p.kinds);
    const halt = isHead ? null : haltMark(dn);
    const sub = isHead ? head.sub : tileSub(dn);
    const cls = [
      "sky-node",
      isHead ? "sky-head sky-head-" + head.kind : "sky-node-" + dn.kind,
      isAgent ? "sky-agent" : "",
      sub_ ? "sky-subframe" : "",
      "st-" + dn.state,
      "sky-fam-" + fam,
      // A session-like tile (the head, a sub-session, a worker) wears its
      // session's state: working glows, asking is gold.
      dn.chip ? "tone-" + dn.chip.tone : "",
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
    const said = sub_
      ? "Sub-session " + dn.title + " — its agent" + (frame ? " (" + frame.agent + ")" : "") + (dn.chip ? " " + dn.chip.label.toLowerCase() : "") + (sub ? ", " + sub : "")
      : (isAgent ? "Agent: " : "") + tileLabel(isHead ? { ...dn, title: head.title, sub: head.sub } : dn, isHead);
    const opened = !isHead && p.renderOpen ? p.renderOpen(dn) : null;
    if (opened)
      return (
        <div key={n.id} role="listitem" className="sky-node sky-node-open" data-node={n.id} data-diagram-node={dn.id} aria-label={said} style={{ transform: `translate(${n.x}px, ${n.y}px)`, width: n.w, height: n.h }}>
          {opened}
        </div>
      );
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
        title={isHead ? undefined : sub_ ? dn.title + " — click its agent to open it here" : dn.reason ? dn.title + " — " + dn.reason : sub ? dn.title + " — " + sub : dn.title}
        style={{
          transform: `translate(${n.x}px, ${n.y}px)`,
          width: n.w,
          height: n.h,
          ...(isHead ? { ["--sky-head-color" as string]: head.color } : {}),
          ...(sub_ ? { ["--sky-sub-color" as string]: chainColor(dn.session) } : {}),
        }}
        data-agent={isAgent || sub_ ? "" : undefined}
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
        {sub_ && (
          <div className="sky-subframe-bar" title={dn.title}>
            {dn.title}
          </div>
        )}
        <div className="sky-tile" style={{ width: TILE, height: TILE }}>
          <span className="sky-tile-icon">
            <TileIcon kind={isAgent || sub_ ? "agent" : isHead ? (head.kind === "trigger" ? "handoff" : "session") : dn.perch ? "perch" : dn.icon} size={28} />
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
          {/* A sub-frame's bus lands on its frame, not on a port. */}
          {!sub_ && port(n, "in", wiredIn.has(n.id))}
          {!sub_ && port(n, "out", wiredOut.has(n.id))}
          {!isHead && dn.risk && dn.risk.length > 0 && (
            <span className="sky-risk" title={riskTitle(dn.risk)} role="img" aria-label={"Touches: " + riskTitle(dn.risk)} data-risk={dn.risk.join(" ")}>
              {dn.risk.map((t) => (
                <i key={t} className={"sky-risk-" + t} />
              ))}
            </span>
          )}
        </div>
        <div className="sky-node-title">{isHead ? head.title : sub_ ? frame?.agent || "Agent" : dn.title}</div>
        {sub && <div className="sky-node-sub">{sub}</div>}
        {!isHead && dn.until && (dn.state === "current" || dn.state === "paused" || dn.state === "halted") ? <Countdown until={dn.until} /> : null}
        {isHead ? (
          <div className="sky-head-extra" data-no-drag>
            {p.renderHead ? p.renderHead() : dn.chip ? <span className={"sky-head-chip tone-" + dn.chip.tone}>{dn.chip.label}</span> : null}
          </div>
        ) : dn.chip && !isStopish(dn) ? (
          // A sub-session / worker: its state, always (the owner: "I should be
          // able to see the states of the sub-sessions, always").
          <div className="sky-node-extra">
            <span className={"sky-head-chip tone-" + dn.chip.tone}>{dn.chip.label}</span>
          </div>
        ) : (
          p.renderInBox && <div className="sky-node-extra" data-no-drag>{p.renderInBox(dn)}</div>
        )}
        {sub_ && frame && frame.stops.length > 0 && <MiniChain stops={frame.stops} />}
        {!isHead && dn.chip && !isStopish(dn) && (dn.chip.tone === "working" || dn.chip.tone === "attention") && (
          <BirdMarker tone={dn.chip.tone === "working" ? "working" : "you"} x={sub_ ? 8 + TILE / 2 : n.w / 2} y={sub_ ? SUB_BAR + SUB_PAD : 0} size={18} label={dn.title + " — " + dn.chip.label} className="sky-kid-bird" />
        )}
      </div>
    );
  };

  const wireClass = (w: GraphWire) => {
    const to = byId.get(w.to)?.node;
    const live = (w.kind === "line" || w.kind === "head" || w.kind === "fan") && to && (to.state === "current" || to.state === "perched");
    // The bus down to a sub-session's frame is dashed (round 20).
    const bus = w.kind === "fan" && !!to && isSubSession(to) && isBranchRow(to);
    return ["sky-wire", "sky-wire-" + w.kind, bus ? "sky-wire-bus" : "", w.done ? "is-done" : "", live ? "is-live" : "", w.loop ? "sky-loop-" + w.loop.state : ""].filter(Boolean).join(" ");
  };
  const tinted = (w: GraphWire) => w.kind === "line" || w.kind === "head";

  const b = g.bbox;
  const plusAfter = g.plus ? byId.get(g.plus.after) || null : null;
  const socket = head.kind === "none" && firstStop && !p.run?.running ? inPortOf(firstStop) : null;

  return (
    <div
      className={"sky-chain" + (p.className ? " " + p.className : "") + (drag?.live ? " is-dragging-node" : "")}
      style={{ position: "relative", width: Math.max(0, b.x + b.w), height: Math.max(0, b.y + b.h), ["--sky-chain-color" as string]: head.color }}
      data-chain={g.key}
      data-flight={g.flightId || undefined}
      data-head={head.kind}
      role="list"
      aria-label={(head.kind === "session" ? "Chain in " + (head.session ? windowName(head.session) : head.title) : head.kind === "trigger" ? head.title : "Stops with no session yet") + " — drag a tile to move it, drag a port to wire it"}
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
      {p.run && firstStop && (() => {
        const at = inPortOf(firstStop);
        const r = p.run;
        return (
          <button
            type="button"
            className={"sky-run fl-icon-btn" + (r.running ? " is-running" : "")}
            style={{ left: at.x - (socket ? 46 : 22), top: at.y }}
            data-no-drag
            aria-label={r.label}
            title={r.label}
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => {
              e.stopPropagation();
              if (r.running) r.onStop();
              else r.onRun();
            }}
          >
            {r.running ? "■" : "▶"}
          </button>
        );
      })()}
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

/** A waiting stop's live countdown (a Wait's timer, a paced Loop's next run):
 * "12:34 left", ticking each second; "due" once it is up. */
function Countdown({ until }: { until: number }): JSX.Element {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, []);
  const at = new Date(until * 1000);
  return (
    <div className="sky-node-left" title={"Until " + at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} aria-live="off">
      ⏱ {leftWord(until - now)}
    </div>
  );
}
