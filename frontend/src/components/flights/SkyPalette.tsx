/** The floating blocks palette (docs/flights.md "Round 13" → "The palette",
 * "As built: round 13 palette"): the shelf's content as a sky object the
 * owner drags by its head, sizes by its corner and drags blocks out of. The
 * host (SkyView) mounts it when `mf_sky_palette` is "float", under the layout
 * key `"palette"` (x / y / pinned / w / h), as it mounts the intake lane.
 *
 * Inside: the shelf's three regions — Blocks (the Session chip, the picked
 * kinds), Multi-step (the picked saved stops) — as a wrapped grid of the
 * shelf's own chips (StopChip / SessionChip / SavedChip: the same drag MIMEs,
 * the same right-click menus), and the head's **All blocks…** opens the same
 * catalog. It reads the same pick (`mf_sky_shelf`, `useShelfPick`), so the
 * shelf, the palette and the catalog always agree. A click on a block →
 * `onPlace(drop)`: the host puts it on the board to the palette's right. The
 * head's ⋯ → **Dock at the bottom** (`onDock`). */

import { useEffect, useRef, useState, type JSX, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import type { FlightPlan } from "../../lib/flights";
import { savedStopOrder } from "../../lib/groundedLines";
import { onShelf, shelfKinds, shelfPlans, toggleShelf, type CatalogDrop } from "../../lib/stopCatalog";
import { useFlightKinds, useFlightPlans } from "../../state/queries";
import { OverflowMenu } from "../OverflowMenu";
import { StopCatalog } from "./StopCatalog";
import { SavedChip, SessionChip, StopChip, useSavedActions, useShelfPick } from "./StopShelf";
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
  /** A click / Add on an item: the host places a grounded line next to the palette. */
  onPlace?(drop: CatalogDrop): void;
  /** The ⋯'s "Dock at the bottom". */
  onDock?(): void;
  /** A Session chip pressed. */
  onSession?(): void;
}

/** A press on the head that is the head's own (not one of its buttons). */
const headPress = (e: ReactPointerEvent) => e.button === 0 && !(e.target as Element).closest("button, a, input, summary, [data-no-drag]");

export function SkyPalette({ width, height, zoom = 1, onHeadDown, draggable = true, onResize, onMeasure, onPlace, onDock, onSession }: SkyPaletteProps): JSX.Element {
  const kinds = useFlightKinds().data;
  const plans = useFlightPlans().data;
  const [shelf, changeShelf] = useShelfPick();
  const blocks = shelfKinds(shelf, kinds || []);
  const allSaved = savedStopOrder(plans || []);
  const saved = shelfPlans(shelf, allSaved);
  const acts = useSavedActions(changeShelf);
  const [catalog, setCatalog] = useState(false);

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
  const moveHint = draggable && onHeadDown ? "Drag to move the blocks" : undefined;
  const clickKind = onPlace ? (kind: string) => onPlace({ kind }) : undefined;
  const clickPlan = onPlace ? (plan: FlightPlan) => onPlace({ planId: plan.id }) : undefined;
  const count = blocks.length + saved.length;
  const add = (d: CatalogDrop) => {
    setCatalog(false);
    onPlace?.(d);
  };

  return (
    <section ref={rootRef} className="sky-palette" data-tour="sky-palette" aria-label="Blocks palette" style={{ width: frame.w, height: frame.h }}>
      <div className={"sky-palette-head" + (moveHint ? " movable" : "")} data-sky-head title={moveHint} onPointerDown={headDown}>
        <strong className="sky-palette-title">Blocks</strong>
        <span className="sky-palette-count" title={count + " on the palette — ☆ more in All blocks…"}>
          {count}
        </span>
        <button
          type="button"
          className="test-btn sky-palette-all"
          aria-haspopup="dialog"
          aria-expanded={catalog}
          title="Every block and every multi-step block, in a list you can filter"
          onClick={() => setCatalog(true)}
        >
          All blocks…
        </button>
        {onDock && <OverflowMenu label="The blocks palette — more" items={[{ key: "dock", label: "Dock at the bottom", title: "Put the blocks back in the shelf at the sky's foot", onSelect: onDock }]} />}
      </div>
      <div className="sky-palette-body sky-own-scroll">
        {kinds !== undefined && !kinds?.length ? (
          <p className="sky-stops-empty muted">The stop list didn't load — this server may not know Flights yet.</p>
        ) : (
          <>
            <div className="sky-palette-region" data-region="blocks" role="list" aria-label="Blocks">
              <span className="sky-stops-label" aria-hidden="true">
                Blocks
              </span>
              <SessionChip onSession={onSession} />
              {blocks.map((k) => (
                <StopChip key={k.kind} def={k} onClick={clickKind} place onMenu={acts.openKindMenu} />
              ))}
            </div>
            {saved.length > 0 && (
              <div className="sky-palette-region" data-region="multi" role="list" aria-label="Multi-step">
                <span className="sky-stops-label" aria-hidden="true">
                  Multi-step
                </span>
                {saved.map((p) => (
                  <SavedChip
                    key={p.id}
                    plan={p}
                    plans={allSaved}
                    kinds={kinds || []}
                    renaming={acts.renaming === p.id}
                    onClick={clickPlan}
                    place
                    onMenu={acts.openMenu}
                    onRenamed={acts.doneRenaming}
                  />
                ))}
              </div>
            )}
            <p className="sky-palette-hint muted">Click to put a block on the board, or drag it onto a line or the sky.</p>
          </>
        )}
        {acts.confirm && <div className="sky-palette-confirm">{acts.confirm}</div>}
      </div>
      {onResize && (
        <span
          className="sky-palette-resize"
          role="separator"
          tabIndex={0}
          data-no-drag
          aria-label="Resize the blocks palette"
          title="Drag to resize the blocks (or the arrow keys)"
          onPointerDown={resizeDown}
          onPointerMove={resizeMove}
          onPointerUp={resizeUp}
          onPointerCancel={resizeUp}
          onKeyDown={resizeKey}
        />
      )}
      {acts.overlays}
      {catalog && (
        <StopCatalog
          kinds={kinds || []}
          plans={allSaved}
          onAdd={onPlace ? add : undefined}
          place
          onClose={() => setCatalog(false)}
          shelf={(d) => onShelf(shelf, d)}
          onShelfToggle={(d) => changeShelf((p) => toggleShelf(p, d))}
        />
      )}
    </section>
  );
}
