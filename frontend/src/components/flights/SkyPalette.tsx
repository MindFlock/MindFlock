/** The floating blocks palette (docs/flights.md "Round 13"): the shelf's
 * content as a sky object the owner drags by its head, sizes by its corner
 * and drags blocks out of. This file is the palette worker's; the host
 * (SkyView) mounts it when `mf_sky_palette` is "float". */

import type { PointerEvent as ReactPointerEvent } from "react";
import type { CatalogDrop } from "../../lib/stopCatalog";

export const PALETTE_KEY = "palette";
export const PALETTE_MODE_KEY = "mf_sky_palette";
export type PaletteMode = "shelf" | "float";

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

/** Placeholder until the palette worker lands the real one. */
export function SkyPalette(_p: SkyPaletteProps) {
  return null;
}
