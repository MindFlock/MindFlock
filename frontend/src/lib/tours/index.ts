/** The tours: one short coach-mark walkthrough per top-bar surface, plus the
 * first-hour Welcome tour over the Get set up checklist.
 *
 * THE CONTRACT (round 5) — every surface codes against it:
 *  - `TourId` names a tour; `TOURS[id]` is its definition.
 *  - A step points at `target`, a CSS selector resolved in `document` (a
 *    selector LIST is fine: the first match in the page wins, so a step can
 *    name a `[data-tour="…"]` hook and today's id as its fallback). `""`, or
 *    a target that isn't on screen, centres the card and still shows the step.
 *    A target below the fold of a scrolling pane is scrolled into view (a
 *    closed <details> around it is opened first) by the coach mark itself.
 *  - `screen` (optional) names a screen of the surface to switch to first.
 *  - One file per tour (`./<id>.ts`). A surface's owner refines its own file
 *    as the surface changes; hooks it adds are named
 *    `data-tour="<surface>-<thing>"` once and kept (tests pin them).
 *
 * People (Settings → People) is the one tour of a Settings screen: its "?"
 * sits in that screen's header, and replaying it opens Settings on People
 * (settings/screens/peer/replayPeopleTour).
 *
 * Voice walks the top-bar mic, not a dialog: it opens by itself the first
 * time the mic is used (components/TopBarMic), its copy is built from the
 * current voice words each time it is read (./voice.ts), and its last steps'
 * `screen` "mic-pop" opens the mic's options.
 *
 * Rendered by components/onboarding/CoachTour.tsx (CoachTourHost), opened by
 * the store's `openCoachTour(id)` — from a dialog header's "?"
 * (onboarding/TourButton), Settings → General → Tours, the palette, or ONCE
 * by itself in the calm first hour (onboarding/useAutoTour). Closing it any
 * way marks it seen (synced pref `tours_seen`). Pure data: no React, no store. */

import { WELCOME_TOUR } from "./welcome";
import { NEW_TOUR } from "./new";
import { INTAKE_TOUR } from "./intake";
import { VERIFY_TOUR } from "./verify";
import { SCHEDULE_TOUR } from "./schedule";
import { SETTINGS_TOUR } from "./settings";
import { PEOPLE_TOUR } from "./people";
import { VOICE_TOUR } from "./voice";

export type TourId = "welcome" | "new" | "intake" | "verify" | "schedule" | "settings" | "people" | "voice";

export interface TourStep {
  /** CSS selector resolved in `document`; "" centres the card. */
  target: string;
  title: string;
  /** One plain sentence. */
  body: string;
  /** The screen of the tour's surface this step shows, switched to before the
   * step is measured — so one tour can walk several screens. Settings reads
   * it (a Settings screen key: "connections", "coding", …) and follows the
   * store's `coachTour` step; Back returns to the step's screen too. The
   * Voice tour's "mic-pop" opens the top-bar mic's options instead. Omit it
   * and the step stays on whatever is showing. */
  screen?: string;
}

export interface TourDef {
  id: TourId;
  /** The surface's name as the UI says it: "Replay the <title> tour". */
  title: string;
  steps: TourStep[];
}

export const TOURS: Record<TourId, TourDef> = {
  welcome: WELCOME_TOUR,
  new: NEW_TOUR,
  intake: INTAKE_TOUR,
  verify: VERIFY_TOUR,
  schedule: SCHEDULE_TOUR,
  settings: SETTINGS_TOUR,
  people: PEOPLE_TOUR,
  voice: VOICE_TOUR,
};

/** Every tour, in the order Settings → General → Tours lists them. */
export const TOUR_IDS: readonly TourId[] = ["welcome", "new", "intake", "verify", "schedule", "settings", "people", "voice"];

export function isTourId(v: unknown): v is TourId {
  return typeof v === "string" && (TOUR_IDS as readonly string[]).includes(v);
}
