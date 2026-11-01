/** The zoom readout as a menu (docs/flights.md "Round 17": task
 * `zoom-shortcuts`). SEAM: the props are the contract the sky feeds; the task
 * fills the body in. Until then it renders the plain readout. */

import type { ReactElement } from "react";

export interface ZoomMenuProps {
  zoom: number;
  min: number;
  max: number;
  onZoomTo(z: number): void;
  onFitAll(): void;
  onFitSelection(): void;
  onFitFocused(): void;
  canFitSelection: boolean;
  canFitFocused: boolean;
}

export function ZoomMenu(p: ZoomMenuProps): ReactElement {
  return (
    <span className="sky-zoom-read" aria-live="polite">
      {Math.round(p.zoom * 100)}%
    </span>
  );
}
