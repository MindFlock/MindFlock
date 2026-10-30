/** One session's line in the Sky (docs/flights-sky-line.md): its title bar,
 * then its session diagram — the session box, its workers, the fast-track's
 * rungs or the flight's stops, the trailing + — with the bird on the box the
 * flight is at.
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
import { BOX_PRESETS, boxScale, boxTier, clampBox, lineBox, lodFor, nearLines } from "../../lib/skyLayout";
import { windowName } from "../../lib/windowName";
import { useUi } from "../../state/store";
import type { Instance } from "../../api/types";
import { useFlightKinds, useFlights } from "../../state/queries";
import { closeSession, selectSession } from "../../lib/sessionActions";
import { nodeById, nodeSession, removableBox } from "../../lib/skySelect";
import { deriveSessionState } from "../../lib/sessionState";
import { firstLine } from "../../lib/format";
import { birdPerch, drawnIndexes, flownCount, lineWhere, sessionDiagram, type Diagram, type DiagramNode } from "../../lib/flightDiagram";
import { ageOf, ageWord, fmtDuration, segmentTitle, segmentsFor, totalSeconds, type LineSegment } from "../../lib/lineTime";
import type { FlightPlan } from "../../lib/flights";
import { InlineConfirm } from "../InlineConfirm";
import { OverflowMenu, type MenuItem } from "../OverflowMenu";
import { Pane } from "../grid/Pane";
import { FlywayCanvas, type BoxSizeName } from "./FlywayCanvas";
import { useSessionLine, type LineSource } from "./useSessionLine";
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
}

/** The sky's handle on a line's Delete: a box's node id ("" = the line). */
export type LineDelete = (node: string) => boolean;

/** An open card's pane, in plane px: its default and its floor. */
export const PANE_W = 720;
export const PANE_H = 460;
export const PANE_MIN_W = 360;
export const PANE_MIN_H = 240;
/** The card's own head (the name and ▣) above the pane. */
export const CARD_HEAD_H = 30;

const SIZES: readonly BoxSizeName[] = ["compact", "normal", "wide"];
const SIZE_WORD: Record<BoxSizeName, string> = { compact: "compact — titles only", normal: "normal", wide: "wide — the full text" };

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

