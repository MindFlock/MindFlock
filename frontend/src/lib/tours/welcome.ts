/** The Welcome tour: after the one welcome card (the picture in one breath,
 * "Let's go"), three coach marks over the Get set up checklist — the three
 * steps to a first session, and nothing else, so a new user acts on step
 * three. Where the session then shows up (the Grid, the Sky, the bell's Needs
 * you) is the card's picture and each view's own empty state, not more steps
 * here. The setup hooks are the checklist's (`data-tour="setup-…"`), each
 * with the checklist's own `data-step` section as its fallback.
 *
 * The steps name no agent: the checklist installs and signs in to whichever
 * one is the default (its own rows say which), and "Install Claude Code" over
 * a checklist set up for Codex read as the wrong app. */
import type { TourDef } from ".";

export const WELCOME_TOUR: TourDef = {
  id: "welcome",
  title: "Welcome",
  steps: [
    {
      target: '[data-tour="setup-step-tools"], .setup-step[data-step="tools"]',
      title: "① Install your agent",
      body: "One click installs the coding agent MindFlock runs for you; each step ticks itself once it is really done.",
    },
    {
      target: '[data-tour="setup-step-agent_login"], .setup-step[data-step="agent_login"]',
      title: "② Sign in to your agent",
      body: "Sign in once in the window that opens, with that agent's own account.",
    },
    {
      target: '[data-tour="setup-step-repo"], .setup-step[data-step="repo"]',
      title: "③ Start your first agent",
      body: "Pick a repo and say what to do — that is a session: the agent works on its own branch, and the Sky shows its route.",
    },
  ],
};
