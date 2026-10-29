/** The Flights tour: the sky (the main area's second view — every session a
 * line of blocks), the bird on each line, starting a session from the space,
 * then the Flyways editor and Describe a stop. Each step names its data-tour
 * hook first; a step whose hook isn't on screen (the grid is showing, or no
 * flight is in the air yet, so no bird) falls back to the top bar's Grid ·
 * Sky toggle (`view-sky`), the tab button, or centres. `screen` is the
 * Flights tab the step shows (FlightsDialog follows the store's coachTour
 * step); the sky's steps name none — the sky is not a tab. */
import type { TourDef } from ".";

export const FLIGHTS_TOUR: TourDef = {
  id: "flights",
  title: "Flights",
  steps: [
    {
      target: '[data-tour="view-sky"]',
      title: "Every session in the sky",
      body: "Grid · Sky (Alt+Y) shows the same sessions as lines — drag empty sky to pan, Ctrl+wheel to zoom, Tidy to put lines back.",
    },
    {
      target: '[data-tour="flights-bird"], [data-tour="view-sky"]',
      title: "The bird is where it is",
      body: "It stands on the stop each flight is at: bobbing while the flock works, gold when perched on you.",
    },
    {
      target: '[data-tour="flights-newblock"], [data-tour="view-sky"]',
      title: "Build from the space",
      body: "In the sky, double-click (or + New session, Ctrl+N) to start one right there, and press + on any line to tack on a stop.",
    },
    {
      target: '[data-tour="flights-plans"], #flights-tab-plans',
      title: "Flyways are stops on a line",
      body: "Drag boxes to reorder them, press + to add a stop, and add a perch wherever the flight should wait for you.",
      screen: "plans",
    },
    {
      target: '[data-tour="flights-describe"], #flights-tab-plans',
      title: "Describe a stop",
      body: "Say what should happen in a sentence and the stops are added for you to check and save.",
      screen: "plans",
    },
  ],
};
