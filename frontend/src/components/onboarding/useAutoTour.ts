/** A top-bar dialog's tour, opened by itself ONCE: the first time the dialog
 * opens in the calm first hour (lib/calm.ts — a newcomer with fewer than three
 * sessions, decided from the server's setup record, never from the render
 * hint), and only while the tour isn't in the synced `tours_seen` list —
 * which this waits for (the page's first prefs pull), so a tour seen on
 * another device never opens here. Never outside the first hour, never while
 * hints are off, the welcome card is up, or another tour is running.
 *
 * It also ties the tour to its dialog: closing the dialog closes the tour —
 * an exit like any other, so it counts as seen.
 *
 * The calm decision is read from the shared query cache when the dialog
 * opens (App keeps the instances, setup and config queries live), not
 * subscribed to: the dialogs render in node tests with no DOM, and a
 * subscription would start the instances poll there. */

import { useEffect, useRef } from "react";
import { useUi } from "../../state/store";
import { queryClient } from "../../state/queries";
import { SETUP_KEY } from "../setup/useSetup";
import { isVerifySession } from "../dialogs/verify";
import { calmDecision } from "../../lib/calm";
import { firstPrefsPull } from "../../lib/prefsSync";
import type { TourId } from "../../lib/tours";
import type { Config, Instance, SetupStatus } from "../../api/types";

/** How long after the dialog opens the tour appears: its rows have painted. */
export const AUTO_TOUR_DELAY_MS = 400;
/** While the server hasn't said whether this is the first hour, look again
 * this often, this many times — then give up (unknown is never "yes"). */
const RETRY_MS = 500;
const RETRIES = 10;

/** Whether tour `id` may open by itself now. (Pure, for the tests.) */
export function autoTourDue(o: {
  open: boolean;
  /** calmDecision(): true / false, or null while the server hasn't said. */
  calm: boolean | null;
  seen: readonly string[];
  id: TourId;
  /** The first prefs pull has finished. */
  pulled: boolean;
  hints: boolean;
  /** Another tour or the welcome card is on screen. */
  busy: boolean;
}): boolean {
  return o.open && o.pulled && o.calm === true && o.hints && !o.busy && !o.seen.includes(o.id);
}

let pulled = false;
void firstPrefsPull.then(() => {
  pulled = true;
});

/** calmDecision() off the query cache. */
function calmNow(): boolean | null {
  const instances = queryClient.getQueryData<Instance[]>(["instances"]);
  if (!instances) return null;
  const sessions = instances.filter((i) => !i.device && !isVerifySession(i)).length;
  return calmDecision({
    sessions,
    status: queryClient.getQueryData<SetupStatus>(SETUP_KEY),
    onboarded: queryClient.getQueryData<Config>(["config"])?.onboarded,
  });
}

export function useAutoTour(id: TourId, open: boolean): void {
  useEffect(() => {
    if (!open) return;
    let tries = 0;
    let t = 0;
    const look = () => {
      const ui = useUi.getState();
      const calm = calmNow();
      const o = {
        open: true,
        calm,
        seen: ui.toursSeen,
        id,
        pulled,
        hints: ui.hintsEnabled,
        busy: ui.coachTour !== null || ui.tourOpen,
      };
      if (autoTourDue(o)) ui.openCoachTour(id);
      else if ((calm === null || !pulled) && ++tries < RETRIES) t = window.setTimeout(look, RETRY_MS);
    };
    t = window.setTimeout(look, AUTO_TOUR_DELAY_MS);
    return () => window.clearTimeout(t);
  }, [open, id]);

  // The dialog closed under its tour (Esc, Close, the backdrop): end it too.
  const wasOpen = useRef(open);
  useEffect(() => {
    if (wasOpen.current && !open && useUi.getState().coachTour?.id === id) useUi.getState().closeCoachTour();
    wasOpen.current = open;
  }, [open, id]);
}

/** Whether a tour that opens on a control's FIRST USE (the mic: Voice) may
 * open now: unlike a dialog's, in any hour — the control was just used, so
 * it is the moment — but still only once (not in `tours_seen`), after the
 * first prefs pull, with hints on and nothing else touring. (Pure.) */
export function firstUseTourDue(o: { seen: readonly string[]; id: TourId; pulled: boolean; hints: boolean; busy: boolean }): boolean {
  return o.pulled && o.hints && !o.busy && !o.seen.includes(o.id);
}

/** The control behind tour `id` was just used: open the tour once it has
 * painted, if {@link firstUseTourDue}. True when it is on its way. */
export function openTourOnFirstUse(id: TourId): boolean {
  const ui = useUi.getState();
  const due = firstUseTourDue({ seen: ui.toursSeen, id, pulled, hints: ui.hintsEnabled, busy: ui.coachTour !== null || ui.tourOpen });
  if (!due) return false;
  window.setTimeout(() => {
    const now = useUi.getState();
    if (!now.coachTour && !now.tourOpen && !now.toursSeen.includes(id)) now.openCoachTour(id);
  }, AUTO_TOUR_DELAY_MS);
  return true;
}
