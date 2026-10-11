/** The compact "Setup 3/5" chip: shows once the empty-grid card has gone
 * (a required step done, or a session exists) and until every step is done
 * or skipped. Clicking it opens the Setup modal, which renders the same
 * checklist.
 *
 * Mounted in the sidebar's footer, beside the doctor's "setup issues" chip
 * (which opens the same modal). */

import { useEffect } from "react";
import { useConfig, useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { chipLabel, showSetupChip } from "./setupModel";
import { refreshSetup, useSetup } from "./useSetup";

export function SetupChip() {
  const { data: status } = useSetup();
  const { data: config } = useConfig();
  const { data: instances } = useInstances();
  // -1 while the first snapshot is in flight: unknown, so nothing is drawn.
  const sessions = instances ? instances.length : -1;
  const haveSessions = sessions > 0;

  // The first session create marks "Pick a repo" on the server; re-read the
  // checklist when the grid goes from empty to not, so the chip says 3/5
  // without waiting for the next stale read.
  useEffect(() => {
    if (haveSessions) void refreshSetup();
  }, [haveSessions]);

  if (sessions < 0) return null;
  if (!showSetupChip({ sessions, status, onboarded: config?.onboarded })) return null;
  return (
    <button
      type="button"
      id="setup-chip"
      className="setup-chip"
      title="Finish setting up MindFlock — click for the checklist"
      onClick={() => useUi.getState().openDialogFor("setup")}
    >
      {chipLabel(status)}
    </button>
  );
}
