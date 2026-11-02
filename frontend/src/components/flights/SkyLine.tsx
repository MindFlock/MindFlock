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
 * line, so Delete has somewhere to land. */

import { useEffect, useMemo, useRef, useState, type JSX, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { setView, showInSky } from "../../lib/viewMode";
import { BOX_PRESETS, boxScale, clampBox, lodFor, nearLines } from "../../lib/skyLayout";
import { windowName } from "../../lib/windowName";
import { useUi } from "../../state/store";
import type { Instance } from "../../api/types";
import { queryClient, useFlightKinds, useFlights } from "../../state/queries";
import { closeSession, selectSession, undoLastClose } from "../../lib/sessionActions";
import { nodeById, nodeSession, removableBox, removalNeedsAsk } from "../../lib/skySelect";
import { pushUndo } from "../../lib/skyUndo";
import { deriveSessionState } from "../../lib/sessionState";
import { firstLine } from "../../lib/format";
import { birdPerch, drawnIndexes, flownCount, lineWhere, sessionDiagram, type Diagram, type DiagramNode } from "../../lib/flightDiagram";
import { NODE_W, TILE, chainColor, chainGraph, type ChainHead, type GraphNode } from "../../lib/skyGraph";
import { ageOf, ageWord, fmtDuration, segmentTitle, segmentsFor, totalSeconds, type LineSegment } from "../../lib/lineTime";
import type { FlightPlan, StepKindDef } from "../../lib/flights";
import { InlineConfirm } from "../InlineConfirm";
import { OverflowMenu, type MenuItem } from "../OverflowMenu";
import { Pane } from "../grid/Pane";
import type { BoxSizeName } from "./FlywayCanvas";
import { pillAimer, useChainMenu } from "./useChainMenu";
import { ReadyHead, startWord } from "./ReadyHead";
import { SkyChain } from "./SkyChain";
import { ChainPanels } from "./SkyWires";
import { HEAD, useChainEntry, useNodeMoves, useTourHooks, type ChainEntry } from "./useSkyWiring";
import { nodeIndexes, useSessionLine, type LineSource } from "./useSessionLine";
import { Bird } from "./Bird";
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
    <div className="skl-ruler" role="img" aria-label={"Time flown: " + fmtDuration(total) + " over " + segments.length + (segments.length === 1 ? " stop" : " stops")} data-no-drag>
      <div className="skl-ruler-bar">
        {segments.map((s) => (
          <span key={s.step} className={"skl-ruler-seg skl-ruler-" + s.state} style={{ flexGrow: Math.max(s.seconds, total / 200) }} title={segmentTitle(s)} data-step={s.step} />
        ))}
      </div>
      <span className="skl-ruler-total" title={"Time flown so far"}>
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
      <div className="skl-card-head" title="Double-click to fold it back into a box">
        <span className="skl-card-name">{windowName(inst.title)}</span>
        <button type="button" className="skl-card-fold" aria-label={"Fold " + windowName(inst.title) + " back into a box"} title="Back to a box" onClick={onClose}>
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

export function SkyLine(p: SkyLineProps): JSX.Element {
  const flights = useFlights().data;
  const kinds = useFlightKinds().data;
  const src: LineSource = useMemo(() => ({ rows: p.rows, flights, kinds: kinds || NO_KINDS, plans: p.plans }), [p.rows, flights, kinds, p.plans]);
  const line = useSessionLine(p.title, src, { family: true, askRunning: true, onSession: (t) => selectSession(t) });
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
  const rowOf = (t: string | undefined) => (t ? p.rows.find((r) => r.title === t) : undefined);

  // Round 18: the line is a CHAIN (lib/skyGraph): the session tile heads it
  // (its name, state chip and bird, in the session's colour), its stops are
  // free-floating tiles at their saved spots, wires join their ports.
  const where = row ? lineWhere(line.diagram) : "";
  const moves = useNodeMoves(line.flight?.id, line.graphDiagram, p.title);
  const sessionNode = useMemo(() => moves.diagram.nodes.find((n) => n.kind === "session" && n.row === "main"), [moves.diagram]);
  const headSub = line.flight?.state === "ready" ? "ready · " + startWord(line.flight) : where || (row ? firstLine(row.task_headline || row.task || "") : "");
  const head: ChainHead = useMemo(
    () => ({ kind: "session", title: name, sub: headSub, session: p.title, color: chainColor(p.title), node: sessionNode }),
    [name, headSub, p.title, sessionNode]
  );
  const graph = useMemo(() => chainGraph(moves.diagram, { head, key: p.title, flightId: line.flight?.id || "" }), [moves.diagram, head, p.title, line.flight?.id]);
  useChainEntry(p.registerChain, graph, wrapRef, line.paletteAfter);
  useTourHooks(wrapRef, { ".sky-head": "sky-session-box", ".sky-node:not(.sky-head)": "sky-stop" });
  const gc = line.graphCanvas;
  // Round 18: a tile's right-click menu; a Loop's pill dragged onto a stop re-aims it.
  const chainMenu = useChainMenu(gc, line.flight?.id || "");
  const onPillDown = pillAimer(graph, gc.onLoopTo);

  const foldKid = (t: string) => setKids((k) => Object.fromEntries(Object.entries(k).filter(([x]) => x !== t)));
  // A press on the head that moved was the line's drag, not a click on it.
  const press = useRef<{ x: number; y: number; shift: boolean; moved: boolean }>({ x: 0, y: 0, shift: false, moved: false });
  const onOpen = (n: DiagramNode) => {
    if (n.kind === "session" && press.current.moved) return;
    if (n.kind === "flown") setFoldFlown((x) => !x);
    else if (n.kind === "session") p.onLayout({ open: !p.layout.open });
    else if ((n.kind === "child" || n.kind === "branch") && n.session) {
      const t = n.session;
      if (kids[t]) foldKid(t);
      else setKids((k) => ({ ...k, [t]: { w: PANE_W, h: PANE_H } }));
    } else gc.onOpen?.(n);
  };
  const onSelect = (n: DiagramNode) => {
    if (press.current.shift && p.onShiftSelect) p.onShiftSelect(n);
    else gc.onSelect?.(n);
  };

  const onBarDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    if ((e.target as Element).closest("button, summary, a, input, [data-no-drag]")) return;
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

  // The ⋯, in groups (a group's first item draws the rule above it): the
  // session · how the line looks · what flies on it · closing it.
  const items: MenuItem[] = [
    p.layout.open
      ? { key: "open", label: "Fold the session back", title: "Back to its tile on the chain", onSelect: () => p.onLayout({ open: false }) }
      : { key: "open", label: "Open session", title: "Open the session here, in a card under its chain", onSelect: () => p.onLayout({ open: true }) },
    {
      key: "grid",
      label: "Open in the grid",
      title: "Switch to the grid with this session selected",
      onSelect: () => {
        selectSession(p.title);
        setView("grid");
      },
    },
    { key: "only", label: "Show only this line", title: "This session: one chain, filling the sky", onSelect: () => showInSky(p.title) },
    ...(folds
      ? [{ key: "flown", label: foldFlown ? "Show flown stops" : "Fold flown stops", title: "The stops this flight has already flown: each on its own, or one pill", className: "skl-mi-group", onSelect: () => setFoldFlown((x) => !x) }]
      : []),
    p.layout.pinned
      ? { key: "tidy", label: "Tidy this line", title: "Forget where you put it: back to its place in the rows", onSelect: () => p.onLayout({ pinned: false }) }
      : { key: "pin", label: "Pin here", title: "Keep this chain where it is when the others move", onSelect: () => p.onLayout({ x: p.layout.x, y: p.layout.y, pinned: true }) },
    { key: "flyway", label: "Add a stop…", title: "A stop or a saved stop, at the end of the chain", className: "skl-mi-group", onSelect: () => line.togglePalette(true) },
    { key: "close", label: "Close session", danger: true, className: "skl-mi-group", title: "Stops the agent and moves it to Recently closed", onSelect: () => askClose(p.title) },
  ];
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
  for (const [t, ks] of Object.entries(kids)) {
    const kid = rowOf(t);
    if (kid) cards.push(<PaneCard key={t} inst={kid} w={ks.w} h={ks.h} zoom={p.zoom} onSize={(w, h) => setKids((k) => ({ ...k, [t]: { w, h } }))} onClose={() => foldKid(t)} />);
  }

  return (
    <div
      ref={rootRef}
      className={
        "skl skl-chainline" +
        (p.focused ? " is-focused" : "") +
        (p.selected ? " is-selected" : "") +
        (p.layout.open ? " is-open" : "") +
        (p.dropFlyway ? " is-drop" : "") +
        (p.mergeTarget ? " is-merge-target" : "") +
        (line.editing ? " is-editing" : "")
      }
      data-line={p.title}
      data-lod={lod}
      data-age={age || undefined}
      // The sky's zoom, for CSS that counter-scales (a pill, the level of
      // detail a tile shows): `--sky-z` on every line.
      style={{ "--sky-z": p.zoom || 1, "--sky-lines": nearLines(p.zoom || 1), "--sky-chain-color": head.color } as React.CSSProperties}
      role="group"
      tabIndex={-1}
      data-sky-box
      aria-label={name + "'s chain"}
      onPointerDownCapture={() => {
        if (!p.focused) p.onFocus();
      }}
    >
      {/* The chain's label: its handle (drag) and its ⋯ — a small tag over
          the chain, no card around it. */}
      <div className="skl-bar skl-label" data-tour="sky-line-bar" title={p.draggable === false ? "Click to focus this chain (drag mode moves it: V)" : "Drag to move this chain"} onPointerDown={onBarDown}>
        <Bird size={14} />
        <span className="skl-name">{name}</span>
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
          <span className="skl-pin" title="Pinned: the sky's layout leaves this chain where you put it" aria-label="Pinned">
            📌
          </span>
        )}
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
            onMenu={chainMenu.onMenu}
            kinds={kinds}
            onNodeMove={moves.move}
            onPortDown={p.onPortDown}
            onPlus={(after) => (after && after.id !== HEAD ? line.paletteAfter(after.node, null) : line.togglePalette(true))}
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
