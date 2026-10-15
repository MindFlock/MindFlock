/** The Intake tour: add a ticket source, start a ticket by hand, then
 * auto-start. Seeded from the r5-intake judge's tutorial draft (its 3-step
 * "after the fixes" version plus the hand-start step). `intake-start` and
 * `intake-autostart` are hooks Intake's owner adds; until then those steps
 * centre. */
import type { TourDef } from ".";

export const INTAKE_TOUR: TourDef = {
  id: "intake",
  title: "Intake",
  steps: [
    {
      target: "#intake-tabs",
      title: "Where sessions come from",
      body: "Intake turns tickets, pull requests and issues into sessions — one tab for each.",
    },
    {
      target: '[data-tour="intake-add-source"], #intake-add-source',
      title: "Add a ticket source",
      body: "Connect Shortcut, Jira, Linear, Asana or GitHub Issues; your token is tested before anything is saved.",
    },
    {
      target: '[data-tour="intake-start"]',
      title: "Start one by hand first",
      body: "Start opens a ticket as a session in the sidebar, on its own branch.",
    },
    {
      target: '[data-tour="intake-autostart"]',
      title: "Auto-start, when you trust it",
      body: "Pick the states that mean ready and turn auto-start on; Pause intake stops it all at once.",
    },
  ],
};
