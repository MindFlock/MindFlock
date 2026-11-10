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
    version: "0.14.5",
    date: "2026-11-10",
    big: false,
    headline: "Verify on your phone, and a steadier server.",
    features: [
      {
        title: "Confirm verified from the phone",
        body:
          "The phone shows Verifying and Verified like the desktop, lists the steps that need you, and has Confirm verified and Not verified buttons, plus a Verify action in a session's menu.",
      },
      {
        title: "Login terminals can't freeze the server",
        body:
          "Sign-in terminals and the ticket log no longer copy the whole server to start, and terminal typing and closing happen off its main thread.",
      },
    ],
  },
  {
    version: "0.14.4",
    date: "2026-11-10",
    big: false,
    headline: "Verify is the session's own agent checking its work.",
    features: [
      {
        title: "Verifying, in the session",
        body:
          "Verify asks the session's own agent to write real test steps for what it built and work through them while you watch. A Verifying pill shows it is at work, and anything only you can do, it tells you.",
      },
      {
        title: "You confirm when a step is yours",
        body:
          "If any step needs a person, Verify waits for you: Confirm verified or Not verified, right on the pane, the bell or the route's stop.",
      },
      {
        title: "No more white panes",
        body:
          "Terminals repaint when the window comes back, so a pane no longer stays blank until you scroll. New agents also stop waiting on a memory reading that was never short.",
      },
    ],
  },
  {
    version: "0.14.3",
    date: "2026-11-10",
    big: false,
    headline: "Memory warnings you can trust, and voice that sends.",
    features: [
      {
        title: "Memory reads real trouble",
        body:
          "Low and critical now mean processes are stalling on memory, swap is being read back, or Windows is near its commit limit, held for 30 seconds, and each warning names the signal. Starts no longer wait while WSL has gigabytes it can reuse.",
      },
      {
        title: "Over sends again",
        body:
          "Saying \"over\" presses Enter again, and \"over and out\" sends what is already in the box. Voice also stays on when memory gets low.",
      },
      {
        title: "Verify talks to you in the session",
        body:
          "Check now hands the behaviors to the session that wrote the work: they show up on its todo list, and you can talk to it while it walks them.",
      },
    ],
  },
  {
    version: "0.14.2",
    date: "2026-11-09",
    big: false,
    headline: "Verify you can hand to an agent, start to finish.",
    features: [
      {
        title: "Checks an agent can walk",
        body:
          "Each behavior now says what to do and what to expect, in order, so the checker triggers things itself instead of waiting for someone to have used the feature.",
      },
      {
        title: "Agent only means the agent does it all",
        body:
          "Choose Agent only on a Verify stop, or \"Let the agent walk every behavior\" on a Behaviors card, and the agent tries your steps too. It needs evidence to pass one, and it still asks you for what it can't do.",
      },
      {
        title: "Checks start on the right commit",
        body:
          "MindFlock now checks out the code under test before the checker starts, so a check no longer fails on its first step.",
      },
    ],
  },
  {
    version: "0.14.1",
    date: "2026-11-09",
    big: false,
    headline: "Your assigned epics, in their own tab.",
    features: [
      {
        title: "Intake → Epics",
        body:
          "Every Shortcut epic you own is listed in its own Intake tab, grouped by state, with how many of your stories are left. Start any of them; the ones in your auto-start states still start by themselves.",
      },
    ],
  },
  {
    version: "0.14.0",
    date: "2026-11-09",
    big: true,
    headline: "Talk to MindFlock, hand it whole epics, and let reviews come back to the right session.",
    features: [
      {
        title: "Talk to MindFlock",
        body:
          "Say \"MindFlock, …\" or press Alt+M to drive the app by voice: open a dialog, start a session on a project, change a setting, or ask what a session is doing. It asks before anything destructive and never repeats back what you just did.",
      },
      {
        title: "A built-in voice",
        body:
          "Kokoro is a natural voice that runs on your machine. Install it from Settings → Voice; the assistant speaks with it.",
      },
      {
        title: "Whole epics, one lead",
        body:
          "A Shortcut source can take your epics too. Each one gets a single lead session that works your stories in it side by side, or one after another where one blocks another, with a pull request per story.",
      },
      {
        title: "Review rounds go back to work",
        body:
          "When a reviewer asks for changes on one of your PRs, the session that built it decides per request whether to do it, push back with a reply, or ask you.",
      },
      {
        title: "Verify says where, and never stalls silently",
        body:
          "A Verify stop now picks where to check (this branch or an environment like sandbox) and when it's ready. A check that can't go on tells you why, and a step only you can do comes to you.",
      },
      {
        title: "Timed todos",
        body:
          "Give a todo a time (\"call the bank tue 9am\") and it pops up then, with snooze.",
      },
    ],
  },
  {
    version: "0.13.1",
    date: "2026-11-09",
    big: false,
    headline: "Dictation that follows the whole sentence.",
    features: [
      {
        title: "Words heard in context",
        body:
          "When you dictate a message in several pauses, each part is now transcribed knowing what you already said, so fewer words come out wrong.",
      },
      {
        title: "A sharper English model",
        body:
          "English dictation now uses Whisper's English-only model. It makes about half as many mistakes on coding talk and is just as fast.",
      },
    ],
  },
  {
    version: "0.13.0",
    date: "2026-11-09",
    big: true,
    headline: "Voice that hears you right, and does more hands-free.",
    features: [
      {
        title: "Accurate words, instant commands",
        body:
          "Whisper now writes everything you dictate, while the fast Moonshine model listens only for commands like \"over\", stop and session names, so those still act the moment you say them.",
      },
      {
        title: "Scroll by voice",
        body:
          "Say \"scroll up\", \"page down\", \"scroll to the top\" or \"back to the bottom\" to move through a session's output, and \"more\" to keep going.",
      },
      {
        title: "Interrupt, go back, where am I",
        body:
          "\"Interrupt\" presses Escape in the session, \"go back\" returns to the session you were on, and \"where am I\" says which one is focused.",
      },
      {
        title: "Scratch that",
        body:
          "Say \"scratch that\" to take back just the last phrase instead of everything. \"Disregard\" still clears it all.",
      },
      {
        title: "The rail keeps up",
        body:
          "When you switch sessions, the list on the left scrolls to show the one you picked, and a voice switch makes its row blink once so you can spot it.",
      },
      {
        title: "What can I say?",
        body:
          "Ask \"what can I say\" for a cheat sheet of every voice command. It's also in the mic's right-click menu.",
      },
    ],
  },
  {
    version: "0.12.0",
    date: "2026-11-04",
    big: true,
    headline: "Steadier with many agents: they wait for memory instead of freezing the machine.",
    features: [
      {
        title: "Agents wait their turn for memory",
        body:
          "When memory is getting low, agents that start by themselves (workers, Intake, schedules, routes) wait in line and show \"Waiting for memory\", and starting one yourself asks first. Each agent also has its own memory limit, so one that runs away is stopped on its own instead of freezing everything.",
      },
      {
        title: "After a crash, sessions stay offline",
        body:
          "A session whose agent died stays Offline until you press Start, so a restart can't refill memory and crash again. Prompts sent to it wait on its queue.",
      },
      {
        title: "One name per session",
        body:
          "Renaming a session renames it everywhere — there's no hidden second name any more — and a copy is named after it with the next free number (api-fix → api-fix-2).",
      },
      {
        title: "Quieter, quicker voice",
        body:
          "Voice Lab starts only when something is spoken and stops when idle; dictation never wakes it, and speech recognition loads only while you listen. \"MindFlock\" wakes the mic faster and more reliably.",
      },
      {
        title: "The Sky opens on This session",
        body:
          "The Sky starts on the session you're on, fits its route to the window on every switch, and never pops the terminal open. Frames show their state at a glance: amber needs you, red halted.",
        action: { label: "Show me", run: "sky-tour" },
      },
    ],
  },
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
