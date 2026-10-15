/** The "?" in a top-bar dialog's header, beside Close: replays that dialog's
 * tour (lib/tours). Same place on every surface — New, Intake, Verify,
 * Schedule, Settings — so "how does this work?" always has one answer. */

import { useUi } from "../../state/store";
import { TOURS, type TourId } from "../../lib/tours";
import "./CoachTour.css";

export function TourButton({ tour }: { tour: TourId }) {
  const openCoachTour = useUi((s) => s.openCoachTour);
  return (
    <button
      type="button"
      className="tour-btn"
      aria-label="Show me around"
      title={`Replay the ${TOURS[tour].title} tour`}
      data-tour-id={tour}
      onClick={() => openCoachTour(tour)}
    >
      ?
    </button>
  );
}
