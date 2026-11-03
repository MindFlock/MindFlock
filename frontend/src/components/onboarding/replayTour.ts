/** Replay a tour on request (Settings → General → Tours, the palette): open
 * the surface it walks, then the tour over it. The Welcome tour starts from
 * its welcome card, as it did the first time. Never automatic — the one
 * automatic opening of each is useAutoTour's (and WelcomeTour's for the card). */

import { useUi } from "../../state/store";
import { openBehaviors } from "../../lib/behaviorsFocus";
import { openSettings } from "../../lib/openSettings";
import type { TourId } from "../../lib/tours";
import type { DialogName } from "../../state/store";

const SURFACE: Partial<Record<TourId, DialogName>> = {
  new: "new-session",
  intake: "intake",
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
  } else if (id === "people") {
    // People is a Settings screen (key "peer"), not a top-bar dialog.
    openSettings("peer");
  } else if (id === "sky") {
    // The sky is a view, not a dialog: show it, shelf and all.
    ui.showSky();
  } else if (id === "verify") {
    // Verify is a stop on the route: the tour walks a Behaviors stop in the
    // Sky — fly to the next behaviors that need you (else the board).
    ui.closeDialog();
    openBehaviors({ next: true });
  } else if (id === "voice") {
    // The top-bar mic: nothing over it (Settings → Voice's "Show me how").
    ui.closeDialog();
  } else {
    ui.openDialogFor(SURFACE[id]!);
  }
  useUi.getState().openCoachTour(id);
}
