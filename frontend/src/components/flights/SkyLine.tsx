/** One session's line in the Sky (docs/flights-sky-line.md): its title bar,
 * then its session diagram — the session box, its workers, the fast-track's
 * rungs or the flight's stops, the trailing + — with the bird on the box the
 * flight is at.
 *
 * The line is an isomorphism of the normal flow: press the session box (or a
 * worker's) and it opens in place into a card holding the SAME Pane the grid
 * draws for that title (`embedded`: no grip, no hide −) — Agent · Terminal ·
 * Diff · Queue · Map · Flight, the registry's terminals, the per-title tab —
 * resizable from its corner, ▣ folds it back into a box. ◧ cycles how much
 * each box shows (compact → normal → wide).
 *
 * The host (Sky.tsx) places the line at `layout.x/y`, drags it by the title
 * bar (`onDragStart`), persists `layout` (`onLayout`) and gives it room from
 * `onMeasure` — the line's size in plane px, read by a ResizeObserver. A
 * worker's open card is this line's own state (the host keeps one layout per
 * line); it reaches the host only as the line growing. */

import { useEffect, useMemo, useRef, useState, type JSX, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import type { Instance } from "../../api/types";
import { useFlightKinds, useFlights } from "../../state/queries";
import { useUi } from "../../state/store";
import { closeSession, selectSession } from "../../lib/sessionActions";
import { deriveSessionState } from "../../lib/sessionState";
import { firstLine } from "../../lib/format";
import { birdPerch, type DiagramNode } from "../../lib/flightDiagram";
import type { FlightPlan } from "../../lib/flights";
import { OverflowMenu, type MenuItem } from "../OverflowMenu";
import { Pane } from "../grid/Pane";
import { FlywayCanvas, type BoxSizeName } from "./FlywayCanvas";
import { useSessionLine, type LineSource } from "./useSessionLine";
import { Bird } from "./Bird";
import "./SkyLine.css";

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

/** ◧: compact → normal → wide → compact. */
export const nextSize = (s: BoxSizeName): BoxSizeName => SIZES[(SIZES.indexOf(s) + 1) % SIZES.length];

/** A card's pane size, never under the floor. */
export function clampPane(w: number, h: number): { w: number; h: number } {
  return { w: Math.max(PANE_MIN_W, Math.round(w)), h: Math.max(PANE_MIN_H, Math.round(h)) };
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
  // Workers' open cards, by the worker's title (their pane sizes with them).
  const [kids, setKids] = useState<Record<string, { w: number; h: number }>>({});
  const rootRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef(p.onMeasure);
  measureRef.current = p.onMeasure;
  const size: BoxSizeName = p.layout.size || "normal";

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
    const kid = n.kind === "child" ? rowOf(n.session) : undefined;
    const ks = kid ? kids[kid.title] : undefined;
    if (kid && ks)
      return card(kid, ks.w, ks.h, (w, h) => setKids((k) => ({ ...k, [kid.title]: { w, h } })), () => foldKid(kid.title));
    return null;
  };
  const onOpen = (n: DiagramNode) => {
    if (n.kind === "session") p.onLayout({ open: !p.layout.open });
    else if (n.kind === "child" && n.session) {
      const t = n.session;
      if (kids[t]) foldKid(t);
      else setKids((k) => ({ ...k, [t]: { w: PANE_W, h: PANE_H } }));
    } else line.canvas.onOpen?.(n);
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

  const items: MenuItem[] = [
    { key: "open", label: "Open session", title: "Open this session", onSelect: () => selectSession(p.title) },
    { key: "only", label: "Show only this line", title: "This session: one line, filling the sky", onSelect: () => useUi.getState().openDialogFor("flights", "sky/" + p.title) },
    { key: "flyway", label: "Fly a flyway…", title: "Tack a stop, or a whole flyway, onto this line", onSelect: () => line.togglePalette(true) },
    { key: "close", label: "Close session", danger: true, title: "Stops the agent and moves it to Recently closed", onSelect: () => void closeSession(p.title) },
  ];

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
      role="group"
      aria-label={p.title + "'s line"}
      onPointerDownCapture={() => {
        if (!p.focused) p.onFocus();
      }}
    >
      <div className="skl-bar" title="Drag to move this line" onPointerDown={onBarDown}>
        <Bird size={13} />
        <span className="skl-name">{p.title}</span>
        {row && (
          <span className={"fl-box-chip skl-chip tone-" + ss.tone} title={ss.hint}>
            {ss.label}
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
          aria-label={"Box size: " + size + " (next: " + nextSize(size) + ")"}
          title={"Boxes: " + SIZE_WORD[size] + " — press for " + nextSize(size)}
          onClick={() => p.onLayout({ size: nextSize(size) })}
        >
          ◧
        </button>
        <span className="skl-menu" data-no-drag>
          <OverflowMenu label={"Actions for " + p.title} items={items} />
        </span>
      </div>
      {line.cancel.node}
      {row ? (
        <FlywayCanvas
          diagram={line.diagram}
          label={"What " + p.title + " is doing and will do"}
          vertical={false}
          bird={birdPerch(line.diagram)}
          className="sky-canvas skl-canvas"
          size={size}
          detailOf={detailOf}
          dropLabel={p.dropFlyway ? "Drop to fly " + p.dropFlyway.name : null}
          openBox={openBox}
          {...line.canvas}
          onOpen={onOpen}
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
