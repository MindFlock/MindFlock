/** Drag handle on the sidebar's right edge.
 *
 * The sidebar was a fixed 260px, which made long session labels ellipsize while
 * the agent panes had width to spare (or the other way round on a big screen).
 * The width lives in the UI store, so it persists across reloads; the panes
 * refit their terminals on their own (Pane's ResizeObserver), so nothing here
 * has to know about xterm.
 *
 * During the drag the CSS var is written DIRECTLY and the store is left alone —
 * committing on every pointermove would mean a synchronous localStorage write
 * per frame. The store is updated once on release, which re-asserts the var
 * through App's effect. */

import { useCallback, useEffect, useRef } from "react";
import {
  SIDEBAR_DEFAULT_W,
  SIDEBAR_MAX_W,
  SIDEBAR_MIN_W,
  clampSidebarWidth,
  useUi,
} from "../../state/store";

/** How far one arrow-key press moves the edge. */
const STEP = 16;

export function SidebarResizer() {
  const width = useUi((s) => s.sidebarWidth);
  const setSidebarWidth = useUi((s) => s.setSidebarWidth);
  // Drag origin (pointer x + width at pointerdown) and the width the pointer is
  // currently over — read on release, so a drag that ends outside the window
  // still commits the last width the user actually saw.
  const startX = useRef(0);
  const startW = useRef(width);
  const liveW = useRef(width);

  const paint = useCallback((px: number) => {
    liveW.current = px;
    document.body.style.setProperty("--sidebar-w", px + "px");
  }, []);

  // Body class drives the global col-resize cursor + selection lock; make sure
  // an unmount mid-drag can't leave the page stuck in it.
  useEffect(() => () => document.body.classList.remove("sidebar-resizing"), []);

  const onPointerDown = (ev: React.PointerEvent<HTMLDivElement>) => {
    if (ev.button !== 0) return;
    ev.preventDefault();
    startX.current = ev.clientX;
    startW.current = width;
    liveW.current = width;
    ev.currentTarget.setPointerCapture(ev.pointerId);
    document.body.classList.add("sidebar-resizing");
  };

  const onPointerMove = (ev: React.PointerEvent<HTMLDivElement>) => {
    if (!ev.currentTarget.hasPointerCapture(ev.pointerId)) return;
    paint(clampSidebarWidth(startW.current + (ev.clientX - startX.current)));
  };

  const endDrag = (ev: React.PointerEvent<HTMLDivElement>) => {
    if (!ev.currentTarget.hasPointerCapture(ev.pointerId)) return;
    ev.currentTarget.releasePointerCapture(ev.pointerId);
    document.body.classList.remove("sidebar-resizing");
    setSidebarWidth(liveW.current);
  };

  return (
    <div
      className="sidebar-resizer"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize sidebar"
      aria-valuenow={width}
      aria-valuemin={SIDEBAR_MIN_W}
      aria-valuemax={SIDEBAR_MAX_W}
      tabIndex={0}
      title="Drag to resize · double-click to reset"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onDoubleClick={() => setSidebarWidth(SIDEBAR_DEFAULT_W)}
      onKeyDown={(ev) => {
        if (ev.key === "ArrowLeft") setSidebarWidth(width - STEP);
        else if (ev.key === "ArrowRight") setSidebarWidth(width + STEP);
        else if (ev.key === "Home") setSidebarWidth(SIDEBAR_DEFAULT_W);
        else return;
        ev.preventDefault();
      }}
    />
  );
}
