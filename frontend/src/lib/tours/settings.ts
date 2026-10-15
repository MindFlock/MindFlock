/** The Settings tour: the status page, GitHub, search, and where the rest
 * lives. Seeded from the r5-settings judge's tutorial draft. Every target
 * exists today; `settings-github` is the hook Settings' owner may add for a
 * more precise ring. */
import type { TourDef } from ".";

export const SETTINGS_TOUR: TourDef = {
  id: "settings",
  title: "Settings",
  steps: [
    {
      target: '.set-nav-item[data-screen="connections"]',
      title: "Your status page",
      body: "Connections lists everything MindFlock talks to: what is connected and what needs you.",
    },
    {
      target: '[data-tour="settings-github"], [data-conn="github"]',
      title: "Connect GitHub when you need PRs",
      body: "Agents need GitHub to open pull requests; pushing already works without it.",
    },
    {
      target: "#settings-search",
      title: "Search finds any setting",
      body: "Type a word like dark, phone or parallel and jump straight to it.",
    },
    {
      target: "#settings-advanced-toggle",
      title: "The rest waits here",
      body: "Phones, other computers, local models and troubleshooting live behind Show advanced settings.",
    },
  ],
};
