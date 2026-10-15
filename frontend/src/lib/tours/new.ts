/** The New tour: the task, where it runs, Start, and what the rest is.
 * Seeded from the r5-new judge's tutorial draft. `new-folder`, `new-start`
 * and `new-more` are hooks the New dialog's owner adds; until then each step
 * falls back to today's ids (or centres). */
import type { TourDef } from ".";

export const NEW_TOUR: TourDef = {
  id: "new",
  title: "New",
  steps: [
    {
      target: '[data-tour="new-task"], #new-describe-text',
      title: "Say what to work on",
      body: "Describe one task in plain words — the agent starts on exactly this.",
    },
    {
      target: '[data-tour="new-folder"]',
      title: "Where it works",
      body: "This is the repo it works in; each session gets its own branch, so your checkout is never touched.",
    },
    {
      target: '[data-tour="new-start"], #new-describe-start, #new-create',
      title: "Start it",
      body: "Start begins the session now — watch, steer or stop it from its pane.",
    },
    {
      target: '[data-tour="new-more"], #new-describe-skip, #new-advanced-sum',
      title: "Everything else is optional",
      body: "More options holds the agent, branch and Fast-track; the defaults are safe.",
    },
  ],
};
