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
