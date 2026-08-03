/** The welcome walkthrough — a slideshow that introduces MindFlock and, more
 * usefully, walks a new user through the non-obvious setup: connecting a coding
 * provider, ticket ingestion, PR review, issue handling, a linked IDE, and
 * mobile access. Opens automatically on first run (see App) and is replayable
 * from Settings → General.
 *
 * Setup slides carry a `screen` key; their "Set up now" button closes the tour
 * (marking it done) and jumps straight to that Settings screen — so a user can
 * act on the exact thing they're reading about. Everything else is Back/Next. */

import { useEffect, useState } from "react";
import { useUi } from "../../state/store";
import "./WelcomeTour.css";

interface Slide {
  /** Emoji icon, or omit and set `logo` to show the MindFlock mark. */
  icon?: string;
  logo?: boolean;
  title: string;
  body: React.ReactNode;
  /** Settings screen key this slide's "Set up now" button jumps to. */
  screen?: string;
}

const SLIDES: Slide[] = [
  {
    logo: true,
    title: "Welcome to MindFlock",
    body: (
      <>
        MindFlock runs a private flock of AI coding agents, each in its own git
        worktree, all of them on this machine — there is no MindFlock cloud and no
        account. Start one yourself with any agent CLI — or let a ticket assigned
        to you in Jira, Linear, GitHub Issues, Shortcut or Asana start it for you,
        with the ticket already seeded. Either way you review the diff and merge.
        This tour
        covers the basics and then walks you through connecting your accounts. You
        can skip it any time and replay it later from <b>Settings → General</b>.
      </>
    ),
  },
  {
    icon: "▦",
    title: "Sessions & the grid",
    body: (
      <>
        Start work with <b>New Session</b> in the top bar. Every session gets a
        pane in the grid. The <b>View</b> buttons at the bottom of the sidebar
        (Auto / 2 / 4 / 9) decide how many panes show at once — the rest keep
        running, hidden, until you bring them forward. Drag sessions to reorder.
      </>
    ),
  },
  {
    icon: "⚙",
    title: "Your sidebar, your way",
    body: (
      <>
        You start with <b>Usage</b>, <b>Ticket Ingestion</b> and{" "}
        <b>Assistant</b>. Click <b>⚙ Customize</b> at the bottom of the sidebar to
        switch on <b>PR review</b> and <b>issue handling</b> whenever you want them
        — and drag any bar to reorder.
      </>
    ),
  },
  {
    icon: "💬",
    title: "Your assistant",
    body: (
      <>
        The <b>Assistant</b> bar is a personal AI helper, <b>powered by whichever
        coding provider you choose</b>. It answers questions, manages your{" "}
        <b>Todo</b> list, and follows standing instructions you set under{" "}
        <b>Agent</b> — handy for planning before you spin up sessions.
      </>
    ),
  },
  {
    icon: "🔗",
    title: "Connect your accounts",
    body: (
      <>
        The powerful features need a one-time hookup to your outside services.
        <b> Settings → Connections</b> is your at-a-glance dashboard — it shows
        what's <b>Connected</b>, <b>Action needed</b>, or <b>Not connected</b>,
        and its <b>Configure</b> links jump to each setup screen. The next few
        steps cover each one.
      </>
    ),
    screen: "connections",
  },
  {
    icon: "🤖",
    title: "1. Coding provider",
    body: (
      <>
        Pick the CLI new sessions launch with (Claude, Codex, or any provider you
        add) and any default launch flags. Then hit the <b>Agent CLI check</b> —
        it probes the binary <i>and</i> its login state, so you catch a
        not-installed or not-logged-in provider before your first session, not
        during it.
      </>
    ),
    screen: "coding",
  },
  {
    icon: "🎫",
    title: "2. Ticket ingestion",
    body: (
      <>
        Add a source — <b>Jira, Linear, GitHub Issues, Shortcut or Asana</b> —
        and paste its <b>API token</b>. The non-obvious part: set each source's{" "}
        <b>Repo URL</b> so agents know which repository to clone and branch from.
        Press <b>Test</b> on a source to confirm the credentials before you rely
        on it. New tickets then spin up sessions automatically.
      </>
    ),
    screen: "ticketing",
  },
  {
    icon: "🔀",
    title: "3. PR review",
    body: (
      <>
        List repositories as <b>owner/name</b> (e.g. <code>mindflockai/MindFlock</code>),
        then paste a <b>GitHub token</b>. That token is the whole setup: it also
        lets MindFlock open and merge PRs for you. It falls back to{" "}
        <code>$GH_TOKEN</code> / <code>$GITHUB_TOKEN</code>, and to{" "}
        <code>gh auth token</code> if you happen to have the GitHub CLI — which is
        optional, not required. <b>Pushing</b> is always plain <code>git push</code>{" "}
        over the remote you already use, so an SSH remote needs nothing extra. Tune{" "}
        <b>base branch</b>, <b>min PR age</b>, <b>poll interval</b>, and{" "}
        <b>skip authors</b> (e.g. dependabot) to control what gets reviewed.
      </>
    ),
    screen: "repo",
  },
  {
    icon: "🐛",
    title: "4. Issue handling",
    body: (
      <>
        MindFlock can watch repos for <b>newly opened issues</b> and auto-start a
        session on a fresh branch for each. Two prerequisites that trip people up:
        it needs <b>git installed</b>, and it runs alongside ticket ingestion, so
        it needs a <b>connected ticketing source</b>. Its repo list is independent
        from PR review's.
      </>
    ),
    screen: "issues",
  },
  {
    icon: "🖥️",
    title: "5. Linked IDE",
    body: (
      <>
        Choose the editor MindFlock opens worktrees in. Detected editors are
        selectable; missing ones are grayed out. <b>VS Code-family editors</b>{" "}
        (code, cursor, windsurf) get the best integration — window focus and
        auto-adopt — but you can point to any editor with a{" "}
        <b>custom command</b> (e.g. <code>zed</code>).
      </>
    ),
    screen: "ide",
  },
  {
    icon: "📱",
    title: "6. Mobile & remote (optional)",
    body: (
      <>
        Want to check on sessions from your phone or another machine? Turn on{" "}
        <b>Tailscale mode</b>, then use the <b>access token</b> to reach this
        MindFlock securely from any device on your tailnet. Skip this if you only
        ever work at one desk.
      </>
    ),
    screen: "mobile",
  },
  {
    logo: true,
    title: "You're all set",
    body: (
      <>
        That's the tour. Watch for <b>💡 hints</b> around the app as you go — turn
        them off (or replay this walkthrough) any time under{" "}
        <b>Settings → General</b>. The <b>Doctor</b> screen flags anything that
        still needs attention. Happy flocking!
      </>
    ),
  },
];

