/** The floating blocks palette (docs/flights.md "Round 14" → "The palette
 * fills itself", "As built: round 14 palette"): EVERY block as a sky object
 * the owner drags by its head, sizes by its corner and drags blocks out of.
 * The host (SkyView) mounts it when `mf_sky_palette` is "float", under the
 * layout key `"palette"` (x / y / pinned / w / h), as it mounts the intake
 * lane; the shelf's "All blocks…" floats it large.
 *
 * Its body is the catalog's (`StopCatalogBody`, compact): the Session card,
 * every kind, every saved stop, as a wrapped grid of cards that fills the
 * frame and scrolls past it — the frame decides how many show — with the
 * filter in the head. The kinds group by family (round 15: Work · Ship ·
 * Outside · People, the Session card first in Work), the saved stops after. Each card drags with its chip's MIME; a click or a
 * double-click (once) → `onPlace(drop)`: the host puts it on the board to the
 * palette's right. A card's ★ / ☆ sets the bottom shelf's pick
 * (`useShelfPick`) — the palette itself always shows everything. The head's
 * ⋯ → **Dock at the bottom** (`onDock`), its × (`onClose`) → back to the
 * shelf. */

import { useEffect, useRef, useState, type DragEvent, type JSX, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import { useClickOrDouble } from "../../lib/clickOrDouble";
import { savedStopOrder } from "../../lib/groundedLines";
import { catalogMatch, onShelf, toggleShelf, type CatalogDrop } from "../../lib/stopCatalog";
import { useFlightKinds, useFlightPlans } from "../../state/queries";
import { OverflowMenu } from "../OverflowMenu";
import { StopIcon } from "./Bird";
import { StopCatalogBody } from "./StopCatalog";
import { sessionDragData, useShelfPick } from "./StopShelf";
import "./SkyPalette.css";

export const PALETTE_KEY = "palette";
export const PALETTE_MODE_KEY = "mf_sky_palette";
export type PaletteMode = "shelf" | "float";

/** The palette's frame when the host keeps none, and the least it shrinks to. */
export const PALETTE_W = 360;
export const PALETTE_H = 420;
export const PALETTE_MIN_W = 240;
export const PALETTE_MIN_H = 200;

/** A frame held to the palette's least size (whole px); no number (or not a
 * finite one) is the default. */
export function paletteFrame(w?: number, h?: number): { w: number; h: number } {
  const ok = (v: number | undefined, d: number) => (typeof v === "number" && Number.isFinite(v) ? v : d);
  return { w: Math.max(PALETTE_MIN_W, Math.round(ok(w, PALETTE_W))), h: Math.max(PALETTE_MIN_H, Math.round(ok(h, PALETTE_H))) };
}

/** The corner drag: the frame it started from plus the pointer's screen
 * delta, divided by the sky's zoom (plane px), held to the least size. */
export function dragFrame(start: { w: number; h: number }, dx: number, dy: number, zoom: number): { w: number; h: number } {
  const z = zoom > 0 ? zoom : 1;
  return paletteFrame(start.w + dx / z, start.h + dy / z);
}

export interface SkyPaletteProps {
  /** The frame the host keeps for it (its layout entry's w / h), else its own default. */
  width?: number;
  height?: number;
  /** The sky's zoom: a corner drag's screen delta becomes plane px by it. */
  zoom?: number;
  /** The head's press, when the host lets it move (drag mode). */
  onHeadDown?(e: ReactPointerEvent): void;
  draggable?: boolean;
  /** The corner handle's release: the frame the owner dragged to. */
  onResize?(w: number, h: number): void;
  /** Its measured size (a ResizeObserver), for the host's spots. */
  onMeasure?(w: number, h: number): void;
  /** A card's click or double-click: the host places a grounded line next to the palette. */
  onPlace?(drop: CatalogDrop): void;
  /** The ⋯'s "Dock at the bottom". */
  onDock?(): void;
  /** A Session chip pressed. */
  onSession?(): void;
  /** The head's ×: back to the shelf (the host sets shelf mode). */
  onClose?(): void;
  /** Opened by "All blocks…": the filter takes the focus on mount. */
  focusFilter?: boolean;
}

/** A press on the head that is the head's own (not one of its buttons). */
const headPress = (e: ReactPointerEvent) => e.button === 0 && !(e.target as Element).closest("button, a, input, summary, [data-no-drag]");

/** The Session block as a card, first in the palette: it drags the
 * new-session type; a click (or a double-click, once) opens the block. */
function SessionCard({ onSession }: { onSession?(): void }) {
  const cd = useClickOrDouble(onSession);
  return (
    <div
      role="option"
      aria-selected={false}
      className={"sky-catalog-card sky-catalog-session" + (onSession ? "" : " drag-only")}
      data-tour="flights-newblock"
      draggable
      title={"A new session: drag it onto the sky (or onto stops with no session yet)" + (onSession ? ", or click" : "")}
      onDragStart={(e: DragEvent) => {
        cd.cancel();
        for (const [type, value] of sessionDragData()) e.dataTransfer.setData(type, value);
        e.dataTransfer.effectAllowed = "copy";
      }}
      onClick={(e) => cd.onClick(e)}
      onDoubleClick={() => cd.onDoubleClick()}
    >
      <div className="sky-catalog-card-head">
        <span className="sky-catalog-icon">
          <StopIcon kind="session" size={14} />
        </span>
        <span className="sky-catalog-name">
          <span className="sky-catalog-title">Session</span>
        </span>
      </div>
      <span className="sky-catalog-summary">A new agent session, its own branch</span>
    </div>
  );
}

export function SkyPalette({ width, height, zoom = 1, onHeadDown, draggable = true, onResize, onMeasure, onPlace, onDock, onSession, onClose, focusFilter = false }: SkyPaletteProps): JSX.Element {
  const kinds = useFlightKinds().data;
  const plans = useFlightPlans().data;
  const [shelf, changeShelf] = useShelfPick();
  const allSaved = savedStopOrder(plans || []);
  // The corner handle: the intake lane's (pointer capture, the delta over the
  // zoom), the frame drawn live and handed to the host on release.
  const [live, setLive] = useState<{ w: number; h: number } | null>(null);
  const grab = useRef<{ id: number; sx: number; sy: number; w: number; h: number } | null>(null);
  const kept = paletteFrame(width, height);
  const frame = live || kept;
  const resizeDown = (e: ReactPointerEvent<HTMLSpanElement>) => {
    if (e.button !== 0 || !onResize) return;
    e.preventDefault();
    e.stopPropagation();
    grab.current = { id: e.pointerId, sx: e.clientX, sy: e.clientY, w: kept.w, h: kept.h };
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* a synthetic event has no capture */
    }
  };
  const resizeMove = (e: ReactPointerEvent<HTMLSpanElement>) => {
    const g = grab.current;
    if (!g || g.id !== e.pointerId) return;
    setLive(dragFrame(g, e.clientX - g.sx, e.clientY - g.sy, zoom));
  };
  const resizeUp = (e: ReactPointerEvent<HTMLSpanElement>) => {
    const g = grab.current;
    if (!g || g.id !== e.pointerId) return;
    grab.current = null;
    if (live && onResize) onResize(live.w, live.h);
    setLive(null);
  };
  // The arrows on a focused handle size it too, 20px a press.
  const resizeKey = (e: ReactKeyboardEvent<HTMLSpanElement>) => {
    const step: Record<string, [number, number]> = { ArrowRight: [20, 0], ArrowLeft: [-20, 0], ArrowDown: [0, 20], ArrowUp: [0, -20] };
    const d = step[e.key];
    if (!d || !onResize) return;
    e.preventDefault();
    e.stopPropagation();
    const f = paletteFrame(kept.w + d[0], kept.h + d[1]);
    onResize(f.w, f.h);
  };

  // The host lays the palette out from what it measures (plane px), as a line's.
  const rootRef = useRef<HTMLElement>(null);
  const measureRef = useRef(onMeasure);
  measureRef.current = onMeasure;
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

  const headDown = (e: ReactPointerEvent) => {
    if (draggable && onHeadDown && headPress(e)) onHeadDown(e);
  };
  const moveHint = draggable && onHeadDown ? "Drag to move the stops palette" : undefined;
  const count = (kinds?.length || 0) + allSaved.length;

  return (
    <section ref={rootRef} className="sky-palette" data-tour="sky-palette" aria-label="Stops palette" style={{ width: frame.w, height: frame.h }}>
      <StopCatalogBody
        kinds={kinds || []}
        plans={allSaved}
        onAdd={onPlace}
        place
        compact
        idPrefix="sky-palette"
        listClass="sky-palette-body"
        autoFocus={focusFilter}
        shelf={(d: CatalogDrop) => onShelf(shelf, d)}
        onShelfToggle={(d: CatalogDrop) => changeShelf((p) => toggleShelf(p, d))}
        lead={(q) => (catalogMatch(q, "Session", "A new agent session") ? <SessionCard key="session" onSession={onSession} /> : null)}
        head={(filter) => (
          <div className={"sky-palette-head" + (moveHint ? " movable" : "")} data-sky-head title={moveHint} onPointerDown={headDown}>
            <strong className="sky-palette-title">Stops</strong>
            <span className="sky-palette-count" title={count + " stops and saved chains — ★ puts one on the bottom shelf"}>
              {count}
            </span>
            {filter}
            {onDock && <OverflowMenu label="The stops palette — more" items={[{ key: "dock", label: "Dock at the bottom", title: "Put the stops back in the shelf at the sky's foot", onSelect: onDock }]} />}
            {onClose && (
              <button type="button" className="fl-icon-btn sky-palette-close" aria-label="Close the stops palette" title="Close — back to the shelf" onClick={onClose}>
                ×
              </button>
            )}
          </div>
        )}
      />
      {onResize && (
        <span
          className="sky-palette-resize"
          role="separator"
          tabIndex={0}
          data-no-drag
          aria-label="Resize the stops palette"
          title="Drag to resize the palette (or the arrow keys)"
          onPointerDown={resizeDown}
          onPointerMove={resizeMove}
          onPointerUp={resizeUp}
          onPointerCancel={resizeUp}
          onKeyDown={resizeKey}
        />
      )}
    </section>
  );
}
