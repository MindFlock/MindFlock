/** What a What's new feature's "Show me" does: `action.run` in
 * whatsNew/releases.ts names one of these. Each goes through an existing
 * helper (the view switch, a tour replay, the Behaviors focus, Settings);
 * an id not in this table draws no button (lib/whatsNew knownAction). */

import { setView } from "../../lib/viewMode";
import { openBehaviors } from "../../lib/behaviorsFocus";
import { openSettings } from "../../lib/openSettings";
import { useUi } from "../../state/store";
import { replayTour } from "../onboarding/replayTour";
import { openSkyGuide } from "../flights/SkyGuide";

export const WHATS_NEW_ACTIONS: Record<string, () => void> = {
  /** The Sky view. */
  sky: () => setView("sky"),
  /** The Sky's tour, over the Sky. */
  "sky-tour": () => replayTour("sky"),
  /** The Sky's long-form guide. */
  "sky-guide": () => {
    setView("sky");
    openSkyGuide();
  },
  /** The shelf of saved routes on the Sky. */
  "saved-routes": () => useUi.getState().showFlyways(),
  /** The Behaviors board on the Sky. */
  behaviors: () => openBehaviors({ board: true }),
  intake: () => useUi.getState().openDialogFor("intake"),
  schedule: () => useUi.getState().openDialogFor("schedule"),
  settings: () => openSettings(),
  /** Settings → Coding agents (which agent CLIs a session can run). */
  agents: () => openSettings("coding"),
};

export function isKnownAction(id: string): boolean {
  return Object.prototype.hasOwnProperty.call(WHATS_NEW_ACTIONS, id);
}

/** Run an action by id; an unknown id does nothing. */
export function runWhatsNewAction(id: string): void {
  if (isKnownAction(id)) WHATS_NEW_ACTIONS[id]();
}
