/** The Intake tour, in the order a first source is set up: pick the tracker,
 * paste its token, name the repo, start one ticket by hand, then the
 * auto-start switches. From the r5-intake judge's "Intake in 5 steps". Each
 * step names its data-tour hook first; a step whose hook isn't on screen yet
 * (no ticket rows before a source is connected, the token field before a
 * tracker is picked) falls back to the next selector or centres. */
import type { TourDef } from ".";

export const INTAKE_TOUR: TourDef = {
  id: "intake",
  title: "Intake",
  steps: [
    {
      // The tiles while the add flow is open; "+ Add source" otherwise.
      target:
        '[data-tour="intake-tiles"], #intake-panel:not(:has([data-tour="intake-tiles"])) #intake-add-source',
      title: "Pick where your work lives",
      body: "Intake turns tickets, pull requests and issues into sessions — start by picking your tracker, or Repository for a GitHub repo.",
    },
    {
      target: '[data-tour="intake-token"]',
      title: "Paste a token",
      body: "Create a token ↗ opens the tracker's page for one; Test & continue checks it, and nothing is saved until Finish.",
    },
    {
      target: '[data-tour="intake-repo"]',
      title: "Name the repo",
      body: "Each ticket starts as a session on its own branch of this repo — the one thing Finish needs.",
    },
    {
      target: '[data-tour="intake-start"]',
      title: "Start one by hand first",
      body: "Start opens a ticket as a session in the sidebar, and Open session › takes you to it.",
    },
    {
      // The switches on the Auto-start tab; its tab button from anywhere else.
      target:
        '[data-tour="intake-autostart"], #intake-panel:not(:has([data-tour="intake-autostart"])) #intake-tab-autostart',
      title: "Auto-start, when you trust it",
      body: "Switches for tickets, PR review comments and new issues live here, with how often each checks and Pause all auto-start.",
    },
  ],
};
