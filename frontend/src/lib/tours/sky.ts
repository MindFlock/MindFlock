/** The Sky tour (round 12): every surface of the Sky in the order a newcomer
 * meets it — the Grid · Sky toggle, a line and its bar, the boxes, the bird,
 * a stop's toolbar and dialog, the shelf, a grounded line, selection, the
 * Drag / Click tools, Delete, the intake lane, the zoom, and the ? that opens
 * the long-form guide (components/flights/SkyGuide.tsx).
 *
 * Targets. A selector list matches in DOCUMENT order, and the top bar comes
 * before the sky, so a step never falls back to the toggle (it would always
 * win). Instead each target is the element itself — its `data-tour` hook and
 * today's class on the SAME element, so either finds it — and a step whose
 * element isn't on screen (an empty sky, no flight in the air, nothing
 * selected) centres its card. The one real fallback is the grounded line →
 * the shelf, which sits after every line in the page. `""` is a step about a
 * gesture, not a control. It opens by itself the first time the sky is shown
 * (SkyView), and from the sky toolbar's "?". */
import type { TourDef } from ".";

export const SKY_TOUR: TourDef = {
  id: "sky",
  title: "Sky",
  steps: [
    {
      target: '[data-tour="view-sky"]',
      title: "The same sessions, as lines",
      body: "Grid · Sky (Ctrl+Y) switches the main area between panes and the sky — the sessions are the same, only the drawing changes.",
    },
    {
      target: '[data-tour="sky-line-bar"], [data-tour="sky-line"] .skl-bar',
      title: "One line per session",
      body: "Drag a line by its bar to put it anywhere; its ⋯ opens the session here, shows only this line, tacks on a stop or closes it.",
    },
    {
      target: '[data-tour="sky-session-box"], .sky .fl-box-session',
      title: "Boxes are stops",
      body: "The session's own box comes first and opens its real terminal on the sky; each box after it is a stop of its flight.",
    },
    {
      target: '[data-tour="flights-bird"]',
      title: "The bird is where the flight is",
      body: "Click the bird to halt the flight (click again to carry on), or drag it onto another stop to jump there.",
    },
    {
      target: '[data-tour="sky-stop"], .sky .fl-box.fl-has-tools',
      title: "Hover a stop for its tools",
      body: "✎ edit, ⇤ ⇥ move it earlier or later, ⧉ duplicate, ⏸ halt here, 🗑 remove — right-click shows the same as a menu.",
    },
    {
      target: '[data-tour="sky-stop"], .sky .fl-box-stop, .sky .fl-box-group',
      title: "Every stop edits in a dialog",
      body: "Double-click a stop, press Enter on it, or use ✎ to open its dialog: its title, its settings, what happens if it fails.",
    },
    {
      target: '[data-tour="sky-shelf"]',
      title: "The shelf holds the blocks",
      body: "First the default blocks, then your saved stops (several steps as one block), and All blocks… for the full list.",
    },
    {
      target: '[data-tour="sky-grounded"], .skl-grounded-head, [data-tour="sky-shelf"]',
      title: "Stops before a session",
      body: "Drop a block on empty sky and it waits as a grounded line; drop the Session chip on it, or use its ⋯, to give it a session.",
    },
    {
      target: '[data-tour="sky-selbar"], .sky-selbar',
      title: "Select an area",
      body: "Drag across empty sky to select lines and boxes, then move them together, Group into a stop, or Save to the shelf.",
    },
    {
      target: '[data-tour="sky-tool"]',
      title: "Drag or Click",
      body: "V is Drag (lines move, empty sky selects), H is Click (any drag pans the sky); hold Space to pan in either.",
    },
    {
      target: "",
      title: "Delete removes a box",
      body: "Click any box and press Delete: a stop comes off its line (with Undo), a grounded line or a session asks first.",
    },
    {
      target: '[data-tour="sky-intake"], .sky-intake',
      title: "The intake lane",
      body: "Tickets and PRs waiting to fly sit here — drag it by its head, and expand it to see every item with its Fly button.",
    },
    {
      target: '[data-tour="sky-zoom"], .sky-toolbar .sky-zoom',
      title: "Zoom and fit",
      body: "+ and − zoom, 0 or Fit shows every line legibly, 1 fills the window with the focused line, and Ctrl+wheel zooms too.",
    },
    {
      target: '[data-tour="sky-guide"], .sky-toolbar .tour-btn',
      title: "The guide",
      body: "This ? replays the tour or opens the guide — the long version, with every key; the palette's Learn the Sky opens it too.",
    },
  ],
};
