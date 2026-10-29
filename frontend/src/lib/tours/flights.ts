/** The Flights tour: the Board, a flight's strip, the Plans editor and
 * Describe a step — the four things the surface is. Each step names its
 * data-tour hook first; a step whose hook isn't on screen (no flight in the
 * air yet) falls back to the tab button or centres. `screen` is the Flights
 * tab the step shows (FlightsDialog follows the store's coachTour step). */
import type { TourDef } from ".";

export const FLIGHTS_TOUR: TourDef = {
  id: "flights",
  title: "Flights",
  steps: [
    {
      target: '[data-tour="flights-board"], #flights-tab-board',
      title: "Everything on its way",
      body: "The Board shows what needs you, what is in flight, what has arrived and not started, and what landed.",
      screen: "board",
    },
    {
      target: '[data-tour="flights-strip"], #fl-group-flying',
      title: "One strip per flight",
      body: "Each block is a step: ✓ done, ● where it is now, ○ still to come — click the strip to open its session.",
      screen: "board",
    },
    {
      target: '[data-tour="flights-plans"], #flights-tab-plans',
      title: "Plans are rows of blocks",
      body: "Drag blocks to reorder them, press + to add one, and add a You step wherever the flight should wait for you.",
      screen: "plans",
    },
    {
      target: '[data-tour="flights-describe"], #flights-tab-plans',
      title: "Describe a step",
      body: "Say what should happen in a sentence and the blocks are added for you to check and save.",
      screen: "plans",
    },
  ],
};
