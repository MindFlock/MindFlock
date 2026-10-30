/** The "?" in a top-bar dialog's header, beside Close: replays that dialog's
 * tour (lib/tours). Same place on every surface — New, Intake, Verify,
 * Schedule, Settings — so "how does this work?" always has one answer.
 *
 * The Sky's "?" (round 12) has a long answer too: it opens a small menu —
 * Replay the tour · Open the guide (components/flights/SkyGuide). An inline
 * DOM menu, never window.prompt: the desktop app has none. */

import { useEffect, useRef, useState } from "react";
import { useUi } from "../../state/store";
import { TOURS, type TourId } from "../../lib/tours";
import { openSkyGuide } from "../flights/SkyGuide";
import "./CoachTour.css";

export function TourButton({ tour }: { tour: TourId }) {
  const openCoachTour = useUi((s) => s.openCoachTour);
  if (tour === "sky") return <SkyTourButton />;
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

function SkyTourButton() {
  const openCoachTour = useUi((s) => s.openCoachTour);
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLSpanElement | null>(null);
  // A press anywhere else, or Esc, folds it.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
      wrap.current?.querySelector<HTMLElement>(".tour-btn")?.focus();
    };
    document.addEventListener("mousedown", onDown, true);
    window.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("mousedown", onDown, true);
      window.removeEventListener("keydown", onKey, true);
    };
  }, [open]);
  useEffect(() => {
    if (open) wrap.current?.querySelector<HTMLElement>(".tour-menu button")?.focus();
  }, [open]);
  const pick = (go: () => void) => {
    setOpen(false);
    go();
  };
  return (
    <span className="tour-btn-wrap" ref={wrap} data-tour="sky-guide">
      <button
        type="button"
        className="tour-btn"
        aria-label="Show me around"
        aria-haspopup="menu"
        aria-expanded={open}
        title="The Sky tour and guide"
        data-tour-id="sky"
        onClick={() => setOpen((o) => !o)}
      >
        ?
      </button>
      {open && (
        <span className="tour-menu" role="menu" aria-label="Show me around">
          <button type="button" role="menuitem" onClick={() => pick(() => openCoachTour("sky"))}>
            Replay the tour
          </button>
          <button type="button" role="menuitem" onClick={() => pick(() => openSkyGuide())}>
            Open the guide
          </button>
        </span>
      )}
    </span>
  );
}
