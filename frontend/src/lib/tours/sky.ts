/** The Sky tour (round 12): every surface of the Sky in the order a newcomer
 * meets it — the Grid · Sky toggle, a session's frame and its bar, the agent tile,
 * the bird, free tiles, wires, a stop's dialog, the shelf, a stops-only chain
 * (round 18: free-floating chains), selection, the
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
      title: "The same sessions, as chains",
      body: "Grid · Sky (Ctrl+Y) switches the main area between panes and the sky — the sessions are the same, only the drawing changes.",
    },
    {
      target: '[data-tour="sky-line-bar"], [data-tour="sky-line"] .skl-bar',
      title: "A session is a frame",
      body: "Drag a session's frame by its bar to put it anywhere (double-click renames); its ⋯ shows only this chain or closes it.",
    },
    {
      target: '[data-tour="sky-session-box"], .sky-head',
      title: "The agent starts the chain",
      body: "The first tile is the session's agent: click it for its terminal right here, right-click to send it a prompt or stop it.",
    },
    {
      target: '[data-tour="flights-bird"]',
      title: "The bird is where the flight is",
      body: "Click the bird to halt the flight (click again to carry on), or drag it onto another stop to jump there.",
    },
    {
      target: '[data-tour="sky-stop"], .sky-node:not(.sky-head)',
      title: "Tiles float free",
      body: "Drag a tile anywhere (it never changes the order); right-click it to edit, duplicate, halt there or remove it.",
    },
    {
      target: '.sky-port-out:not(.sky-port-off), [data-tour="sky-stop"]',
      title: "Wires set the order",
      body: "Drag from a tile's round port: onto a later stop to move it, an earlier one to loop, empty sky to add a stop there.",
    },
    {
      target: '[data-tour="sky-stop"], .sky-node:not(.sky-head)',
      title: "Every stop edits in a dialog",
      body: "Double-click a tile or press Enter for its dialog; drag a Loop's ↻ pill onto a stop to loop from there.",
    },
    {
      target: '[data-tour="sky-shelf"]',
      title: "The shelf holds the blocks",
      body: "First the default blocks, then your saved stops (several steps as one block), and All blocks… for the full list.",
    },
    {
      target: '[data-tour="sky-grounded"], .sky-socket, [data-tour="sky-shelf"]',
      title: "Stops with no session yet",
      body: "A block dropped on empty sky floats as a stops-only chain with no frame; wire an agent's port into its socket to run it.",
    },
    {
      target: '[data-tour="sky-selbar"], .sky-selbar',
      title: "Select an area",
      body: "Drag across empty sky to select chains and tiles, then move them together, Group into a stop, or Save to the shelf.",
    },
    {
      target: '[data-tour="sky-tool"]',
      title: "Drag or Click",
      body: "V is Drag (chains and tiles move, empty sky selects), H is Click (any drag pans the sky); hold Space to pan in either.",
    },
    {
      target: "",
      title: "Delete removes a tile",
      body: "Click any tile and press Delete: a stop comes off its chain (with Undo), a stops-only chain or a session asks first.",
    },
    {
      target: '[data-tour="sky-intake"], .sky-intake',
      title: "The intake lane",
      body: "Tickets and PRs waiting to fly sit here — drag it by its head, and expand it to see every item with its Fly button.",
    },
    {
      target: '[data-tour="sky-zoom"], .sky-toolbar .sky-zoom',
      title: "Zoom and fit",
      body: "+ and − zoom, 0 or Fit shows every chain legibly, 1 fills the window with the focused chain, and Ctrl+wheel zooms too.",
    },
    {
      target: '[data-tour="sky-guide"], .sky-toolbar .tour-btn',
      title: "The guide",
      body: "This ? replays the tour or opens the guide — the long version, with every key; the palette's Learn the Sky opens it too.",
    },
  ],
};
