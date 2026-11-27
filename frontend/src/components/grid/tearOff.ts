/** Tearing a session off the main window by dragging it out, like a browser
 * tab: a pane header (Pane) or a rail row (sidebar/rowDnd) whose drag ENDS
 * outside the window opens the session in a window of its own there
 * (lib/popout). A drop inside the window is the ordinary reorder, untouched.
 *
 * "Outside" is decided by {@link dragEndedOutside}, pure so it is tested
 * without a DOM: nothing took the drop (`dropEffect` "none" — a cancelled
 * drag says the same, which is why the pointer must also be outside) AND the
 * pointer is past the window's edges. Some Linux / WSLg builds report the
 * dragend at 0,0; then the last position a `drag` / `dragover` event gave, and
 * whether the drag left the document (a `dragleave` with no `relatedTarget`,
 * not followed by a `dragover` back inside), stand in — {@link trackDrag}
 * collects those while the drag lasts. */

import type { PopoutPlacement } from "../../lib/popout";

/** What a dragend says. */
export interface DragEndInfo {
  dropEffect: string;
  screenX: number;
  screenY: number;
}

/** The window's place on the screen (`window.screenX` …). */
export interface WindowBox {
  screenX: number;
  screenY: number;
  outerWidth: number;
  outerHeight: number;
}

/** What the drag's own events said while it lasted. */
export interface DragTrail {
  /** The pointer left the document and has not come back. */
  left: boolean;
  /** The last non-zero screen position a drag event reported. */
  lastX: number | null;
  lastY: number | null;
}

export const NO_TRAIL: DragTrail = { left: false, lastX: null, lastY: null };

/** Where the drag ended, when it ended OUTSIDE the window with nothing taking
 * the drop: the screen point (when anything reported one), or `{}` when only
 * the drag's leaving the document says so. Null for every other end — a drop
 * on a pane, a drop or a cancel inside the window. */
export function dragEndedOutside(
  end: DragEndInfo,
  win: WindowBox,
  trail: DragTrail = NO_TRAIL
): { x?: number; y?: number } | null {
  if (end.dropEffect !== "none") return null;
  const zero = !end.screenX && !end.screenY;
  const x = zero ? trail.lastX : end.screenX;
  const y = zero ? trail.lastY : end.screenY;
  const known = x != null && y != null;
  if (
    known &&
    (x < win.screenX || y < win.screenY || x >= win.screenX + win.outerWidth || y >= win.screenY + win.outerHeight)
  )
    return { x, y };
  // No trustworthy end point: the drag's leaving the document is the word.
  if (zero && trail.left) return known ? { x, y } : {};
  return null;
}

/** Follow one drag from its start: call at dragstart, then `end(ev)` at
 * dragend, which stops listening and answers {@link dragEndedOutside}. */
export function trackDrag(): { end(ev: DragEndInfo): { x?: number; y?: number } | null } {
  const trail: DragTrail = { left: false, lastX: null, lastY: null };
  const at = (ev: DragEvent) => {
    if (ev.screenX || ev.screenY) {
      trail.lastX = ev.screenX;
      trail.lastY = ev.screenY;
    }
  };
  const onDrag = (ev: DragEvent) => at(ev);
  const onOver = (ev: DragEvent) => {
    trail.left = false;
    at(ev);
  };
  const onLeave = (ev: DragEvent) => {
    if (!ev.relatedTarget) trail.left = true;
  };
  document.addEventListener("drag", onDrag, true);
  document.addEventListener("dragover", onOver, true);
  document.addEventListener("dragleave", onLeave, true);
  let done = false;
  return {
    end(ev) {
      if (!done) {
        done = true;
        document.removeEventListener("drag", onDrag, true);
        document.removeEventListener("dragover", onOver, true);
        document.removeEventListener("dragleave", onLeave, true);
      }
      return dragEndedOutside(ev, window, trail);
    },
  };
}

/** Where the new window goes: its top-left so the pointer keeps its grip on
 * the header (`grabX` px in, a few px down), at the size of what was dragged
 * (when that has one — a rail row lets the window take its default). */
export function tearOffPlacement(
  pt: { x?: number; y?: number },
  grabX: number,
  size?: { width: number; height: number }
): PopoutPlacement {
  const at: PopoutPlacement = {};
  if (pt.x != null && pt.y != null) {
    at.x = Math.round(pt.x - Math.max(0, grabX));
    at.y = Math.round(pt.y - 12);
  }
  if (size && size.width > 0 && size.height > 0) {
    at.width = Math.round(size.width);
    at.height = Math.round(size.height);
  }
  return at;
}
