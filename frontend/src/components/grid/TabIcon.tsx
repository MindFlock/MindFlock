/** The pane header's tab icons — shared by the header (grid/Pane.tsx) and
 * Customize → Pane header's preview, so the preview draws the real thing. */

import type { ReactNode } from "react";
import type { PaneTabId } from "../../lib/paneHeader";

/** A header tab's icon. Drawn only where the head is too narrow for the
 * words (Pane.tsx's measured fit sets .head-icons); the label stays in the
 * button for screen readers and the title says what the tab is. */
const TAB_ICONS: Record<PaneTabId, ReactNode> = {
  // A sparkle: the agent.
  agent: <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" />,
  // >_ : a shell prompt.
  shell: (
    <>
      <polyline points="4 17 10 11 4 5" />
      <line x1="12" y1="19" x2="20" y2="19" />
    </>
  ),
  // ± : what changed.
  diff: (
    <>
      <path d="M12 3v12" />
      <path d="M6 9h12" />
      <path d="M6 20h12" />
    </>
  ),
  // A folded map.
  map: (
    <>
      <polygon points="2 6 9 3 15 6 22 3 22 18 15 21 9 18 2 21" />
      <line x1="9" y1="3" x2="9" y2="18" />
      <line x1="15" y1="6" x2="15" y2="21" />
    </>
  ),
  // A list: prompts waiting their turn.
  queue: (
    <>
      <line x1="9" y1="6" x2="20" y2="6" />
      <line x1="9" y1="12" x2="20" y2="12" />
      <line x1="9" y1="18" x2="20" y2="18" />
      <circle cx="4.5" cy="6" r="1" />
      <circle cx="4.5" cy="12" r="1" />
      <circle cx="4.5" cy="18" r="1" />
    </>
  ),
  // Two speech bubbles: what passed between the workers.
  thread: (
    <>
      <path d="M14 9a2 2 0 0 1-2 2H6l-4 4V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2z" />
      <path d="M18 9h2a2 2 0 0 1 2 2v11l-4-4h-6a2 2 0 0 1-2-2v-1" />
    </>
  ),
};

export function TabIcon({ name }: { name: PaneTabId }) {
  return (
    <svg
      className="tab-icon"
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {TAB_ICONS[name]}
    </svg>
  );
}
