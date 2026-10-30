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
 * there, never also below. ◧ cycles how much each box shows (compact →
 * normal → wide).
 *
 * The sky draws the roomy tier of boxes (`roomy`: bigger boxes, bigger type,
 * docs/flights-visibility.md), and folds the stops the flight has already
 * flown into one `flown` pill; pressing it (or the ⋯'s "Show every flown
 * stop") unfolds them on this line.
 *
 * The host (Sky.tsx) places the line at `layout.x/y`, drags it by the title
 * bar (`onDragStart`), persists `layout` (`onLayout`) and gives it room from
 * `onMeasure` — the line's size in plane px, read by a ResizeObserver. A
 * worker's open card is this line's own state (the host keeps one layout per
 * line); it reaches the host only as the line growing. */

import { useEffect, useMemo, useRef, useState, type JSX, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { setView, showInSky } from "../../lib/viewMode";
import { lodFor, nearLines } from "../../lib/skyLayout";
import type { Instance } from "../../api/types";
import { useFlightKinds, useFlights } from "../../state/queries";
import { closeSession, selectSession } from "../../lib/sessionActions";
import { deriveSessionState } from "../../lib/sessionState";
import { firstLine } from "../../lib/format";
import { birdPerch, drawnIndexes, flownCount, lineWhere, sessionDiagram, type Diagram, type DiagramNode } from "../../lib/flightDiagram";
import type { FlightPlan } from "../../lib/flights";
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
}

/** An open card's pane, in plane px: its default and its floor. */
export const PANE_W = 720;
export const PANE_H = 460;
export const PANE_MIN_W = 360;
export const PANE_MIN_H = 240;
/** The card's own head (the name and ▣) above the pane. */
export const CARD_HEAD_H = 30;

const SIZES: readonly BoxSizeName[] = ["compact", "normal", "wide"];
const SIZE_WORD: Record<BoxSizeName, string> = { compact: "compact — titles only", normal: "normal", wide: "wide — the full text" };

/** The ⋯ menu's three box sizes, in words. */
const SIZE_ITEM: Record<BoxSizeName, string> = { compact: "Compact boxes", normal: "Normal boxes", wide: "Wide boxes" };

/** ◧: compact → normal → wide → compact. */
export const nextSize = (s: BoxSizeName): BoxSizeName => SIZES[(SIZES.indexOf(s) + 1) % SIZES.length];

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
        <span className="skl-card-name">{inst.title}</span>
        <button type="button" className="skl-card-fold" aria-label={"Fold " + inst.title + " back into a box"} title="Back to a box" onClick={onClose}>
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
        aria-label={"Resize " + inst.title + "'s card"}
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
  // What has flown: folded into one pill until this line unfolds it.
  const [expandFlown, setExpandFlown] = useState(false);
  const folds = flownCount(line.diagram) > 0;
  const full = useMemo(
    () => (expandFlown ? sessionDiagram(p.rows, p.title, { flights, kinds, family: true, expandFlown: true }) : line.diagram),
    [expandFlown, line.diagram, p.rows, p.title, flights, kinds]
  );
  const shown = flownCanvas(full, line.canvas);
  // Workers' open cards, by the worker's title (their pane sizes with them).
  const [kids, setKids] = useState<Record<string, { w: number; h: number }>>({});
  const rootRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef(p.onMeasure);
  measureRef.current = p.onMeasure;
  const size: BoxSizeName = p.layout.size || "normal";
  // What the boxes show at this zoom (far / mid / near); their size never
  // changes with it — only what is drawn inside and the type on screen.
  const lod = lodFor(p.zoom || 1);

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
    return {
      body: <PaneCard inst={inst} w={s.w} h={s.h} zoom={p.zoom} onSize={onSize} onClose={onClose} />,
      size: { w: s.w, h: s.h + CARD_HEAD_H },
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
    if (n.kind === "flown") setExpandFlown((x) => !x);
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
    p.onDragStart?.(e);
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
      onSelect: () => p.onLayout({ size: s }),
    })),
    ...(folds
      ? [{ key: "flown", label: expandFlown ? "Fold the flown stops" : "Show every flown stop", title: "The stops this flight has already flown: one pill, or each on its own", onSelect: () => setExpandFlown((x) => !x) }]
      : []),
    p.layout.pinned
      ? { key: "tidy", label: "Tidy this line", title: "Forget where you put it: back to its place in the rows", onSelect: () => p.onLayout({ pinned: false }) }
      : { key: "pin", label: "Pin here", title: "Keep this line where it is when the others move", onSelect: () => p.onLayout({ x: p.layout.x, y: p.layout.y, pinned: true }) },
    { key: "flyway", label: "Fly a flyway…", title: "A whole flyway, flown on this line", className: "skl-mi-group", onSelect: () => line.togglePalette(true) },
    { key: "tack", label: "Tack on a stop", title: "One stop, at the end of the line", onSelect: () => line.togglePalette(true) },
    { key: "close", label: "Close session", danger: true, className: "skl-mi-group", title: "Stops the agent and moves it to Recently closed", onSelect: () => void closeSession(p.title) },
  ];
  // Where the flight is, in words (the bar's second voice).
  const where = row ? lineWhere(full) : "";

  return (
    <div
      ref={rootRef}
      className={
        "skl skl-size-" + size +
        (p.focused ? " is-focused" : "") +
        (p.layout.open ? " is-open" : "") +
        (p.dropFlyway ? " is-drop" : "") +
        (line.editing ? " is-editing" : "")
      }
      data-line={p.title}
      data-lod={lod}
      // The sky's zoom, for CSS that counter-scales (a hover toolbar, the
      // level of detail a box shows): `--sky-z` on every line.
      style={{ "--sky-z": p.zoom || 1, "--sky-lines": nearLines(p.zoom || 1) } as React.CSSProperties}
      role="group"
      aria-label={p.title + "'s line"}
      onPointerDownCapture={() => {
        if (!p.focused) p.onFocus();
      }}
    >
      <div className="skl-bar" title="Drag to move this line" onPointerDown={onBarDown}>
        <Bird size={16} />
        <span className="skl-name">{p.title}</span>
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
          onClick={() => p.onLayout({ size: nextSize(size) })}
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
          <OverflowMenu label={"Actions for " + p.title} items={items} />
        </span>
      </div>
      {line.cancel.node}
      {row ? (
        <FlywayCanvas
          diagram={shown.diagram}
          label={"What " + p.title + " is doing and will do"}
          vertical={false}
          bird={birdPerch(full)}
          className="sky-canvas skl-canvas"
          size={size}
          detailOf={detailOf}
          dropLabel={p.dropFlyway ? "Drop to fly " + p.dropFlyway.name : null}
          openBox={openBox}
          lod={lod}
          {...shown.canvas}
          onOpen={onOpen}
          // The sky's bigger boxes (lib/flightDiagram boxMetrics' roomy
          // tier); the canvas passes it to layoutDiagram and sizedBoxFor.
          {...({ roomy: true } as object)}
        />
      ) : (
        <p className="skl-gone muted">This session isn't running any more.</p>
      )}
      {line.editing && (
        <div className="skl-panels sky-own-scroll" data-no-drag>
          {line.palette}
          {line.inspector}
        </div>
      )}
    </div>
  );
}
