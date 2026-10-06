/** The welcome walkthrough — a short slideshow that says what MindFlock is and
 * where its few everyday surfaces live: New, the grid, the ship step and the
 * bell, Intake. Opens automatically on first run (see App) and is replayable
 * from Settings → General.
 *
 * It used to be twelve slides that walked every setup screen in turn, which put
 * a page of configuration between a new user and their first session. Setup now
 * waits until it is needed; the one slide that names a setup surface (Intake)
 * carries a `screen` key, and its "Set up now" button pauses the tour and jumps
 * there. Everything else is Back/Next. */

import { useEffect, useState } from "react";
import { useUi } from "../../state/store";
import { LEGACY_SCREEN_TABS } from "../intake/IntakeDialog";
import "./WelcomeTour.css";

interface Slide {
  /** Emoji icon, or omit and set `logo` to show the MindFlock mark. */
  icon?: string;
  logo?: boolean;
  title: string;
  body: React.ReactNode;
  /** Where this slide's "Set up now" button jumps to — a Settings screen key,
   * or an Intake tab key when the setup step lives there. */
  screen?: string;
}

const SLIDES: Slide[] = [
  {
    logo: true,
    title: "Welcome to MindFlock",
    body: (
      <>
        MindFlock runs coding agents side by side on this machine — there is no
        account and no cloud. Each session is its own branch and folder with an
        agent working in it; you review the diff and ship it.
      </>
    ),
  },
  {
    icon: "▦",
    title: "Sessions & the grid",
    body: (
      <>
        Press <b>New</b> (Ctrl+N) and say what to work on — one task, or one per
        line. Each session gets a pane in the grid and a row in the sidebar; drag
        rows to reorder. <b>View</b> at the bottom of the sidebar picks how many
        panes show at once. The <b>Assistant</b> bar is a personal helper with a
        todo list.
      </>
    ),
  },
  {
    icon: "⏩",
    title: "Shipping",
    body: (
      <>
        When an agent stops, its pane's button offers the next step —{" "}
        <b>Commit…</b>, <b>Push</b>, <b>Make PR</b>. <b>⏩ Fast-track</b> takes
        those steps for you, as far as you choose, and can ask first. Anything
        waiting on you — a question or an approval — shows under the{" "}
        <b>bell</b>.
      </>
    ),
  },
  {
    icon: "🎫",
    title: "Where work comes from",
    body: (
      <>
        <b>Intake</b> turns tickets, PRs and issues into sessions (Jira, Linear,
        GitHub Issues, Shortcut, Asana). <b>Verify</b> checks what you shipped.{" "}
        <b>⚙ Customize</b> at the bottom of the sidebar adds more bars — like{" "}
        <b>Prompts</b>, your saved text one click from any session.
      </>
    ),
    screen: "ticketing",
  },
  {
    logo: true,
    title: "You're all set",
    body: (
      <>
        Watch for <b>💡 hints</b> around the app as you go — replay this tour or
        turn hints off under <b>Settings → General</b>. <b>Settings → Doctor</b>{" "}
        flags anything that still needs attention.
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
  // While a setup dialog is open we PAUSE the tour: hide it but stay mounted so
  // the slide index survives. Closing that dialog brings the tour back where it
  // was. All three dialogs count — .modal carries no z-index, and the tour
  // renders last in App.tsx, so an unpaused tour would paint on top of whichever
  // dialog its own "Set up now" / "Open Settings" just opened, or of the Setup
  // checklist a failing doctor probe popped.
  const settingsOpen = useUi(
    (s) => s.openDialog === "settings" || s.openDialog === "intake" || s.openDialog === "setup"
  );
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

  // Open the setup surface ON TOP of the (now paused) tour instead of ending it,
  // so the user lands back on this exact slide when they close it. A legacy
  // Settings key that moved to Intake routes there; anything else is Settings.
  const jumpTo = (screen: string) => {
    const tab = LEGACY_SCREEN_TABS[screen];
    openDialogFor(tab ? "intake" : "settings", tab || screen);
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
