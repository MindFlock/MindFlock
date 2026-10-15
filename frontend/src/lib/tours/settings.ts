/** The Settings tour: the status page and GitHub on Connections, search, the
 * default agent (Agents), where worktrees live (Workspace), where the rest
 * waits, and where to replay this. Each step names the Settings screen it
 * shows (`screen`: SettingsDialog switches to it), so the tour walks several
 * screens and Back walks them in reverse; the first step's screen is why the
 * "?" always restarts it on Connections. Every hook is Settings' own
 * (`data-tour="settings-…"`), with today's id as the fallback where that id
 * is the hooked element or inside it — a selector list matches in DOCUMENT
 * order, so a fallback that comes earlier in the page (a nav button before
 * its screen) would win over the hook. */
import type { TourDef } from ".";

export const SETTINGS_TOUR: TourDef = {
  id: "settings",
  title: "Settings",
  steps: [
    {
      target: '[data-tour="settings-status"]',
      screen: "connections",
      title: "Your status page",
      body: "Connections lists everything MindFlock talks to: what is connected and what needs you.",
    },
    {
      target: '[data-tour="settings-github"]',
      screen: "connections",
      title: "Connect GitHub when you need PRs",
      body: "Agents need GitHub to open pull requests; pushing already works without it.",
    },
    {
      target: "#settings-search",
      screen: "connections",
      title: "Search finds any setting",
      body: "Type a word like dark, phone or parallel and jump straight to it.",
    },
    {
      target: '[data-tour="settings-default-agent"], #default-provider-select',
      screen: "coding",
      title: "Pick your default agent",
      body: "New sessions start with this agent; you can still pick another one when you start a session.",
    },
    {
      target: '[data-tour="settings-worktrees"], #workspace-paths',
      screen: "workspace",
      title: "Where your worktrees live",
      body: "Each session gets a separate worktree, its own branch and copy of your repo, kept in these folders.",
    },
    {
      target: '[data-tour="settings-advanced"], #settings-advanced-toggle',
      title: "The rest waits here",
      body: "Phones, other computers, local models and troubleshooting live behind Show advanced settings.",
    },
    {
      target: '[data-tour="settings-tours"], #general-tours',
      screen: "general",
      title: "Replay any tour",
      body: "Every tour lives here under Help, next to the switch for hints.",
    },
  ],
};
