/** The Sky tour: the top bar's Grid · Sky toggle (the main area's second
 * view — every session a line of blocks), a line, the bird on it, and the
 * Stops shelf at the sky's foot, where sessions and stops are dragged in from.
 * Each step names its data-tour hook first; a step whose hook isn't on screen
 * (the grid is showing, no session yet, or no flight in the air so no bird)
 * falls back to the toggle (`view-sky`) or centres. It opens by itself the
 * first time the sky is shown (SkyView), and from the sky toolbar's "?".
 * (It was the Flights tour, which also walked the retired Flights dialog.) */
import type { TourDef } from ".";

export const SKY_TOUR: TourDef = {
  id: "sky",
  title: "Sky",
  steps: [
    {
      target: '[data-tour="view-sky"]',
      title: "Every session in the sky",
      body: "Grid · Sky (Alt+Y) shows the same sessions as lines — drag empty sky to pan, Ctrl+wheel to zoom, Tidy to put lines back.",
    },
    {
      target: '[data-tour="sky-line"], [data-tour="view-sky"]',
      title: "One line per session",
      body: "Its blocks are the session, its workers and its flight's stops — the session box opens its pane, + tacks on a stop.",
    },
    {
      target: '[data-tour="flights-bird"], [data-tour="view-sky"]',
      title: "The bird is where it is",
      body: "It stands on the stop each flight is at: bobbing while the flock works, gold when perched on you.",
    },
    {
      target: '[data-tour="sky-shelf"], [data-tour="view-sky"]',
      title: "Stops live on the shelf",
      body: "Drag the Session chip onto the sky, or a stop onto a line — on empty sky, stops wait for a session.",
    },
  ],
};
