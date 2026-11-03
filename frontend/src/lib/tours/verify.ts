/** The Verify tour: three steps in the Sky (docs/behaviors.md §4) — Verify is
 * a stop on the route, not a place. It walks the Behaviors stop on a
 * session's route, its card opened in the stop's place, and the Behaviors
 * board. Each target names its `data-tour` hook first and the element's own
 * class after it (either finds it); a step whose element isn't on screen (no
 * session has behaviors yet, the card is folded, the board is hidden) centres
 * its card. Replayed from Settings → Tours and the palette (replayTour opens
 * the Sky first); never automatic. */
import type { TourDef } from ".";

export const VERIFY_TOUR: TourDef = {
  id: "verify",
  title: "Verify",
  steps: [
    {
      target: '[data-tour="behaviors-stop"], .sky-node.sky-behaviors',
      title: "Behaviors live on the route",
      body: "What the work should do in real use sits on its route as a Verify stop, and its face says how they stand.",
    },
    {
      target: '[data-tour="behaviors-card"], .bh-card',
      title: "Open it in place",
      body: "Click it to open the card in place: the agent checks what it can, you answer the rest, and a failure halts the route.",
    },
    {
      target: '[data-tour="behaviors-board"], .sky-bh',
      title: "The Behaviors board",
      body: "Behaviors with no session in the Sky wait on this board, and Alt+V flies to the next one that needs you.",
    },
  ],
};
