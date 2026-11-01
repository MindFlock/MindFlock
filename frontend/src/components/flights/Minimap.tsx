/** The minimap and the off-screen hints (docs/flights.md "Round 17": task
 * `minimap`, "As built: round 17 minimap"). The props below are the contract
 * the sky (SkyView) feeds: `rects` in plane px, `cam` (screen = plane × z +
 * (x, y)), `viewport` in screen px. The maths is lib/minimap; both components
 * are hookless (a drag's start lives in a WeakMap keyed by the map element),
 * so they render the same on the server and their handlers can be driven
 * without a DOM. */

import type { CSSProperties, PointerEvent as ReactPointerEvent, ReactElement } from "react";

import "./Minimap.css";
import { lodFor, type SkyCamera } from "../../lib/skyLayout";
import {
  HINT_MAX,
  LINE_KINDS,
  MINIMAP_H,
  MINIMAP_PAD,
  MINIMAP_W,
  centreAt,
  dragCamera,
  edgeHints,
  fromMap,
  inside,
  mapBlocks,
  mapScale,
  pickBlock,
  rectToMap,
  shortLabel,
  toneVar,
  viewportRect,
  type MapBlock,
  type MapFit,
} from "../../lib/minimap";

/** One thing on the plane, in plane px, with what it is and how it reads. */
export interface MinimapRect {
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
  kind: "line" | "grounded" | "intake" | "palette" | "landing" | "person" | "device" | "note";
  /** The line's state tone, e.g. "working" | "idle" | "asking" | "waiting_you" | "done"; "" when none. */
  tone?: string;
  /** What the line is called (for a hint's label and the minimap's tooltip). */
  label?: string;
  selected?: boolean;
  focused?: boolean;
}

export interface MinimapProps {
  rects: readonly MinimapRect[];
  cam: SkyCamera;
  /** The sky's viewport, screen px. */
  viewport: { w: number; h: number };
  /** The owner clicked or dragged the map: move the camera there (same zoom). */
  onCamera(cam: SkyCamera): void;
  /** The owner clicked a rect on the map: focus that thing (fit it). */
  onJump(id: string): void;
  /** Folded to a corner glyph, or open. */
  open: boolean;
  onToggle(): void;
}

/** A press on the map: where it began (map px), the camera then, and whether
 * it is dragging the viewport rectangle or is (so far) a click. */
interface Press {
  pointer: number;
  x: number;
  y: number;
  cam0: SkyCamera;
  fit: MapFit;
  drag: boolean;
  moved: boolean;
}

/** A press travels this far (map px) before it is a drag, not a click. */
const CLICK_SLOP = 3;
const presses = new WeakMap<object, Press>();

const GLYPH = "◰";

/** Where an event sits on the map element, map px. */
function mapPoint(e: { clientX: number; clientY: number; currentTarget: unknown }): { x: number; y: number } {
  const r = (e.currentTarget as Element).getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}

const blockTitle = (r: MinimapRect | undefined) => (r ? r.label || r.kind : "");

