/** Replay a tour on request (Settings → General → Tours, the palette): open
 * the surface it walks, then the tour over it. The Welcome tour starts from
 * its welcome card, as it did the first time. Never automatic — the one
 * automatic opening of each is useAutoTour's (and WelcomeTour's for the card). */

import { useUi } from "../../state/store";
import { openSettings } from "../../lib/openSettings";
import type { TourId } from "../../lib/tours";
import type { DialogName } from "../../state/store";

/** Each tour's surface and the target it opens on: Intake's tour walks the
 * Sources tab of Flights, Verify's Checklists, Schedule's Schedule. */
const SURFACE: Partial<Record<TourId, [DialogName, string | null]>> = {
  new: ["new-session", null],
  flights: ["flights", "plans"],
  intake: ["flights", "sources"],
  verify: ["flights", "checklists"],
  schedule: ["flights", "schedule"],
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
  } else if (id === "voice") {
    // The top-bar mic: nothing over it (Settings → Voice's "Show me how").
    ui.closeDialog();
  } else {
    const [name, target] = SURFACE[id]!;
    ui.openDialogFor(name, target);
  }
  useUi.getState().openCoachTour(id);
}
