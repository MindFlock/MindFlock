/** One session's line in the Sky (docs/flights-sky-line.md): its title bar,
 * then its session diagram — the session box, its workers, the fast-track's
 * rungs or the flight's stops, the trailing + — with the bird on the box the
 * flight is at.
 *
 * Round 18 (docs/flights.md "Round 18", docs/flights-r18/sky.md): the line
 * draws as a CHAIN (SkyChain over lib/skyGraph `chainGraph`): a small label
 * (its handle and ⋯), then the session tile heading free-floating stop tiles
 * joined by wires, in the session's colour (`chainColor`). The session
 * tile's press opens its card (PaneCard, under the chain); a tile dragged
 * keeps its spot (`useNodeMoves` → `queueStopPos`); a port pressed hands the
 * wire drag to the host (`onPortDown`); the palette and a stop's dialog float
 * beside their tile (`ChainPanels`); the host reads the chain's graph from
 * `registerChain`. No ◧, no corner: a chain's tiles have one size. The
 * older notes below describe the row this replaced where they differ.
 *
 * The line is an isomorphism of the normal flow: press the session box (or a
 * worker's) and it opens in place into a card holding the SAME Pane the grid
 * draws for that title (`embedded`: no grip, no hide −) — Agent · Terminal ·
 * Diff · Queue · Map · Flight, the registry's terminals, the per-title tab —
 * resizable from its corner, ▣ folds it back into a box. A worker spawned
 * at one of the flight's stops is a BRANCH between that stop and the next
 * (docs/flights-branches.md) — it opens the same way, and is drawn only
 * there, never also below.
 *
 * The line's corner resizes its boxes (`boxW` × `boxH`, plane px of one stop
 * box): the canvas draws the ◧ tier nearest that width (compact → normal →
 * wide, what a box shows) and the line scales it uniformly to the width asked
 * for (`boxScale`) — a transform inside a sizer, so the line still measures
 * its drawn size, and `--sky-z` inside carries the scale so what counter-
 * scales (a box's toolbar, a menu) stays screen-sized. ◧ and the ⋯'s sizes
 * are presets of the same two numbers.
 *
 * The sky draws the roomy tier of boxes (`roomy`: bigger boxes, bigger type,
 * docs/flights-visibility.md). Flown stops stay boxes; the ⋯'s "Fold flown
 * stops" folds them into one `flown` pill on this line, and pressing the pill
 * unfolds them.
 *
 * The host (Sky.tsx) places the line at `layout.x/y`, drags it by the title
 * bar (`onDragStart`), persists `layout` (`onLayout`) and gives it room from
 * `onMeasure` — the line's size in plane px, read by a ResizeObserver. A
 * worker's open card is this line's own state (the host keeps one layout per
 * line); it reaches the host only as the line growing.
 *
 * Round 12 (docs/flights.md "As built: round 12 sky"): `draggable` false (the
 * sky's click mode) and the bar never starts a move. `registerDelete` hands
 * the host this line's Delete: a stop or group box leaves the line through
 * the canvas's own Remove (the undoable toast), a session's box or the line
 * itself asks inline to close that session. A press on the bar focuses the
 * line, so Delete has somewhere to land.
 *
 * Round 20 (docs/flights.md "Round 20" A, docs/flights-r20-frames.md): the
 * session is a FRAME round its chain — a soft rounded box in the session's
 * colour (`.skl-frame-box`, grown to every tile's box when one is dragged
 * left of / above the origin), its title bar the session's name · branch ·
 * state chip · where it is · ⋯ (the drag handle; a double-click renames).
 * The chain's first tile is the AGENT (SkyChain `sky-agent`): its name, what
 * it is doing now, its chip (or round 19's Ready + ▶ Run), the bird; a click
 * opens the terminal in its place, a right-click its menu (Open terminal,
 * Send a prompt…, Stop, then the session's own ⋯ items). Sub-sessions are
 * small frames under it (`subFramesOf` / `subFrameBoxes`). */

