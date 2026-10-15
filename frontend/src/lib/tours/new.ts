/** The New tour: the task, where it runs, Start, and what to do with the
 * session afterwards — the r5-new judge's four steps. Steps 1–3 point at the
 * New dialog's own hooks (`new-task`, `new-folder`, `new-start`); step 4 at
 * a session pane's Agent/Diff tabs and its ⋯ menu (`pane-tabs`,
 * `pane-menu`), and centres when no session is open yet. */
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
      target: '[data-tour="new-folder"], #new-describe-folder',
      title: "Where it works",
      body: "This is the repo it works in; each session gets its own branch, so your checkout is never touched.",
    },
    {
      target: '[data-tour="new-start"], #new-describe-start, #new-create',
      title: "Start it",
      body: "Start begins the session now — watch, steer or stop it from its pane.",
    },
    {
      target: '[data-tour="pane-tabs"], [data-tour="pane-menu"]',
      title: "Then, in its pane",
      body: "Diff shows what changed; when the agent stops, ⋯ → Push or Open PR ships it, and ✕ closes it (reopen it from Recently closed).",
    },
  ],
};