/** The ⋯ menu's three box sizes, in words. */
const SIZE_ITEM: Record<BoxSizeName, string> = { compact: "Compact boxes", normal: "Normal boxes", wide: "Wide boxes" };

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
    <div className="skl-card sky-own-scroll" data-no-drag style={{ width: cur.w, height: cur.h + CARD_HEAD_H }}>
      <div className="skl-card-head">
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
  const src: LineSource = useMemo(() => ({ rows: p.rows, flights, kinds: kinds || [], plans: p.plans }), [p.rows, flights, kinds, p.plans]);
  const line = useSessionLine(p.title, src, { family: true, onSession: (t) => selectSession(t) });
  // A rename redraws the line (its name is read through windowName).
  useUi((s) => s.aliases[p.title]);
  const name = windowName(p.title);
  // What has flown stays as boxes until this line folds it into one pill
  // (`line.foldFlown`): the hook draws the diagram with the fold and the
  // open groups, and its canvas already speaks drawn places.
  const { foldFlown, setFoldFlown } = line;
  const folded = useMemo(() => sessionDiagram(p.rows, p.title, { flights, kinds, family: true, foldFlown: true }), [p.rows, p.title, flights, kinds]);
  const folds = flownCount(folded) > 0;
  const shown = { diagram: line.diagram, canvas: line.canvas };
  // Workers' open cards, by the worker's title (their pane sizes with them).
  const [kids, setKids] = useState<Record<string, { w: number; h: number }>>({});
  const rootRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef(p.onMeasure);
  measureRef.current = p.onMeasure;
  // The boxes' size: the corner's drag while it lasts, else the layout's.
  const [liveBox, setLiveBox] = useState<{ w: number; h: number } | null>(null);
  const box = liveBox || lineBox(p.layout);
  // The tier the canvas draws and the uniform scale to the size asked for.
  const { size, k } = boxScale(box.w);
  const scaled = Math.abs(k - 1) > 0.001;
  const z = p.zoom || 1;
  // What the boxes show at this zoom (far / mid / near); their size never
  // changes with it — only what is drawn inside and the type on screen.
  const lod = lodFor(p.zoom || 1);
  // The canvas's own size (layout px, before the scale) sizes the box that
  // holds it, so the line measures what it draws.
  const innerRef = useRef<HTMLDivElement>(null);
  const sizerRef = useRef<HTMLDivElement>(null);
  const [inner, setInner] = useState<{ w: number; h: number } | null>(null);
  // The canvas (and so the box observed) exists only while the session does.
  const hasRow = !!line.row;
  useEffect(() => {
    const el = innerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const read = () => {
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      setInner((c) => (c && c.w === w && c.h === h ? c : { w, h }));
    };
    const ro = new ResizeObserver(read);
    ro.observe(el);
    read();
    return () => ro.disconnect();
  }, [hasRow]);

  // The host lays the lines out from what each one measures (plane px: the
  // zoom is a transform, offset sizes don't see it).
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

  const card = (inst: Instance, w: number, h: number, onSize: (w: number, h: number) => void, onClose: () => void): { body: ReactNode; size: { w: number; h: number } } => {
    const s = clampPane(w, h);
    const body = <PaneCard inst={inst} w={s.w} h={s.h} zoom={p.zoom} onSize={onSize} onClose={onClose} />;
    if (!scaled) return { body, size: { w: s.w, h: s.h + CARD_HEAD_H } };
    // A card keeps its own size on a scaled line: it asks the canvas for its
    // room over the scale and draws scaled back (the zoom it sees is the
    // sky's again).
    const cw = s.w / k;
    const ch = (s.h + CARD_HEAD_H) / k;
    return {
      body: (
        <div className="skl-card-unscale" style={{ width: cw, height: ch }}>
          <div style={{ transform: `scale(${1 / k})`, transformOrigin: "0 0", "--sky-z": z } as React.CSSProperties}>{body}</div>
        </div>
      ),
      size: { w: cw, h: ch },
    };
  };
  const foldKid = (t: string) => setKids((k) => Object.fromEntries(Object.entries(k).filter(([x]) => x !== t)));
  const openBox = (n: DiagramNode) => {
    if (n.kind === "session" && p.layout.open && row)
      return card(row, p.layout.paneW ?? PANE_W, p.layout.paneH ?? PANE_H, (w, h) => p.onLayout({ paneW: w, paneH: h }), () => p.onLayout({ open: false }));
    // A worker below the session or a branch between two stops: its pane.
    const kid = n.kind === "child" || n.kind === "branch" ? rowOf(n.session) : undefined;
    const ks = kid ? kids[kid.title] : undefined;
    if (kid && ks)
      return card(kid, ks.w, ks.h, (w, h) => setKids((k) => ({ ...k, [kid.title]: { w, h } })), () => foldKid(kid.title));
    return null;
  };
  const onOpen = (n: DiagramNode) => {
    if (n.kind === "flown") setFoldFlown((x) => !x);
    else if (n.kind === "session") p.onLayout({ open: !p.layout.open });
    else if ((n.kind === "child" || n.kind === "branch") && n.session) {
      const t = n.session;
      if (kids[t]) foldKid(t);
      else setKids((k) => ({ ...k, [t]: { w: PANE_W, h: PANE_H } }));
    } else shown.canvas.onOpen?.(n);
  };
  // Wide: a session's box says what it was last asked.
  const detailOf = (n: DiagramNode) => {
    const r = rowOf(n.session);
    return r ? firstLine(r.last_prompt || r.task || "") : "";
  };

  const onBarDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    if ((e.target as Element).closest("button, summary, a, input, [data-no-drag]")) return;
    // Delete next lands on the line (its close question), not on the page.
    rootRef.current?.focus({ preventScroll: true });
    if (p.draggable === false) return;
    p.onDragStart?.(e);
  };

  // Delete from the sky: a stop or group box through the canvas's own Remove
  // (the undoable toast); a session's box, or the line itself, asks first.
  const [closing, setClosing] = useState("");
  const [closeBusy, setCloseBusy] = useState(false);
  const deleteBox: LineDelete = (node) => {
    const t = node ? nodeSession(node) : p.title;
    if (t) {
      setClosing(t);
      return true;
    }
    const n = nodeById(shown.diagram.nodes, node);
    if (!removableBox(n) || !shown.canvas.onRemove) return false;
    shown.canvas.onRemove(n!);
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
      await closeSession(closing);
    } finally {
      setCloseBusy(false);
      setClosing("");
    }
  };

  // The corner handle: pointer travel over the zoom = plane px, drawn live,
  // persisted once on release.
  const grab = useRef<{ id: number; sx: number; sy: number; cw: number; ch: number; bw: number } | null>(null);
  const resizeDown = (e: ReactPointerEvent<HTMLSpanElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const el = sizerRef.current;
    grab.current = { id: e.pointerId, sx: e.clientX, sy: e.clientY, cw: el?.offsetWidth || 0, ch: el?.offsetHeight || 0, bw: box.w };
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* a synthetic event has no capture */
    }
  };
  const resizeMove = (e: ReactPointerEvent<HTMLSpanElement>) => {
    const g = grab.current;
    if (!g || g.id !== e.pointerId) return;
    setLiveBox(dragBox(g, (e.clientX - g.sx) / z, (e.clientY - g.sy) / z));
  };
  const resizeUp = (e: ReactPointerEvent<HTMLSpanElement>) => {
    const g = grab.current;
    if (!g || g.id !== e.pointerId) return;
    grab.current = null;
    if (liveBox) p.onLayout({ size: boxTier(liveBox.w), boxW: liveBox.w, boxH: liveBox.h });
    setLiveBox(null);
  };

  // The ⋯, in groups (a group's first item draws the rule above it): the
  // session · how the line looks · what flies on it · closing it.
  const items: MenuItem[] = [
    p.layout.open
      ? { key: "open", label: "Fold the session back", title: "Back to a box on the line", onSelect: () => p.onLayout({ open: false }) }
      : { key: "open", label: "Open session", title: "Open the session here, in a card on its line", onSelect: () => p.onLayout({ open: true }) },
    {
      key: "grid",
      label: "Open in the grid",
      title: "Switch to the grid with this session selected",
      onSelect: () => {
        selectSession(p.title);
        setView("grid");
      },
    },
    { key: "only", label: "Show only this line", title: "This session: one line, filling the sky", onSelect: () => showInSky(p.title) },
    ...SIZES.map((s, i): MenuItem => ({
      key: "size-" + s,
      label: SIZE_ITEM[s],
      title: "Boxes: " + SIZE_WORD[s],
      className: "skl-mi-check" + (s === size ? " skl-mi-on" : "") + (i === 0 ? " skl-mi-group" : ""),
      onSelect: () => p.onLayout(presetPatch(s)),
    })),
    ...(folds
      ? [{ key: "flown", label: foldFlown ? "Show flown stops" : "Fold flown stops", title: "The stops this flight has already flown: each on its own, or one pill", onSelect: () => setFoldFlown((x) => !x) }]
      : []),
    p.layout.pinned
      ? { key: "tidy", label: "Tidy this line", title: "Forget where you put it: back to its place in the rows", onSelect: () => p.onLayout({ pinned: false }) }
      : { key: "pin", label: "Pin here", title: "Keep this line where it is when the others move", onSelect: () => p.onLayout({ x: p.layout.x, y: p.layout.y, pinned: true }) },
    { key: "flyway", label: "Add a stop…", title: "A saved stop, tacked on here", className: "skl-mi-group", onSelect: () => line.togglePalette(true) },
    { key: "tack", label: "Tack on a stop", title: "One stop, at the end of the line", onSelect: () => line.togglePalette(true) },
    { key: "close", label: "Close session", danger: true, className: "skl-mi-group", title: "Stops the agent and moves it to Recently closed", onSelect: () => void closeSession(p.title) },
  ];
  // Where the flight is, in words (the bar's second voice).
  const where = row ? lineWhere(shown.diagram) : "";
  // Round 15: the time under the line, and how long the row has been quiet.
  const segments = useMemo(() => segmentsFor(line.flight), [line.flight]);
  const age = ageOf(row?.activity_since);
  const still = ageWord(row?.activity_since);

  return (
    <div
      ref={rootRef}
      className={
        "skl skl-size-" + size +
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
      // The sky's zoom, for CSS that counter-scales (a hover toolbar, the
      // level of detail a box shows): `--sky-z` on every line.
      style={{ "--sky-z": p.zoom || 1, "--sky-lines": nearLines(p.zoom || 1) } as React.CSSProperties}
      role="group"
      tabIndex={-1}
      data-sky-box
      aria-label={name + "'s line"}
      onPointerDownCapture={() => {
        if (!p.focused) p.onFocus();
      }}
    >
      <div className="skl-bar" data-tour="sky-line-bar" title={p.draggable === false ? "Click to focus this line (drag mode moves it: V)" : "Drag to move this line"} onPointerDown={onBarDown}>
        <Bird size={16} />
        <span className="skl-name">{name}</span>
        {row && (
          <span className={"fl-box-chip skl-chip tone-" + ss.tone} title={ss.hint}>
            {ss.label}
          </span>
        )}
        {/* Where the flight is ("at Reviews settle · 10 min quiet"): mid
            and near. */}
        {where && (
          <span className="skl-where" title={where}>
            {where}
          </span>
        )}
        {/* Near: what the session is on, under its name (CSS shows it only
            at that level of detail). */}
        {row && (row.task_headline || row.task) && <span className="skl-task">{firstLine(row.task_headline || row.task || "")}</span>}
        {still && (
          <span className="skl-age" title={"Nothing has happened on this session for " + still.replace(/ still$/, "")}>
            {still}
          </span>
        )}
        {p.layout.pinned && (
          <span className="skl-pin" title="Pinned: the sky's layout leaves this line where you put it" aria-label="Pinned">
            📌
          </span>
        )}
        <button
          type="button"
          className="skl-size"
          data-size={size}
          aria-label={"Box size: " + size + " (next: " + nextSize(size) + ")"}
          title={"Boxes: " + SIZE_WORD[size] + " — press for " + nextSize(size)}
          onClick={() => p.onLayout(presetPatch(nextSize(size)))}
        >
          <span className="skl-size-glyph" aria-hidden="true">
            ◧
          </span>
          {/* Its state at a glance: one, two or three bars lit. */}
          <span className="skl-size-meter" aria-hidden="true">
            {SIZES.map((s, i) => (
              <i key={s} className={i <= SIZES.indexOf(size) ? "on" : ""} />
            ))}
          </span>
          <span className="skl-size-word" aria-hidden="true">
            {size}
          </span>
        </button>
        <span className="skl-menu" data-no-drag>
          <OverflowMenu label={"Actions for " + name} items={items} />
        </span>
      </div>
      {line.cancel.node}
      {closing && (
        <div className="skl-close-confirm" data-no-drag>
          <InlineConfirm
            title={"Close “" + windowName(closing) + "”?"}
            body="Its agent stops and it moves to Recently closed (Ctrl+Shift+T brings it back)."
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
        // The sizer holds the scaled canvas at its drawn size; inside, the
        // canvas at its tier's size, scaled, with `--sky-z` times the scale.
        <div ref={sizerRef} className="skl-scale" style={scaled && inner ? { width: inner.w * k, height: inner.h * k } : undefined}>
          <div ref={innerRef} className="skl-scale-in" style={scaled ? ({ transform: `scale(${k})`, "--sky-z": z * k } as React.CSSProperties) : undefined}>
            <FlywayCanvas
              diagram={shown.diagram}
              label={"What " + name + " is doing and will do"}
              vertical={false}
              bird={birdPerch(shown.diagram)}
              className="sky-canvas skl-canvas"
              size={size}
              detailOf={detailOf}
              dropLabel={p.dropFlyway ? "Drop to fly " + p.dropFlyway.name : null}
              openBox={openBox}
              lod={lod}
              {...shown.canvas}
              highlighted={p.highlighted}
              onShiftSelect={p.onShiftSelect}
              onOpen={onOpen}
              roomy
            />
          </div>
          {lod !== "far" && <LineRuler segments={segments} />}
        </div>
      ) : (
        <p className="skl-gone muted">This session isn't running any more.</p>
      )}
      {line.editing && (
        <div className="skl-panels sky-own-scroll" data-no-drag>
          {line.palette}
          {line.inspector}
        </div>
      )}
      {row && (
        <span
          className="skl-box-resize"
          role="separator"
          tabIndex={0}
          data-no-drag
          aria-label={"Resize " + name + "'s boxes"}
          title={"Drag to resize the boxes (" + Math.round(box.w) + " × " + Math.round(box.h) + ")"}
          onPointerDown={resizeDown}
          onPointerMove={resizeMove}
          onPointerUp={resizeUp}
          onPointerCancel={resizeUp}
        />
      )}
    </div>
  );
}
