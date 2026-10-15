/** The Schedule tour: a template, what makes it runnable, the schedule, and
 * Run now. Seeded from the r5-schedule judge's tutorial draft (its shorter
 * "after the fixes" cut). The form and card hooks are Schedule's owner's to
 * add; until then those steps centre. */
import type { TourDef } from ".";

export const SCHEDULE_TOUR: TourDef = {
  id: "schedule",
  title: "Schedule",
  steps: [
    {
      target: '[data-tour="schedule-new"], #templates-new, #schedule-empty-create',
      title: "Save a session you'll want again",
      body: "A template keeps a folder, an agent and a task, so you can start that session with one click.",
    },
    {
      target: '[data-tour="schedule-folder"]',
      title: "Pick the repo",
      body: "The folder is what makes a template runnable — every session from it works there.",
    },
    {
      target: '[data-tour="schedule-when"]',
      title: "Let it start by itself",
      body: "Tick Run on a schedule and the line below shows exactly when it runs next.",
    },
    {
      target: '[data-tour="schedule-run-now"]',
      title: "Try it once now",
      body: "Run now starts a new session in the sidebar, and the card logs it under Last run.",
    },
  ],
};
