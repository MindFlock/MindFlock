/** The Sky tour: what the Sky is FOR first (the Sky in one line,
 * docs/vocabulary.md), then the four things that make it useful — each
 * session's route, + adds a stop, the bird shows where the work is, and Needs
 * you. Everything else (the shelf, wiring, the bird's drags, selection, the
 * tools, the Intake lane, the zoom, every key) lives in the long-form guide
 * behind the sky toolbar's ? (components/flights/SkyGuide.tsx), which opens
 * with the same picture.
 *
 * Targets. A selector list matches in DOCUMENT order, and the top bar comes
 * before the sky, so no step names the Grid · Sky toggle (it would always
 * win); the last step names the bell on purpose — it is the one place Needs
 * you lives. Each target is the element itself — its `data-tour` hook and
 * today's class on the SAME element, so either finds it — and a step whose
 * element isn't on screen (an empty sky, no session yet) centres its card.
 * It opens by itself the first time the sky is shown (SkyView), and from the
 * sky toolbar's "?". */
import type { TourDef } from ".";

export const SKY_TOUR: TourDef = {
  id: "sky",
  title: "Sky",
  steps: [
    {
      target: "",
      title: "What the Sky is for",
      body: "Air traffic control for your agents — every piece of work in flight, the route it's cleared to fly, and the ones that need you.",
    },
    {
      target: '[data-tour="sky-session-box"], .sky-head',
      title: "Each session's route",
      body: "The agent comes first, then the stops its work goes through until it is merged; Ctrl+Y shows the same sessions in the Grid.",
    },
    {
      target: '.sky-plus, [data-tour="sky-shelf"]',
      title: "+ adds a stop",
      body: "A stop moves the work, checks the work, or asks you — click + after the last stop, or drag one up from the shelf.",
    },
    {
      target: '[data-tour="flights-bird"]',
      title: "The bird shows where the work is",
      body: "It stands on the stop the work is at; ▶ Run sets the route going and the bird moves along it.",
    },
    {
      target: "#notif-btn",
      title: "Needs you",
      body: "A stop that waits for your OK, or a route that halts, lands under Needs you on the bell; the sky's ? has the full guide.",
    },
  ],
};
