/** "Ask how something works" (components/helpAsk/AskHelp): a small pop-up at
 * the sidebar foot where a person asks how MindFlock works and gets a short
 * answer from its docs (POST /api/help/ask, backend core/help_ask.py), zero
 * to three "Show me" pointers from the place registry (lib/helpPlaces), or
 * both. Opened from ? Help's first row and the palette; never for a guest.
 *
 * This module is only the open/closed state, so the Help menu and the
 * palette can open the pop-up without importing it (and its stylesheets,
 * whose place in the bundle follows the first import). The request, the
 * answer's shaping and the pointers' reach are components/helpAsk/askApi. */

import { create } from "zustand";
import type { HelpPlace } from "./helpPlaces";

interface AskHelpUi {
  open: boolean;
  /** Bumped per open, so the box is focused each time. */
  n: number;
  /** Hidden while one of its pointers is spotlit (it would cover it). */
  pointing: HelpPlace | null;
  show(): void;
  close(): void;
  setPointing(p: HelpPlace | null): void;
}

export const useAskHelp = create<AskHelpUi>((set, get) => ({
  open: false,
  n: 0,
  pointing: null,
  show: () => set({ open: true, pointing: null, n: get().n + 1 }),
  close: () => set({ open: false, pointing: null }),
  setPointing: (p) => set({ pointing: p }),
}));

export function openAskHelp(): void {
  useAskHelp.getState().show();
}

