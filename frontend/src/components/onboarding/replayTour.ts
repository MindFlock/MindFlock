/** Replay a tour on request (Settings → General → Tours, the palette): open
 * the surface it walks, then the tour over it. The Welcome tour starts from
 * its welcome card, as it did the first time. Never automatic — the one
 * automatic opening of each is useAutoTour's (and WelcomeTour's for the card). */

import { useUi } from "../../state/store";
import { openSettings } from "../../lib/openSettings";
import type { TourId } from "../../lib/tours";
import type { DialogName } from "../../state/store";

const SURFACE: Partial<Record<TourId, DialogName>> = {
  new: "new-session",
  intake: "intake",
  verify: "verify",
  schedule: "schedule",
};

export function replayTour(id: TourId): void {
  const ui = useUi.getState();
  if (ui.coachTour) ui.closeCoachTour();
  if (id === "welcome") {
    ui.closeDialog();
    ui.openTour();
    return;
  }
  if (id === "settings") {
    // On Connections: the tour's status-page and GitHub steps point there.
    openSettings("connections");
  } else {
    ui.openDialogFor(SURFACE[id]!);
  }
  useUi.getState().openCoachTour(id);
}
