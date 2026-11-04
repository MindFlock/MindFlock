/** Camera moves the Sky makes for one thing on it (round 21b). Pure. */
import { clampZoom, toPlane, type SkyCamera } from "./skyLayout";

/** Room kept around a card that is too big to centre, screen px. */
export const CARD_PAD = 24;

/** Enter opened a session's box: the camera that shows its card (`r`, in
 * viewport-local screen px under camera `c`) centred at no less than 100 %
 * (closer only if `c` already was), so its tabs and terminal are legible. A
 * card bigger than the window at that zoom starts at its top-left corner,
 * `CARD_PAD` in — its tabs and title are never the part cut off. */
export function cardCamera(c: SkyCamera, r: { x: number; y: number; w: number; h: number }, vw: number, vh: number): SkyCamera {
  const z = clampZoom(Math.max(1, c.z));
  const p = toPlane(c, r.x, r.y);
  const w = (r.w / c.z) * z;
  const h = (r.h / c.z) * z;
  const sx = w + 2 * CARD_PAD <= vw ? (vw - w) / 2 : CARD_PAD;
  const sy = h + 2 * CARD_PAD <= vh ? (vh - h) / 2 : CARD_PAD;
  return { z, x: sx - p.x * z, y: sy - p.y * z };
}

/** A phone's This session (round 21b): the zoom that fits a route `natural`
 * px wide (at 100 %) to `avail` px — never closer than 100 %, never further
 * than `floor` (the route fit's own floor, so stop names stay readable). */
export function phoneRouteZoom(natural: number, avail: number, floor = 0.6): number {
  if (!(natural > 0) || !(avail > 0) || natural <= avail) return 1;
  return Math.max(floor, avail / natural);
}

/** Where a sideways scroller must sit so `[left, right]` (content px) is
 * whole inside its `width`, `pad` to spare — the scroll it has when that is
 * already so. A box wider than the window starts at its left edge. */
export function scrollToShow(scroll: number, width: number, left: number, right: number, pad = 12): number {
  if (left - pad >= scroll && right + pad <= scroll + width) return scroll;
  if (right - left + 2 * pad > width || left - pad < scroll) return Math.max(0, left - pad);
  return Math.max(0, right + pad - width);
}