export function Minimap({ rects, cam, viewport, onCamera, onJump, open, onToggle }: MinimapProps): ReactElement | null {
  if (!open) {
    return (
      <div className="sky-minimap is-folded" data-no-drag="">
        <button type="button" className="sky-minimap-glyph fl-icon-btn" aria-label="Show the minimap" aria-expanded={false} title="Show the minimap" onClick={() => onToggle()}>
          {GLYPH}
        </button>
      </div>
    );
  }
  const fit = mapScale(rects, { w: MINIMAP_W, h: MINIMAP_H }, MINIMAP_PAD);
  const blocks: MapBlock[] = mapBlocks(rects, fit);
  const byId = new Map(rects.map((r) => [r.id, r]));
  const view = rectToMap(fit, viewportRect(cam, viewport));
  const empty = rects.length === 0;

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || empty) return;
    e.stopPropagation();
    e.preventDefault();
    const p = mapPoint(e);
    presses.set(e.currentTarget, { pointer: e.pointerId, x: p.x, y: p.y, cam0: cam, fit, drag: inside(view, p.x, p.y), moved: false });
    try {
      e.currentTarget.setPointerCapture?.(e.pointerId);
    } catch {
      /* a synthetic pointer: no capture, the drag still follows */
    }
  };
  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const s = presses.get(e.currentTarget);
    if (!s || s.pointer !== e.pointerId) return;
    const p = mapPoint(e);
    const dx = p.x - s.x;
    const dy = p.y - s.y;
    if (!s.moved && Math.hypot(dx, dy) < CLICK_SLOP) return;
    s.moved = true;
    // Outside the rectangle a drag pans too: grab where you pressed.
    s.drag = true;
    onCamera(dragCamera(s.cam0, s.fit, dx, dy));
  };
  const onPointerUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const s = presses.get(e.currentTarget);
    if (!s || s.pointer !== e.pointerId) return;
    presses.delete(e.currentTarget);
    e.stopPropagation();
    if (s.moved) return;
    // A click: a block jumps to its thing; anywhere else centres the camera.
    const id = pickBlock(blocks, s.x, s.y);
    if (id) onJump(id);
    else onCamera(centreAt(fromMap(s.fit, s), viewport, s.cam0.z));
  };
  const onPointerCancel = (e: ReactPointerEvent<HTMLDivElement>) => {
    presses.delete(e.currentTarget);
  };

  return (
    <div className="sky-minimap" data-no-drag="">
      <div className="sky-minimap-head">
        <span className="sky-minimap-title">Map</span>
        <button type="button" className="sky-minimap-glyph fl-icon-btn" aria-label="Fold the minimap" aria-expanded={true} title="Fold the minimap" onClick={() => onToggle()}>
          {GLYPH}
        </button>
      </div>
      <div
        className="sky-minimap-map"
        style={{ width: MINIMAP_W, height: MINIMAP_H }}
        role="img"
        aria-label="Minimap: click a line to go to it, click or drag to move the view"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerCancel}
        onDoubleClick={(e) => e.stopPropagation()}
      >
        {empty ? <span className="sky-minimap-empty">Nothing on the sky yet</span> : null}
        {blocks.map((b) => {
          const r = byId.get(b.id);
          const line = LINE_KINDS.has(b.kind);
          const cls =
            "sky-minimap-block" +
            (b.bar ? " is-bar" : line ? " is-line" : " is-object") +
            (r?.selected ? " is-selected" : "") +
            (r?.focused ? " is-focused" : "");
          const style = { left: b.x, top: b.y, width: b.w, height: b.h, ...(line ? { "--mm-tone": toneVar(r?.tone) } : {}) } as CSSProperties;
          return <div key={b.id} className={cls} style={style} title={blockTitle(r)} data-id={b.id} data-kind={b.kind} />;
        })}
        {empty ? null : <div className="sky-minimap-view" style={{ left: view.x, top: view.y, width: view.w, height: view.h }} />}
      </div>
    </div>
  );
}

export interface OffscreenHintsProps {
  rects: readonly MinimapRect[];
  cam: SkyCamera;
  viewport: { w: number; h: number };
  onJump(id: string): void;
}

/** Edge markers for lines outside the view (a bird-coloured chip with the
 * name, pinned to the edge nearest the line, like a game's off-screen HUD).
 * None at lod far: everything is on screen then anyway. */
export function OffscreenHints({ rects, cam, viewport, onJump }: OffscreenHintsProps): ReactElement | null {
  if (lodFor(cam.z) === "far") return null;
  const lines = rects.filter((r) => LINE_KINDS.has(r.kind));
  const hints = edgeHints(lines, cam, viewport, HINT_MAX);
  if (!hints.length) return null;
  const byId = new Map(lines.map((r) => [r.id, r]));
  return (
    <div className="sky-offscreen" data-no-drag="">
      {hints.map((h) => {
        const r = byId.get(h.id);
        const name = r?.label || h.id;
        const style: CSSProperties =
          h.edge === "left"
            ? { left: h.x, top: h.y }
            : h.edge === "right"
              ? { right: viewport.w - h.x, top: h.y }
              : h.edge === "top"
                ? { left: h.x, top: h.y }
                : { left: h.x, bottom: viewport.h - h.y };
        return (
          <button
            key={h.id}
            type="button"
            className={"sky-offscreen-chip fl-pick edge-" + h.edge}
            style={{ ...style, "--mm-tone": toneVar(r?.tone) } as CSSProperties}
            title={"Go to " + name}
            aria-label={"Go to " + name}
            data-id={h.id}
            onClick={(e) => {
              e.stopPropagation();
              onJump(h.id);
            }}
          >
            <span className="sky-offscreen-dot" aria-hidden="true" />
            <span className="sky-offscreen-name">{shortLabel(name, 14)}</span>
          </button>
        );
      })}
    </div>
  );
}
