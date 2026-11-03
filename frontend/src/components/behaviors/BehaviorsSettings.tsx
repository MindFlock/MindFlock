/** The Behaviors board's ⋯ settings (docs/behaviors.md §3): the one
 * "Write behaviors on every push" switch, watched repositories, "Check
 * another branch…", "Start empty behaviors for a session…".
 *
 * SEAM (pre-committed by the integrator). The props are frozen; vs-card
 * replaces the placeholder body. The Behaviors board (vs-board) mounts it. */

import type { JSX } from "react";

export interface BehaviorsSettingsProps {
  /** Close the popover/panel it is shown in. */
  onClose?(): void;
}

export function BehaviorsSettings(_p: BehaviorsSettingsProps): JSX.Element {
  return <div className="bh-settings" />;
}