import { useEffect, useMemo, useRef, useState, type JSX, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { setView, showInSky } from "../../lib/viewMode";
import { BOX_PRESETS, boxScale, clampBox, lodFor, nearLines } from "../../lib/skyLayout";
import { nameTakenBy, windowName } from "../../lib/windowName";
import { toast } from "../../lib/toast";
import { useUi } from "../../state/store";
import type { Instance } from "../../api/types";
import { queryClient, useFlightKinds, useFlights } from "../../state/queries";
import { closeSession, selectSession, undoLastClose } from "../../lib/sessionActions";
import { nodeById, nodeSession, removableBox, removalNeedsAsk } from "../../lib/skySelect";
import { pushUndo } from "../../lib/skyUndo";
import { deriveSessionState, type SessionState, type SessionTone } from "../../lib/sessionState";
import { HELD_CHIP } from "../../lib/heldOffline";
import { startSession } from "../../lib/revive";
import { firstLine } from "../../lib/format";
import { birdPerch, drawnIndexes, flownCount, lineWhere, sessionDiagram, type Diagram, type DiagramNode } from "../../lib/flightDiagram";
import { NODE_W, TILE, chainColor, chainGraph, type ChainHead, type GraphNode } from "../../lib/skyGraph";
import { ageOf, ageWord, fmtDuration, segmentTitle, segmentsFor, totalSeconds, type LineSegment } from "../../lib/lineTime";
import type { FlightPlan, FlightRow, FlightState, StepKindDef } from "../../lib/flights";
import { InlineConfirm } from "../InlineConfirm";
import { OverflowMenu, type MenuItem } from "../OverflowMenu";
import { Pane } from "../grid/Pane";
import type { BoxSizeName } from "./FlywayCanvas";
import { agentMenuItems, pillAimer, useAgentMenu, useChainMenu, type AgentMenuItem } from "./useChainMenu";
import { ReadyHead } from "./ReadyHead";
import { SkyChain, agentTitle, hangFromAgent, subFrameBoxes, subFramesOf } from "./SkyChain";
import { ChainPanels } from "./SkyWires";
import { HEAD, useChainEntry, useNodeMoves, useTourHooks, type ChainEntry } from "./useSkyWiring";
import { nodeIndexes, usePromoteStops, useSessionLine, type LineSource } from "./useSessionLine";
import "./SkyLine.css";
import "./SkyVisibility.css";

export interface SkyLineLayout {
  x: number;
  y: number;
  size: "compact" | "normal" | "wide";
  open: boolean;
  paneW?: number;
  paneH?: number;
  pinned?: boolean;
  /** The boxes' size (plane px of one stop box), from the corner or a ◧
   * preset; unset, the `size` preset's numbers. */
  boxW?: number;
  boxH?: number;
}

export interface SkyLineProps {
  title: string;
  rows: Instance[];
  plans: FlightPlan[];
  layout: SkyLineLayout;
  onLayout(patch: Partial<SkyLineLayout>): void;
  focused: boolean;
  onFocus(): void;
  zoom: number;
  onDragStart?(e: React.PointerEvent): void;
  onMeasure?(w: number, h: number): void;
  dropFlyway?: FlightPlan | null;
  /** In the Sky's selection, as a whole line. */
  selected?: boolean;
  /** Its stop boxes in the Sky's selection (node ids). */
  highlighted?: ReadonlySet<string>;
  /** Shift+click on a box: the host's multi-selection (SkySelection). */
  onShiftSelect?(n: DiagramNode): void;
  /** False in the sky's click mode: the bar never starts a move. */
  draggable?: boolean;
  /** The host's handle on this line's Delete (null when the line goes): given
   * a box's node id ("" = the line itself), it acts and says whether it did. */
  registerDelete?(fn: ((node: string) => boolean) | null): void;
  /** A grounded line dragged by its bar is over this one: the drop puts its
   * stops on this session (docs/flights.md "As built: round 13 sky"). */
  mergeTarget?: boolean;
  /** Round 18: the host's handle on this chain (its graph, where it sits, its
   * palette) — the wiring and the cross-chain wires read it. */
  registerChain?(e: ChainEntry | null): void;
  /** A port (or the +) pressed: the host runs the wire drag. */
  onPortDown?(node: GraphNode, side: "in" | "out", e: React.PointerEvent<HTMLElement>): void;
  /** The tiles a wire being drawn could land on, and the one under it. */
  targets?: ReadonlySet<string>;
  over?: string | null;
}

/** The kinds before their query has loaded (one object: the chain's graph
 * is memoized on it). */
const NO_KINDS: StepKindDef[] = [];

/** The sky's handle on a line's Delete: a box's node id ("" = the line). */
export type LineDelete = (node: string) => boolean;

/** The session is still in the instance list (not closed, or reopened). */
export const sessionOpen = (title: string) => (queryClient.getQueryData<Instance[]>(["instances"]) || []).some((r) => r.title === title);

/** Put closed sessions back from Recently closed, the last closed first
 * (`undoLastClose`, the Ctrl+Shift+T road); one already open again is left be. */
export async function reopenClosed(titles: readonly string[]) {
  for (const t of [...titles].reverse()) if (!sessionOpen(t)) await undoLastClose();
}

/** The undo entry for sessions the sky closed (lib/skyUndo): Ctrl+Z reopens
 * them from Recently closed, newest first (`undoLastClose`, the Ctrl+Shift+T
 * road). closeSession's own toast keeps its own Undo; once every one of them
 * is open again (that toast pressed, Ctrl+Shift+T) the entry lapses. */
export function pushCloseUndo(titles: readonly string[]) {
  if (!titles.length) return;
  pushUndo({
    label: titles.length === 1 ? "Close “" + windowName(titles[0]) + "”" : "Close " + titles.length + " sessions",
    undo: () => reopenClosed(titles),
    valid: () => titles.some((t) => !sessionOpen(t)),
  });
}

/** What a running session is doing, for the question that names it. */
export const runningWord = (row: { activity?: string } | undefined) => (row?.activity === "working" ? "is working" : "is asking you something");

/** Close a session from the sky, undoably (Ctrl+Z, or the toast's Undo). */
export async function closeLine(title: string): Promise<void> {
  await closeSession(title);
  if (!sessionOpen(title)) pushCloseUndo([title]);
}

/** Round 21 (JUDGE-R21-1 #5): what a session's FRAME says at a glance. The
 * frame box is neutral (theme surfaces); this state drives its border
 * (SkyLine.css `.skl-frame[data-frame=…]`): `you` amber + a soft glow,
 * `halted` / `broken` red, `working` the accent with a slow breathing cue,
 * `done` the whole route at reduced emphasis (full on hover / focus),
 * `ready` and `idle` the plain neutral frame. The session's identity colour
 * is only the small dot by its name.
 *
 * Read from what the line already knows, first match wins: the route halted
 * → `halted`; the session broken (offline, setup failed) → `broken`; a route
 * parked on you, the session's Needs-you state (a question, failing checks,
 * idle on unfinished work) or a behavior waiting on you → `you`; the route
 * running or the agent working → `working`; the route finished (still drawn,
 * or `landed`) → `done`; built and waiting for ▶ Run → `ready`; else `idle`. */
export type FrameState = "you" | "halted" | "broken" | "working" | "done" | "ready" | "idle";
export function frameState(s: { flight?: FlightState | null; landed?: boolean; tone?: SessionTone | null; behaviorsYou?: boolean }): FrameState {
  const f = s.flight || null;
  if (f === "halted") return "halted";
  if (s.tone === "broken") return "broken";
  if (f === "waiting_you" || s.tone === "attention" || s.behaviorsYou) return "you";
  if (f === "running" || s.tone === "working") return "working";
  if (f === "done" || s.landed) return "done";
  if (f === "ready") return "ready";
  return "idle";
}

/** An open card's pane, in plane px: its default and its floor. */
export const PANE_W = 720;
export const PANE_H = 460;
export const PANE_MIN_W = 360;
export const PANE_MIN_H = 240;
/** The card's own head (the name and ▣) above the pane. */
export const CARD_HEAD_H = 30;

const SIZES: readonly BoxSizeName[] = ["compact", "normal", "wide"];

/** Round 15: the time under the line — a 4px ruler, one segment per flown
 * stop proportional to how long it took (lib/lineTime `segmentsFor`), its
 * hover title "Checks · 12 min", the total at the right end. Nothing to show
 * (no stop has flown, or none took any time) draws nothing. */
export function LineRuler({ segments }: { segments: readonly LineSegment[] }) {
  const total = totalSeconds(segments);
  if (!segments.length || total <= 0) return null;
  return (
    <div className="skl-ruler" role="img" aria-label={"Time so far: " + fmtDuration(total) + " over " + segments.length + (segments.length === 1 ? " stop" : " stops")} data-no-drag>
      <div className="skl-ruler-bar">
        {segments.map((s) => (
          <span key={s.step} className={"skl-ruler-seg skl-ruler-" + s.state} style={{ flexGrow: Math.max(s.seconds, total / 200) }} title={segmentTitle(s)} data-step={s.step} />
        ))}
      </div>
      <span className="skl-ruler-total" title={"Time spent so far"}>
        {fmtDuration(total)}
      </span>
    </div>
  );
}

/** ◧: compact → normal → wide → compact. */
export const nextSize = (s: BoxSizeName): BoxSizeName => SIZES[(SIZES.indexOf(s) + 1) % SIZES.length];

/** A ◧ preset as a layout patch: its name and its two numbers. */
export const presetPatch = (s: BoxSizeName): Partial<SkyLineLayout> => ({ size: s, boxW: BOX_PRESETS[s].w, boxH: BOX_PRESETS[s].h });

/** The corner handle's drag, in plane px: the line's drawn canvas (`cw` ×
 * `ch`, at box width `bw`) follows the pointer's travel (`dx`, `dy`) along the
 * axis it moved most, and the box size follows the canvas. The height is the
 * tier's, scaled the same (the scale is uniform). */
export function dragBox(start: { cw: number; ch: number; bw: number }, dx: number, dy: number): { w: number; h: number } {
  const rw = start.cw > 0 ? (start.cw + dx) / start.cw : 1;
  const rh = start.ch > 0 ? (start.ch + dy) / start.ch : 1;
  const r = Math.abs(rw - 1) >= Math.abs(rh - 1) ? rw : rh;
  const w = clampBox(start.bw * r, 0).w;
  const { size, k } = boxScale(w);
  return clampBox(w, BOX_PRESETS[size].h * k);
}

/** A card's pane size, never under the floor. */
export function clampPane(w: number, h: number): { w: number; h: number } {
  return { w: Math.max(PANE_MIN_W, Math.round(w)), h: Math.max(PANE_MIN_H, Math.round(h)) };
}

type LineCanvas = ReturnType<typeof useSessionLine>["canvas"];

/** A line's canvas props for a diagram with a `flown` pill. The canvas counts
 * a stop by its place among the DRAWN stops (`drawnIndexes`), the flight by
 * its index: every index (a move, an insert, a drop) and every node handed
 * back (a pick, a remove, a box's own parts) returns to the flight's own on
 * the way out. Without a pill: unchanged. */
export function flownCanvas(d: Diagram, canvas: LineCanvas): { diagram: Diagram; canvas: LineCanvas } {
  const off = flownCount(d);
  if (!off) return { diagram: d, canvas };
  const byId = new Map(d.nodes.map((n) => [n.id, n]));
  const orig = (n: DiagramNode) => byId.get(n.id) || n;
  const c = canvas;
  return {
    diagram: drawnIndexes(d),
    canvas: {
      ...c,
      onSelect: c.onSelect && ((n) => c.onSelect!(orig(n))),
      onOpen: c.onOpen && ((n) => c.onOpen!(orig(n))),
      onInsert: c.onInsert && ((at) => c.onInsert!(at + off)),
      onRemove: c.onRemove && ((n) => c.onRemove!(orig(n))),
      onMove: c.onMove && ((from, to) => c.onMove!(from + off, to + off)),
      onDuplicate: c.onDuplicate && ((n) => c.onDuplicate!(orig(n))),
      onDropStop: c.onDropStop && ((kind, at) => c.onDropStop!(kind, at === null ? null : at + off)),
      renderInBox: c.renderInBox && ((n) => c.renderInBox!(orig(n))),
      sizeOf: c.sizeOf && ((n, b) => c.sizeOf!(orig(n), b)),
    },
  };
}

interface CardProps {
  inst: Instance;
  w: number;
  h: number;
  zoom: number;
  onSize(w: number, h: number): void;
  onClose(): void;
}

/** A box opened into its session: the head (the name, ▣), the real pane, and
 * the corner that resizes it (plane px — the pointer's travel over the zoom). */
function PaneCard({ inst, w, h, zoom, onSize, onClose }: CardProps) {
  const grab = useRef<{ id: number; sx: number; sy: number; w: number; h: number } | null>(null);
  const [live, setLive] = useState<{ w: number; h: number } | null>(null);
  const cur = live || clampPane(w, h);
  const z = zoom || 1;

  const down = (e: ReactPointerEvent<HTMLSpanElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    grab.current = { id: e.pointerId, sx: e.clientX, sy: e.clientY, w: cur.w, h: cur.h };
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* a synthetic event has no capture */
    }
  };
  const move = (e: ReactPointerEvent<HTMLSpanElement>) => {
    const g = grab.current;
    if (!g || g.id !== e.pointerId) return;
    setLive(clampPane(g.w + (e.clientX - g.sx) / z, g.h + (e.clientY - g.sy) / z));
  };
  const up = (e: ReactPointerEvent<HTMLSpanElement>) => {
    const g = grab.current;
    if (!g || g.id !== e.pointerId) return;
    grab.current = null;
    // Persisted once, on release; the drag itself only redraws.
    if (live) onSize(live.w, live.h);
    setLive(null);
  };

  return (
    <div
      className="skl-card sky-own-scroll"
      data-no-drag
      style={{ width: cur.w, height: cur.h + CARD_HEAD_H }}
      // A double-click on the card's top — its own head, or the pane's head
      // under it (not its buttons, tabs or fields) — folds it back into a box.
      onDoubleClick={(e) => {
        const t = e.target as HTMLElement;
        if (!t.closest(".skl-card-head, .pane-head") || t.closest("button, input, textarea, select, a, [role='tab'], [contenteditable='true']")) return;
        e.preventDefault();
        e.stopPropagation();
        onClose();
      }}
    >
      <div className="skl-card-head" title="Double-click to fold it back into the route">
        <span className="skl-card-name">{windowName(inst.title)}</span>
        <button type="button" className="skl-card-fold" aria-label={"Fold " + windowName(inst.title) + " back into the route"} title="Back to the route" onClick={onClose}>
          ▣
        </button>
      </div>
      <div className="skl-card-pane" style={{ height: cur.h }}>
        {/* Keyed by title: a pane open in the grid too keeps its terminal. */}
        <Pane key={inst.title} inst={inst} embedded />
      </div>
      <span
        className="skl-resize"
        role="separator"
        tabIndex={0}
        aria-label={"Resize " + windowName(inst.title) + "'s card"}
        title="Drag to resize"
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={up}
        onPointerCancel={up}
      />
    </div>
  );
}

