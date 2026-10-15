/** The Welcome tour: after the one welcome card ("Let's go"), coach marks over
 * the Get set up checklist — the three steps to a first session — and then
 * where that session shows up. Seeded from the r5-firsthour judge's tutorial
 * draft. The setup hooks are r5w-setup's (`data-tour="setup-…"`); until they
 * land, each step falls back to the checklist's own `data-step` sections. The
 * last step centres until a session exists. */
import type { TourDef } from ".";

export const WELCOME_TOUR: TourDef = {
  id: "welcome",
  title: "Welcome",
  steps: [
    {
      target: '[data-tour="setup-next"], .setup-progress',
      title: "Three steps to your first agent",
      body: "This line always says what to do next; each step ticks itself once it is really done.",
    },
    {
      target: '[data-tour="setup-step-tools"], .setup-step[data-step="tools"]',
      title: "① Install Claude Code",
      body: "MindFlock drives Claude Code for you; one click installs it in about a minute.",
    },
    {
      target: '[data-tour="setup-step-agent_login"], .setup-step[data-step="agent_login"]',
      title: "② Sign in to your agent",
      body: "Sign in once in the window that opens, with your Claude account.",
    },
    {
      target: '[data-tour="setup-step-repo"], .setup-step[data-step="repo"]',
      title: "③ Start your first session",
      body: "Pick your repo and say what to do; the agent works on its own branch, so your checkout is untouched.",
    },
    {
      target: '[data-tour="session-row"], li.inst',
      title: "Your sessions live here",
      body: "Each session is a row: Working means the agent is busy, Idle means it is your turn — click it to watch.",
    },
  ],
};