/** The brand mark, painted with the current accent via CSS mask. */
function Logo() {
  return <div className="wt-logo" role="img" aria-label="MindFlock" />;
}

export function WelcomeTour() {
  const open = useUi((s) => s.tourOpen);
  // While Settings is open we PAUSE the tour: hide it but stay mounted so the
  // slide index survives. Closing Settings brings the tour back where it was.
  const settingsOpen = useUi((s) => s.openDialog === "settings");
  const finishTour = useUi((s) => s.finishTour);
  const openDialogFor = useUi((s) => s.openDialogFor);
  const [i, setI] = useState(0);

  // Reset to the first slide whenever the tour (re)opens — but not when it's
  // merely paused behind Settings.
  useEffect(() => {
    if (open) setI(0);
  }, [open]);

  const paused = open && settingsOpen;

  useEffect(() => {
    if (!open || paused) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") finishTour();
      else if (e.key === "ArrowRight") setI((n) => Math.min(n + 1, SLIDES.length - 1));
      else if (e.key === "ArrowLeft") setI((n) => Math.max(n - 1, 0));
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, paused, finishTour]);

  // Return null (not unmount) while closed or paused: hooks above keep running
  // and `i` is retained for when the tour reappears.
  if (!open || paused) return null;

  const last = i === SLIDES.length - 1;
  const slide = SLIDES[i];

  // Open Settings ON TOP of the (now paused) tour instead of ending it, so the
  // user lands back on this exact slide when they close Settings.
  const jumpTo = (screen: string) => {
    openDialogFor("settings", screen);
  };

  return (
    <div
      id="welcome-tour"
      className="modal"
      onClick={(e) => {
        if (e.target === e.currentTarget) finishTour();
      }}
    >
      <div className="wt-card">
        <button type="button" className="wt-skip" onClick={finishTour}>
          Skip
        </button>
        <div className="wt-content">
          <div className="wt-head">
            {slide.logo ? <Logo /> : <span className="wt-icon" aria-hidden="true">{slide.icon}</span>}
            <h2 className="wt-title">{slide.title}</h2>
          </div>
          <p className="wt-body">{slide.body}</p>
          {slide.screen && (
            <button type="button" className="wt-setup" onClick={() => jumpTo(slide.screen!)}>
              Set up now →
            </button>
          )}
        </div>

        <div className="wt-foot">
          <div className="wt-dots" role="tablist" aria-label="Tour progress">
            {SLIDES.map((_, n) => (
              <button
                key={n}
                type="button"
                className={"wt-dot" + (n === i ? " active" : "")}
                aria-label={`Go to step ${n + 1}`}
                aria-selected={n === i}
                onClick={() => setI(n)}
              />
            ))}
          </div>

          <div className="wt-nav">
          <button
            type="button"
            className="wt-btn ghost"
            disabled={i === 0}
            onClick={() => setI((n) => Math.max(n - 1, 0))}
          >
            Back
          </button>
          {last ? (
            <div className="wt-nav-end">
              <button type="button" className="wt-btn ghost" onClick={() => jumpTo("connections")}>
                Open Settings
              </button>
              <button type="button" className="wt-btn primary" onClick={finishTour}>
                Get started
              </button>
            </div>
          ) : (
            <button
              type="button"
              className="wt-btn primary"
              onClick={() => setI((n) => Math.min(n + 1, SLIDES.length - 1))}
            >
              Next
            </button>
          )}
          </div>
        </div>
      </div>
    </div>
  );
}