/** The agent tile's state chip. A session held offline after a crash
 * (lib/heldOffline) says its remedy — "Offline · Start" — and pressing it
 * starts the agent (the rail chip's Start); every other state is a label. */
export function SkyStateChip({ ss, title, className }: { ss: SessionState; title: string; className: string }) {
  const [busy, setBusy] = useState(false);
  if (!ss.held)
    return (
      <span className={className} title={ss.hint}>
        {ss.label}
      </span>
    );
  return (
    <button
      type="button"
      className={className + " sky-held-start"}
      title={ss.hint}
      aria-label="Offline since a crash — start the agent"
      disabled={busy}
      // The tile drags and opens on the pointer: the chip is its own button.
      onPointerDown={(e) => e.stopPropagation()}
      onDoubleClick={(e) => e.stopPropagation()}
      onClick={(e) => {
        e.stopPropagation();
        setBusy(true);
        void startSession(title).finally(() => setBusy(false));
      }}
    >
      {busy ? "Starting…" : HELD_CHIP}
    </button>
  );
}

export function SkyLine(p: SkyLineProps): JSX.Element {
  const flights = useFlights().data;
  const kinds = useFlightKinds().data;
  const src: LineSource = useMemo(() => ({ rows: p.rows, flights, kinds: kinds || NO_KINDS, plans: p.plans }), [p.rows, flights, kinds, p.plans]);
  const line = useSessionLine(p.title, src, { family: true, askRunning: true, listenBehaviors: true, onSession: (t) => selectSession(t) });
  // A rename redraws the line (its name is read through windowName).
  useUi((s) => s.aliases[p.title]);
  const name = windowName(p.title);
  // What has flown stays as tiles until this line folds it into one pill
  // (`line.foldFlown`): the hook draws the diagram with the fold and the
  // open groups.
  const { foldFlown, setFoldFlown } = line;
  const folded = useMemo(() => sessionDiagram(p.rows, p.title, { flights, kinds, family: true, foldFlown: true }), [p.rows, p.title, flights, kinds]);
  const folds = flownCount(folded) > 0;
  // Workers' open cards, by the worker's title (their pane sizes with them).
  const [kids, setKids] = useState<Record<string, { w: number; h: number }>>({});
  const rootRef = useRef<HTMLDivElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef(p.onMeasure);
  measureRef.current = p.onMeasure;
  // What the tiles show at this zoom (far / mid / near).
  const lod = lodFor(p.zoom || 1);

  // The host lays the lines out from what each one measures (plane px: the
  // zoom is a transform, offset sizes don't see it) — the chain's graph box,
  // its label and any open card.
  useEffect(() => {
    const el = rootRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    let last = "";
    const read = () => {
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      const k = w + "x" + h;
      if (!w || !h || k === last) return;
      last = k;
      measureRef.current?.(w, h);
    };
    const ro = new ResizeObserver(read);
    ro.observe(el);
    read();
    return () => ro.disconnect();
  }, []);

  // A worker that left (closed, re-parented) takes its card with it.
  useEffect(() => {
    const gone = Object.keys(kids).filter((t) => !p.rows.some((r) => r.title === t && r.parent === p.title));
    if (gone.length) setKids((k) => Object.fromEntries(Object.entries(k).filter(([t]) => !gone.includes(t))));
  }, [p.rows, p.title, kids]);

  const row = line.row;
  const ss = deriveSessionState(row || {});
  // Round 21: the frame's state (its border), from the route and the session.
  const frame = row
    ? frameState({ flight: line.flight?.state || (row.flight as FlightRow | null | undefined)?.state, landed: !!line.landedId, tone: ss.tone, behaviorsYou: line.behaviors.mark?.tone === "you" })
    : "idle";
  const rowOf = (t: string | undefined) => (t ? p.rows.find((r) => r.title === t) : undefined);

  // Round 18: the line is a CHAIN (lib/skyGraph): the session tile heads it
  // (its name, state chip and bird, in the session's colour), its stops are
  // free-floating tiles at their saved spots, wires join their ports.
  const where = row ? lineWhere(line.diagram) : "";
  const moves = useNodeMoves(line.flight?.id, line.graphDiagram, p.title);
  const sessionNode = useMemo(() => moves.diagram.nodes.find((n) => n.kind === "session" && n.row === "main"), [moves.diagram]);
  // The head is the session's AGENT (round 20): its name, what it is doing
  // now; where the flight is goes on the frame's bar.
  const agent = agentTitle(row?.provider || row?.program, row?.program);
  // While ready, ReadyHead (on the frame's bar) says so; the sub never repeats it
  // (the task headline — read from its newest meaningful prompt — else the
  // create task, else the last prompt).
  const headSub = row ? firstLine(row.task_headline || row.task || "") || firstLine(row.last_prompt || "") : "";
  const head: ChainHead = useMemo(
    () => ({ kind: "session", title: agent, sub: headSub, session: p.title, color: chainColor(p.title), node: sessionNode }),
    [agent, headSub, p.title, sessionNode]
  );
  // Each sub-session's frame: its agent and its own chain, if it flies one.
  const subFrames = useMemo(() => subFramesOf(p.rows, p.title, flights, kinds), [p.rows, p.title, flights, kinds]);
  // An open sub-session's card takes its tile's place: the graph gives it a
  // card-sized box and the other branches make room (the owner: sub-sessions
  // should open like the session does, not below).
  // Every other sub-session takes its small frame's box.
  // The session's behaviors (docs/behaviors.md §1): the LAST tile of the
  // chain (or its Verify stop) — already on `line.graphDiagram` — whose card
  // opens in the tile's place.
  const behaviors = line.behaviors;
  const big = useMemo(() => {
    const open: Record<string, { w: number; h: number }> = {};
    for (const [t, ks] of Object.entries(kids)) {
      const c = clampPane(ks.w, ks.h);
      open[t] = { w: c.w, h: c.h + CARD_HEAD_H };
    }
    return { ...subFrameBoxes(moves.diagram.nodes, subFrames, open), ...behaviors.big(moves.diagram) };
  }, [kids, moves.diagram, subFrames, behaviors.big]);
  const graph = useMemo(() => chainGraph(hangFromAgent(moves.diagram), { head, key: p.title, flightId: line.flight?.id || "", big }), [moves.diagram, head, p.title, line.flight?.id, big]);
  useChainEntry(p.registerChain, graph, wrapRef, line.paletteAfter, line.paletteParallel);
  // The frame grows to a tile dragged left of / above the chain's origin.
  const frameX = Math.min(0, Math.floor(graph.bbox.x));
  const frameY = Math.min(0, Math.floor(graph.bbox.y));
  useTourHooks(wrapRef, { ".sky-head": "sky-session-box", ".sky-node:not(.sky-head):not(.sky-subframe)": "sky-stop" });
  const gc = line.graphCanvas;
  // Round 18: a tile's right-click menu; a Loop's pill dragged onto a stop re-aims it.
  const chainMenu = useChainMenu(gc, line.flight?.id || line.landedId, { steps: line.flight?.steps, kinds });
  const agentMenu = useAgentMenu();
  const onPillDown = pillAimer(graph, gc.onLoopTo);

  const foldKid = (t: string) => setKids((k) => Object.fromEntries(Object.entries(k).filter(([x]) => x !== t)));
  // A press on the head that moved was the line's drag, not a click on it.
  const press = useRef<{ x: number; y: number; shift: boolean; moved: boolean }>({ x: 0, y: 0, shift: false, moved: false });
  const toggleKid = (t: string) => {
    if (kids[t]) foldKid(t);
    else setKids((k) => ({ ...k, [t]: { w: PANE_W, h: PANE_H } }));
  };
  const onOpen = (n: DiagramNode) => {
    if (n.kind === "session" && press.current.moved) return;
    if (behaviors.onOpen(n)) return;
    if (n.kind === "flown") setFoldFlown((x) => !x);
    else if (n.kind === "session") p.onLayout({ open: !p.layout.open });
    else if ((n.kind === "child" || n.kind === "branch") && n.session) toggleKid(n.session);
    else gc.onOpen?.(n);
  };
  const onSelect = (n: DiagramNode) => {
    if (press.current.shift && p.onShiftSelect) p.onShiftSelect(n);
    else gc.onSelect?.(n);
  };

  // Round 20: the frame's bar renames on a double-click (as the rail's row
  // does: an alias, refused when another session already has that name).
  const setAlias = useUi((s) => s.setAlias);
  const [naming, setNaming] = useState<string | null>(null);
  const named = useRef(false);
  const startRename = () => {
    named.current = false;
    setNaming(name);
  };
  const endRename = (save: boolean) => {
    if (named.current) return;
    named.current = true;
    const next = (naming ?? "").trim();
    setNaming(null);
    rootRef.current?.focus({ preventScroll: true });
    if (!save || !next || next === name) return;
    const taken = nameTakenBy(p.title, next);
    if (taken) {
      toast("Another session is already named “" + next + "”");
      return;
    }
    setAlias(p.title, next === p.title ? "" : next);
    toast("Renamed to “" + next + "”");
  };

  const onBarDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    if ((e.target as Element).closest("button, summary, a, input, textarea, [data-no-drag]")) return;
    // Delete next lands on the line (its close question), not on the page.
    rootRef.current?.focus({ preventScroll: true });
    if (p.draggable === false) return;
    p.onDragStart?.(e);
  };
  // The head tile is the chain's handle too (SkyChain lets its press bubble).
  const onChainDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    press.current = { x: e.clientX, y: e.clientY, shift: e.shiftKey, moved: false };
    const t = e.target as Element;
    if (t.closest(".sky-head") && !t.closest("[data-port], [data-no-drag], button")) onBarDown(e);
  };

  // Delete from the sky: a stop or group tile through the line's own Remove
  // (the undoable toast; the stop a running flight is at asks first); a
  // session's tile, or the line itself, closes it — at once with Undo when
  // its agent is idle, asked first while it works or asks (round 14).
  const [closing, setClosing] = useState("");
  const [closeBusy, setCloseBusy] = useState(false);
  const askClose = (t: string) => {
    if (removalNeedsAsk({ kind: "session", row: p.rows.find((r) => r.title === t) })) setClosing(t);
    else void closeLine(t);
  };
  const deleteBox: LineDelete = (node) => {
    const t = node && node !== HEAD ? nodeSession(node) : p.title;
    if (t) {
      askClose(t);
      return true;
    }
    const n = nodeById(line.graphDiagram.nodes, node);
    // The stop the flight is at can't change, but it can come off (it asks
    // first while the flight runs: useSessionLine's `removeAsk`).
    const here = !!n && !!line.flight && (n.kind === "stop" || n.kind === "group") && nodeIndexes(n).includes(line.flight.cursor);
    if (!(removableBox(n) || here) || !gc.onRemove) return false;
    gc.onRemove(n!);
    return true;
  };
  const deleteRef = useRef(deleteBox);
  deleteRef.current = deleteBox;
  const register = p.registerDelete;
  useEffect(() => {
    if (!register) return;
    register((node) => deleteRef.current(node));
    return () => register(null);
  }, [register]);
  const closeNow = async () => {
    setCloseBusy(true);
    try {
      await closeLine(closing);
    } finally {
      setCloseBusy(false);
      setClosing("");
    }
  };

  const promote = usePromoteStops(line.flight);
  // The ⋯, in groups (a group's first item draws the rule above it): the
  // session · how the line looks · what flies on it · closing it.
  const items: MenuItem[] = [
    p.layout.open
      ? { key: "open", label: "Fold the session back", title: "Back to its agent's stop on the route", onSelect: () => p.onLayout({ open: false }) }
      : { key: "open", label: "Open session", title: "Its agent's terminal, here in place of the agent's stop", onSelect: () => p.onLayout({ open: true }) },
    {
      key: "grid",
      label: "Open in the grid",
      title: "Switch to the grid with this session selected",
      onSelect: () => {
        selectSession(p.title);
        setView("grid");
      },
    },
    { key: "only", label: "Show only this route", title: "This session: one route, filling the sky", onSelect: () => showInSky(p.title) },
    ...(folds
      ? [{ key: "flown", label: foldFlown ? "Show finished stops" : "Fold finished stops", title: "The stops this route has already run: each on its own, or one pill", className: "skl-mi-group", onSelect: () => setFoldFlown((x) => !x) }]
      : []),
    p.layout.pinned
      ? { key: "tidy", label: "Tidy this route", title: "Forget where you put it: back to its place in the rows", onSelect: () => p.onLayout({ pinned: false }) }
      : { key: "pin", label: "Pin here", title: "Keep this route where it is when the others move", onSelect: () => p.onLayout({ x: p.layout.x, y: p.layout.y, pinned: true }) },
    { key: "flyway", label: "Add a stop…", title: "A stop or a saved route, at the end of the route", className: "skl-mi-group", onSelect: () => line.togglePalette(true) },
    // A ticket's line: its stops for every NEW item from its source (this one
    // keeps flying as it is) — the only way a line changes a source's default.
    ...(promote ? [{ key: "promote", label: promote.label, title: promote.title, className: "skl-mi-promote", onSelect: () => void promote.run() }] : []),
    { key: "close", label: "Close session", danger: true, className: "skl-mi-group", title: "Stops the agent and moves it to Recently closed", onSelect: () => askClose(p.title) },
  ];
  // Round 20: the agent tile's right-click menu — the agent's own items, then
  // the session's ⋯ (under a label line). A sub-session's agent: its own.
  const agentItems = (t: string, at: { clientX: number; clientY: number }): AgentMenuItem[] => {
    const own = t === p.title;
    const r = own ? row : rowOf(t);
    const mine = agentMenuItems(r, {
      opened: own ? p.layout.open : !!kids[t],
      openTerminal: () => (own ? p.onLayout({ open: !p.layout.open }) : toggleKid(t)),
      compose: () => agentMenu.compose(t, at),
    });
    if (!own) return [...mine, { key: "agent-go", label: "Open in the grid", title: "Switch to the grid with " + windowName(t) + " selected", onSelect: () => (selectSession(t), setView("grid")) }];
    const session = items.filter((i) => i.key !== "open").map((i): AgentMenuItem => ({ key: "s-" + i.key, label: i.label, title: i.title || "", danger: i.danger, onSelect: i.onSelect }));
    return [...mine, { key: "s-note", label: "Session “" + name + "”", title: "The session round this agent", note: true }, ...session];
  };
  const onMenu = (n: DiagramNode, at: { clientX: number; clientY: number }) => {
    if (behaviors.onMenu(n, at)) return;
    const t = n.kind === "session" ? p.title : (n.kind === "child" || n.kind === "branch") && n.session ? n.session : "";
    if (t) agentMenu.open(t, at, agentItems(t, at));
    else chainMenu.onMenu(n, at);
  };
  // Round 15: the time under the chain, and how long the row has been quiet.
  const segments = useMemo(() => segmentsFor(line.flight), [line.flight]);
  const age = ageOf(row?.activity_since);
  const still = ageWord(row?.activity_since);

  // The session's card opens IN its tile's place (the owner: "open up like in
  // its box, so it is clear where the session is"): the card sits where the
  // tile was, the chain moves right of it, and the head's wire leaves the
  // card's right edge. Each opened worker's card goes in a row under it.
  const cards: ReactNode[] = [];
  const own = p.layout.open && row ? clampPane(p.layout.paneW ?? PANE_W, p.layout.paneH ?? PANE_H) : null;
  const ownCard =
    own && row ? (
      <div className="skl-own-card">
        <PaneCard key={row.title} inst={row} w={own.w} h={own.h} zoom={p.zoom} onSize={(w, h) => p.onLayout({ paneW: w, paneH: h })} onClose={() => p.onLayout({ open: false })} />
      </div>
    ) : null;
  // The head's out port (chain-local) lands on the card's right edge.
  const cardShift = own ? Math.max(0, own.w - (NODE_W + TILE) / 2) : 0;
  // A worker that is not drawn on the chain (no tile to open in) keeps the
  // row under it; the ones on the chain open in their tile's place.
  const onChain = new Set(graph.nodes.map((n) => n.node.session).filter(Boolean));
  for (const [t, ks] of Object.entries(kids)) {
    const kid = rowOf(t);
    if (kid && !onChain.has(t)) cards.push(<PaneCard key={t} inst={kid} w={ks.w} h={ks.h} zoom={p.zoom} onSize={(w, h) => setKids((k) => ({ ...k, [t]: { w, h } }))} onClose={() => foldKid(t)} />);
  }
  const renderOpen = (n: DiagramNode): ReactNode => {
    const bh = behaviors.renderOpen(n);
    if (bh) return bh;
    const t = n.session;
    const ks = t ? kids[t] : undefined;
    const kid = t && ks ? rowOf(t) : undefined;
    if (!t || !ks || !kid) return null;
    return <PaneCard key={t} inst={kid} w={ks.w} h={ks.h} zoom={p.zoom} onSize={(w, h) => setKids((k) => ({ ...k, [t]: { w, h } }))} onClose={() => foldKid(t)} />;
  };

  return (
    <div
      ref={rootRef}
      className={
        "skl skl-chainline skl-frame" +
        (p.focused ? " is-focused" : "") +
        (p.selected ? " is-selected" : "") +
        (p.layout.open ? " is-open" : "") +
        (p.dropFlyway ? " is-drop" : "") +
        (p.mergeTarget ? " is-merge-target" : "") +
        (line.editing ? " is-editing" : "") +
        (behaviors.frameClass ? " " + behaviors.frameClass : "")
      }
      data-line={p.title}
      data-frame={frame}
      data-lod={lod}
      data-age={age || undefined}
      // The sky's zoom, for CSS that counter-scales (a pill, the level of
      // detail a tile shows): `--sky-z` on every line.
      style={{ "--sky-z": p.zoom || 1, "--sky-lines": nearLines(p.zoom || 1), "--sky-chain-color": head.color } as React.CSSProperties}
      role="group"
      tabIndex={-1}
      data-sky-box
      aria-label={name + "'s route"}
      onPointerDownCapture={() => {
        if (!p.focused) p.onFocus();
      }}
    >
      {/* Round 20: the session's frame — a soft box round the chain (grown to
          a tile dragged left of / above the origin) — and its title bar: the
          handle (drag), the name (double-click renames), the branch, the
          session's state, where the flight is, the ⋯. */}
      <div className="skl-frame-box" aria-hidden="true" style={{ left: frameX, top: frameY }} />
      <div
        className="skl-bar skl-label skl-frame-bar"
        data-tour="sky-line-bar"
        style={frameX || frameY ? { marginLeft: frameX, transform: frameY ? `translateY(${frameY}px)` : undefined } : undefined}
        title={(p.draggable === false ? "Click to focus this session (drag mode moves it: V)" : "Drag to move this session") + " · double-click to rename"}
        onPointerDown={onBarDown}
        onDoubleClick={(e) => {
          if ((e.target as Element).closest("button, summary, a, input, [data-no-drag]")) return;
          e.preventDefault();
          e.stopPropagation();
          startRename();
        }}
      >
        {/* Round 21: the session's identity colour, only as this dot. */}
        <span className="skl-id-dot" aria-hidden="true" />
        {naming === null ? (
          <span className="skl-name">{name}</span>
        ) : (
          <input
            className="fl-input skl-name-input"
            aria-label={"Rename “" + name + "”"}
            value={naming}
            maxLength={120}
            autoFocus
            data-no-drag
            onChange={(e) => setNaming(e.target.value)}
            onFocus={(e) => e.target.select()}
            onBlur={() => endRename(true)}
            onKeyDown={(e) => {
              e.stopPropagation();
              if (e.key === "Enter") {
                e.preventDefault();
                endRename(true);
              } else if (e.key === "Escape") {
                e.preventDefault();
                endRename(false);
              }
            }}
          />
        )}
        {row?.branch && (
          <span className="skl-branch" title={"Branch " + row.branch}>
            {row.branch}
          </span>
        )}
        {row && (
          <SkyStateChip ss={ss} title={row.title} className={"skl-chip fl-box-chip skl-state-chip tone-" + ss.tone} />
        )}
        {behaviors.mark?.tone === "you" && ss.tone !== "attention" && (
          // The Needs-you mark: a behavior waits on you (docs/behaviors.md §1).
          <span className="skl-chip fl-box-chip skl-state-chip tone-attention skl-bh-needs" title="A behavior waits on you — open the Behaviors stop to answer it">
            Needs you
          </span>
        )}
        {where && (
          <span className="skl-where" title={where}>
            {where}
          </span>
        )}
        {still && (
          <span className="skl-age" title={"Nothing has happened on this session for " + still.replace(/ still$/, "")}>
            {still}
          </span>
        )}
        {p.layout.pinned && (
          <span className="skl-pin" title="Pinned: the sky's layout leaves this route where you put it" aria-label="Pinned">
            📌
          </span>
        )}
        {/* Round 21 (JUDGE-R21-2 #8): a ready route's ONE ▶ Run lives here,
            on the bar, visible wherever the camera is — not on the agent tile. */}
        {line.flight?.state === "ready" && <ReadyHead flight={line.flight} />}
        <span className="skl-menu" data-no-drag>
          <OverflowMenu label={"Actions for " + name} items={items} />
        </span>
      </div>
      {line.cancel.node}
      {line.removeAsk}
      {closing && (
        <div className="skl-close-confirm" data-no-drag>
          <InlineConfirm
            title={"Close “" + windowName(closing) + "”?"}
            body={"Its agent " + runningWord(p.rows.find((r) => r.title === closing)) + " right now: it stops, and the session moves to Recently closed (Ctrl+Z or Ctrl+Shift+T brings it back)."}
            confirmLabel="Close session"
            busy={closeBusy}
            onConfirm={() => void closeNow()}
            onCancel={() => {
              setClosing("");
              rootRef.current?.focus({ preventScroll: true });
            }}
          />
        </div>
      )}
      {row ? (
        <div
          ref={wrapRef}
          className={"skl-chain-wrap" + (ownCard ? " has-card" : "")}
          style={own ? { paddingLeft: cardShift, minHeight: own.h + CARD_HEAD_H } : undefined}
          onPointerDownCapture={onChainDown}
          onPointerMoveCapture={(e) => {
            if (!press.current.moved && Math.hypot(e.clientX - press.current.x, e.clientY - press.current.y) >= 4) press.current.moved = e.buttons !== 0;
          }}
        >
          {ownCard}
          <SkyChain
            graph={graph}
            zoom={p.zoom}
            selected={gc.selected}
            highlighted={p.highlighted}
            targets={p.targets}
            over={p.over}
            bird={birdPerch(moves.diagram)}
            onSelect={onSelect}
            onEdit={gc.onEdit}
            onOpen={onOpen}
            onRemove={gc.onRemove}
            onLoopPill={(via) => gc.onEdit?.(via)}
            onPillDown={onPillDown}
            onMenu={onMenu}
            kinds={kinds}
            subFrames={subFrames}
            onNodeMove={moves.move}
            onPortDown={p.onPortDown}
            onPlus={(after) => (after && after.id !== HEAD ? line.paletteAfter(after.node, null) : line.togglePalette(true))}
            renderOpen={renderOpen}
            renderInBox={gc.renderInBox ? (n) => gc.renderInBox!(n) as ReactNode : undefined}
            renderHead={() => (
              row ? (
                <SkyStateChip ss={ss} title={row.title} className={"sky-head-chip fl-box-chip tone-" + ss.tone} />
              ) : (
                <span className={"sky-head-chip fl-box-chip tone-" + ss.tone} title={ss.hint}>
                  {ss.label}
                </span>
              )
            )}
          />
          {chainMenu.menu}
          {agentMenu.menu}
          {behaviors.menu}
          <ChainPanels graph={graph} anchor={line.anchor} anchorPos={line.anchorPos}>
            {line.palette}
            {line.inspector}
          </ChainPanels>
        </div>
      ) : (
        <p className="skl-gone muted">This session isn't running any more.</p>
      )}
      {row && lod !== "far" && <LineRuler segments={segments} />}
      {cards.length > 0 && <div className="skl-cards">{cards}</div>}
    </div>
  );
}
