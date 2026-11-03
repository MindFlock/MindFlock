/** The Sky tour: the five things a newcomer needs before the sky is useful —
 * what a chain is, adding a stop from the shelf, wiring it, running it (and
 * answering a Wait for me), and where the help is. Everything else (the bird's
 * drags, selection, the tools, the intake lane, the zoom, every key) lives in
 * the long-form guide behind the same ? (components/flights/SkyGuide.tsx),
 * which leads with a matching "Start here".
 *
 * Targets. A selector list matches in DOCUMENT order, and the top bar comes
 * before the sky, so no step names the Grid · Sky toggle (it would always
 * win). Each target is the element itself — its `data-tour` hook and today's
 * class on the SAME element, so either finds it — and a step whose element
 * isn't on screen (an empty sky, no chain yet) centres its card. It opens by
 * itself the first time the sky is shown (SkyView), and from the sky
 * toolbar's "?". */
import type { TourDef } from ".";

export const SKY_TOUR: TourDef = {
  id: "sky",
  title: "Sky",
  steps: [
    {
      target: '[data-tour="sky-session-box"], .sky-head',
      title: "Each session is a chain",
      body: "A chain is a row of stops: its agent comes first, then the stops that run after its turn; Ctrl+Y goes back to the grid.",
    },
    {
      target: '[data-tour="sky-shelf"]',
      title: "Add a stop from the shelf",
      body: "Drag Agent, Wait for me, Ship, Verify, Wait or Loop onto a chain, or click the + after its last stop; saved chains drag on whole.",
    },
    {
      target: '.sky-port-out:not(.sky-port-off), [data-tour="sky-stop"]',
      title: "Wires set the order",
      body: "Drag from a stop's round port onto another stop to move it there; double-click a stop to edit it.",
    },
    {
      target: '[data-tour="flights-bird"]',
      title: "Run it, and answer it",
      body: "▶ Run (or your next prompt) starts the chain and the bird shows where; Wait for me holds it in Needs you until you press Done.",
    },
    {
      target: '[data-tour="sky-guide"], .sky-toolbar .tour-btn',
      title: "Help is here",
      body: "This ? replays the tour or opens the guide, with every gesture; the ? key lists the shortcuts.",
    },
  ],
};
