/** The Flights tour: the Board, a flight's live canvas, the Flyways editor
 * and Describe a stop — the four things the surface is. Each step names its
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
      title: "Everything on its flyway",
      body: "The Board shows what is perched on you, what is in flight, what is waiting to fly (tickets, PRs, issues) and what landed.",
      screen: "board",
    },
    {
      target: '[data-tour="flights-strip"], #fl-group-flying',
      title: "Every flight, live",
      body: "Each box is a stop — ✓ done, glowing where the flight is now, outlined still to come; click a card to open its session.",
      screen: "board",
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
