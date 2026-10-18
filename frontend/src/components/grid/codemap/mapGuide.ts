/** What the Map tab (CodeMapTab.tsx) hands the bird index past the tree
 * (CodeTree.tsx), which only lays it out: quiet notes about the data, a
 * request to switch on ⚠ Could break, and opening the Plan & lists panel at
 * a section. A context, so the tree's props stay the tree's. */

import { createContext } from "react";
import type { MapSection } from "../../../state/store";

export interface MapGuide {
  /** quiet one-liners shown under the Changes header ("Showing the first 40,000 files") */
  notes: string[];
  /** bumps each time something asks for the ⚠ Could break layer; the bird index switches it on once per bump */
  riskAsk: number;
  /** open the Plan & lists panel at a section */
  openPanel: (section: MapSection) => void;
}

export const MapGuideContext = createContext<MapGuide>({ notes: [], riskAsk: 0, openPanel: () => {} });
