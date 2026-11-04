/** What's new — one entry per release, NEWEST FIRST (docs/vision/whats-new.md).
 *
 * Cutting a release (docs/development.md "Cutting a release"): add an entry for
 * the tag. `big: true` pops the What's new window once for everyone who updates
 * to it (lib/whatsNew.ts decides); a `big: false` entry only shows under "Also in
 * this release" beside a big one, or when someone reopens What's new.
 * tests/unit/test_whats_new.py fails a minor/major release with no entry.
 *
 * Words: docs/vocabulary.md (route, stop, saved route, behaviors, Verify, Halt,
 * Needs you, Sky, Grid). Each body is one or two plain sentences: what it is and
 * why you'd care. An `action.run` is an id from WHATS_NEW_ACTIONS
 * (components/whatsNew/actions.ts); an unknown id draws no button. */

export interface WhatsNewAction {
  /** The button's words ("Show me"). */
  label: string;
  /** An id from components/whatsNew/actions.ts. */
  run: string;
}

export interface WhatsNewFeature {
  title: string;
  body: string;
  action?: WhatsNewAction;
}

export interface WhatsNewRelease {
  /** "major.minor.patch", as in pyproject.toml. */
  version: string;
  /** "YYYY-MM-DD". */
  date: string;
  /** True pops the window once after an update to this release. */
  big: boolean;
  headline: string;
  features: WhatsNewFeature[];
}

export const RELEASES: WhatsNewRelease[] = [
  {
    version: "0.11.0",
    date: "2026-11-03",
    big: true,
    headline: "The Sky: see where every piece of work is, and set the route it takes.",
    features: [
      {
        title: "The Sky",
        body:
          "A second view beside the Grid: a session's route drawn as a row of stops, with a bird on the stop where the work is now. It opens on This session; All shows every route at once, and Needs you lists what is waiting on you. Ctrl+Y switches between the Grid and the Sky.",
        action: { label: "Show me", run: "sky-tour" },
      },
      {
        title: "Saved routes",
        body:
          "Build a route once — the agent, its checks, your OK, the PR — and keep it on the shelf as a saved route. Run it on a session, on its own in a folder, from an Intake source or on a schedule.",
        action: { label: "Show me", run: "saved-routes" },
      },
      {
        title: "Behaviors on the route",
        body:
          "What the work should do in real use is a list of behaviors, each checked by the agent or by you. Verify is now a stop on the session's route, and you answer them on its card there instead of in a separate dialog.",
        action: { label: "Show me", run: "behaviors" },
      },
      {
        title: "Halt",
        body:
          "A stop that fails halts the route, so nothing ships past a failure: Retry it, Skip it, or Let the agent fix it, right on the stop. Halt here on a stop ahead makes the route halt before it until you press Carry on.",
      },
      {
        title: "Loops, side-by-side stops and workers",
        body:
          "Drag from a stop's out port back onto an earlier stop to repeat it; when a check sends the work back to the agent, the return wire says why (\"fixing 1/2\"). Stops can run side by side, and workers the session starts show up inside its route, as small outlines under its agent.",
        action: { label: "Show me", run: "sky-guide" },
      },
      {
        title: "Any agent, GitHub Copilot too",
        body:
          "GitHub Copilot is now one of the agents a session can run, alongside Claude, Codex and the others. Setup's first step asks which agent you use, then installs and signs in to that one.",
        action: { label: "Choose agents", run: "agents" },
      },
    ],
  },
  {
    version: "0.10.6",
    date: "2026-10-29",
    big: false,
    headline: "Ask for workers in one keystroke.",
    features: [
      {
        title: "Ask for workers",
        body:
          "Alt+G types a request for workers into the session's prompt; Flock mode keeps asking on every prompt, so a big task gets split without you writing it out.",
      },
      {
        title: "Invite with sessions",
        body: "An invite to another person can carry the sessions you want to share, and you can chat with them across the link.",
      },
    ],
  },
];
