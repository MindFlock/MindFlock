/** The People tour (Settings → People): pair with another person, check the
 * safety number, then share a folder or start a joint task. Each step rings
 * its data-tour hook; the link-card hooks (safety, share, joint) aren't on
 * screen until someone is linked, so those steps centre until then. No
 * fallbacks: #peer-actions is the parent of both first boxes, and a selector
 * list matches in document order. Lives on one screen — replaying it opens
 * Settings on People first (screens/peer/replayPeopleTour). */
import type { TourDef } from ".";

export const PEOPLE_TOUR: TourDef = {
  id: "people",
  title: "People",
  steps: [
    {
      target: '[data-tour="people-invite"]',
      title: "Invite someone",
      body: "Create invite makes a one-time code — send it to the person you want to work with.",
    },
    {
      target: '[data-tour="people-join"]',
      title: "Or join them",
      body: "Got an invite instead? Paste the whole message here and press Join.",
    },
    {
      target: '[data-tour="people-safety"]',
      title: "Check the safety number",
      body: "Read it to each other: if it matches press It matches, if it differs unlink.",
    },
    {
      target: '[data-tour="people-share"]',
      title: "Share a folder",
      body: "An agent works in one shared folder inside a sandbox and talks to their agent.",
    },
    {
      target: '[data-tour="people-joint"]',
      title: "Build one feature together",
      body: "Start a joint task to agree a contract, then watch their session and mirror their work into your repo.",
    },
  ],
};
