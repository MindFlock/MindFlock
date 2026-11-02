/** The camera that brings a rect of the sky into view (the coach tour's
 * `mf:tour-reveal`, docs/web-ui.md "Tours"): the rect's centre to the
 * viewport's centre, zoomed out just enough that it fits with a margin (never
 * zoomed in). `r` is in viewport-local screen px under camera `c`. Pure. */
import { clampZoom, toPlane, type SkyCamera } from "./skyLayout";

export function revealCamera(c: SkyCamera, r: { x: number; y: number; w: number; h: number }, vw: number, vh: number, margin = 0.8): SkyCamera {
  const k = Math.min(1, r.w > 0 ? (vw * margin) / r.w : 1, r.h > 0 ? (vh * margin) / r.h : 1);
  const z = clampZoom(c.z * k);
  const p = toPlane(c, r.x + r.w / 2, r.y + r.h / 2);
  return { z, x: vw / 2 - p.x * z, y: vh / 2 - p.y * z };
}
