/** Replay the People tour (Settings → General → Help → Tours, the palette):
 * open Settings on People, then the tour over it. Lives here, not in
 * onboarding/replayTour's surface table, because People is a Settings
 * screen rather than a top-bar dialog — replayTour itself would open no
 * surface for it. */

import { useUi } from "../../../../state/store";
import { openSettings } from "../../../../lib/openSettings";
import { PEER_SCREEN } from "../../../../lib/peer";

export function replayPeopleTour(): void {
  const ui = useUi.getState();
  if (ui.coachTour) ui.closeCoachTour();
  openSettings(PEER_SCREEN);
  useUi.getState().openCoachTour("people");
}
