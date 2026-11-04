/** The empty Sky: no sessions, no routes on their own, nothing arriving from
 * Intake — instead of a blank canvas, a calm centred card that says what the
 * Sky is FOR (the Sky in one line, docs/vocabulary.md), the three-part picture
 * (session → route → Needs you), and the two ways work gets here: New session
 * and Connect a source in Intake.
 *
 * SkyView mounts it where its plain "nothing here" line used to draw, passing
 * that line as `children`: when the sky is only filtered or folded (a search,
 * Needs you, This session), or something is waiting in the Intake lane, the
 * plain line still shows instead. `blank` is SkyView's own reading (no
 * sessions, no route on its own, every chain shown); what is arriving is read
 * here, from the same cached Intake panels the lane lists (useWaiting).
 *
 * Its root carries `data-no-drag`, so the sky's pointer handlers (marquee,
 * pan, double-click for a new session) leave its buttons alone. */

import type { ReactNode } from "react";
import { useUi } from "../../state/store";
import { useWaiting } from "../flights/useWaiting";
import { SKY_LINE, VISION_BREATH } from "../../lib/onboarding";
import { VisionPicture } from "./VisionPicture";

/** Whether the calm card shows (pure, for the test): an empty, unfiltered
 * sky with nothing waiting in Intake. */
export function skyEmptyShows(o: { blank: boolean; waiting: number }): boolean {
  return o.blank && o.waiting === 0;
}

export const SKY_EMPTY_TITLE = "Nothing in the Sky yet";

/** `newSession`: there is no session at all (round 21) — when the plain line
 * shows instead of the card (something is waiting in Intake), it still
 * carries the New session button, so no empty view is a dead end. */
export function SkyEmpty({ blank, className, children, newSession = false }: { blank: boolean; className: string; children?: ReactNode; newSession?: boolean }) {
  // fetch=false: the Intake lane (or App's warm-up) already reads the panels.
  const { waiting } = useWaiting(false);
  const open = (d: "new-session" | "intake") => useUi.getState().openDialogFor(d);
  if (!skyEmptyShows({ blank, waiting: waiting.length })) {
    if (!children) return null;
    return newSession ? (
      <div className={className + " sky-empty-plain"} role="status" data-no-drag="">
        <p>{children}</p>
        <button type="button" className="btn-primary sky-empty-new" onClick={() => open("new-session")}>
          New session
        </button>
      </div>
    ) : (
      <p className={className} role="status">
        {children}
      </p>
    );
  }
  return (
    <div className="sky-empty-card" data-no-drag="" role="status" data-tour="sky-empty">
      <h2>{SKY_EMPTY_TITLE}</h2>
      <p>{SKY_LINE}</p>
      <VisionPicture compact />
      <p>{VISION_BREATH[1]} Start one, or let work arrive by itself from your tracker.</p>
      <div className="empty-actions">
        <button type="button" className="btn-primary sky-empty-new" onClick={() => open("new-session")}>
          New session
        </button>
        <button type="button" className="test-btn sky-empty-intake" onClick={() => open("intake")}>
          Connect a source in Intake
        </button>
      </div>
    </div>
  );
}
