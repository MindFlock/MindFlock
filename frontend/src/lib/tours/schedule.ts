/** The Schedule tour: the judge's six steps (r5-schedule §6) — where
 * Schedule lives, Create a template, Name + Folder (what makes it runnable),
 * Task, Run on a schedule (and its next-runs line), ▶ Run now.
 *
 * Step 1 rings the top-bar button; in the first hour that button is hidden
 * until a template exists, so the list falls through to the dialog's own
 * create button. Steps 3–5 ring the form's fields once it is open (they
 * centre while it isn't), and step 5 rings the compact Schedule… row too. */
import type { TourDef } from ".";

export const SCHEDULE_TOUR: TourDef = {
  id: "schedule",
  title: "Schedule",
  steps: [
    {
      target: '#schedule-btn, [data-tour="schedule-new"], #templates-new, #schedule-empty-create',
      title: "Sessions you'll want again",
      body: "Schedule keeps templates — a folder, an agent and a task — so a session starts with one click, or by itself.",
    },
    {
      target: '[data-tour="schedule-new"], #templates-new, #schedule-empty-create',
      title: "Create a template",
      body: "Start with a job you repeat, like a nightly test run or a dependency bump.",
    },
    {
      target: '[data-tour="schedule-name"], [data-tour="schedule-folder"]',
      title: "Name it and pick the repo",
      body: "The folder is what makes a template runnable — every session from it works there.",
    },
    {
      target: '[data-tour="schedule-task"]',
      title: "Say what to do",
      body: "Every session from this template is told this task first.",
    },
    {
      target: '[data-tour="schedule-when"]',
      title: "Let it start by itself",
      body: "Tick Run on a schedule — the line below it shows exactly when it runs next.",
    },
    {
      target: '[data-tour="schedule-run"]',
      title: "Try it once now",
      body: "▶ Run now starts a new session in the sidebar, and the card logs it under Last run.",
    },
  ],
};
