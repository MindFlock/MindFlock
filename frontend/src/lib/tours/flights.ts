/** The Flights tour: the Sky (every session a line of blocks), the bird on
 * each line, starting a session from the space, then the Flyways editor and
 * Describe a stop. Each step names its data-tour hook first; a step whose hook
 * isn't on screen (no flight in the air yet, so no bird) falls back to the
 * sky itself, the tab button, or centres. `screen` is the Flights tab the
 * step shows (FlightsDialog follows the store's coachTour step). */
import type { TourDef } from ".";

export const FLIGHTS_TOUR: TourDef = {
  id: "flights",
  title: "Flights",
  steps: [
    {
      target: '[data-tour="flights-sky"], #flights-tab-sky',
      title: "Every session in the sky",
      body: "Each session is a line of blocks — drag empty sky to pan, Ctrl+wheel to zoom, and drag a line by its bar.",
      screen: "sky",
    },
    {
      target: '[data-tour="flights-bird"], [data-tour="flights-sky"], #flights-tab-sky',
      title: "The bird is where it is",
      body: "It stands on the stop each flight is at: bobbing while the flock works, gold when perched on you.",
      screen: "sky",
    },
    {
      target: '[data-tour="flights-newblock"], #flights-tab-sky',
      title: "Build from the space",
      body: "Double-click the sky (or + New session) to start one right there, and press + on any line to tack on a stop.",
      screen: "sky",
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
