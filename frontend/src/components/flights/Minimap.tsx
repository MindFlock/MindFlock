/** The minimap and the off-screen hints (docs/flights.md "Round 17": task
 * `minimap`). This file is the SEAM: the props below are the contract the
 * sky (SkyView) feeds; the `minimap` task fills the bodies in. Until then both
 * render nothing, so the sky can mount them from day one. */

import type { ReactElement } from "react";

import type { SkyCamera } from "../../lib/skyLayout";

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

export function Minimap(_props: MinimapProps): ReactElement | null {
  return null;
}

export interface OffscreenHintsProps {
  rects: readonly MinimapRect[];
  cam: SkyCamera;
  viewport: { w: number; h: number };
  onJump(id: string): void;
}

/** Edge markers for lines outside the view (a bird-coloured chip with the
 * name, pinned to the edge nearest the line, like a game's off-screen HUD). */
export function OffscreenHints(_props: OffscreenHintsProps): ReactElement | null {
  return null;
}
