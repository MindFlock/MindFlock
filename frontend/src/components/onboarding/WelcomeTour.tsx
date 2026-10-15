/** The welcome card — ONE card on a first launch: what MindFlock is, in two
 * sentences, and one button. "Let's go" hands over to the Welcome tour
 * (lib/tours/welcome.ts): coach marks over the Get set up checklist, the
 * three steps to a first session. Skip, Esc and the backdrop just close it.
 *
 * Every exit marks it seen (the synced `welcome_tour` pref, on every device),
 * so it never comes back by itself — there is no button here that leaves it
 * half-finished and re-armed. It used to be five slides describing a grid,
 * shipping and Intake that weren't on screen yet, over the checklist that
 * was the actual path; those surfaces now have their own tours, from the
 * "?" in each dialog's header.
 *
 * Opens by itself ONCE, on a first launch (`useFirstLaunchTour` below: the
 * synced pref, never a per-browser flag), and is replayable from the palette
 * ("Replay welcome tour"), the ? sheet and Settings → General → Tours. */

import { useEffect, useState } from "react";
import { useUi } from "../../state/store";
import { useConfig, useInstances } from "../../state/queries";
import { useSetup } from "../setup/useSetup";
import { firstLaunchTour } from "../../lib/calm";
import { firstPrefsPull } from "../../lib/prefsSync";
import { Modal } from "../Modal";
import { findTarget } from "./CoachTour";
import "./WelcomeTour.css";

/** The card's words (pinned by the tests). */
export const WELCOME_TITLE = "Welcome to MindFlock";
export const WELCOME_LINES = [
  "MindFlock runs coding agents side by side on this computer, each session on its own branch.",
  "Three steps and your first agent is running.",
];

/** "Let's go": the Welcome tour over the checklist. On the empty grid the
 * checklist is already there; anywhere else (a replay with sessions open) the
 * Setup dialog brings it up first. */
export function startWelcomeTour(): void {
  const ui = useUi.getState();
  if (typeof document !== "undefined" && !findTarget(document, ".setup-checklist")) ui.openDialogFor("setup");
  ui.openCoachTour("welcome");
}

/** The brand mark, painted with the current accent via CSS mask. */
function Logo() {
  return <div className="wt-logo" role="img" aria-label="MindFlock" />;
}

/** Open the card by itself once, on a first launch (lib/calm firstLaunchTour),
 * after this page's first prefs pull — so "seen on another device" is known
 * before anything opens — and remember it as seen once it closes. */
function useFirstLaunchTour() {
  const seen = useUi((s) => s.welcomeTourSeen);
  const { data: instances } = useInstances();
  const { data: status } = useSetup();
  const { data: config } = useConfig();
  const [pulled, setPulled] = useState(false);
  useEffect(() => {
    let live = true;
    void firstPrefsPull.then(() => live && setPulled(true));
    return () => {
      live = false;
    };
  }, []);
  const due =
    pulled &&
    !!instances &&
    firstLaunchTour({ seen, sessions: instances.length, status, onboarded: config?.onboarded });
  useEffect(() => {
    if (!due) return;
    const ui = useUi.getState();
    if (!ui.tourOpen) useUi.setState({ tourOpen: true });
  }, [due]);
  // Seen once it is CLOSED, by any exit (Let's go, Skip, Esc, the backdrop).
  const open = useUi((s) => s.tourOpen);
  const [wasOpen, setWasOpen] = useState(false);
  useEffect(() => {
    if (open) setWasOpen(true);
    else if (wasOpen) {
      setWasOpen(false);
      useUi.getState().markWelcomeTourSeen();
    }
  }, [open, wasOpen]);
}

export function WelcomeTour() {
  useFirstLaunchTour();
  const open = useUi((s) => s.tourOpen);
  // Hidden (not closed) while a setup dialog is up over it: .modal carries no
  // z-index and this renders last in App.tsx, so it would paint on top of
  // Settings, Intake or the Setup checklist opened from elsewhere.
  const covered = useUi((s) => s.openDialog === "settings" || s.openDialog === "intake" || s.openDialog === "setup");
  const finishTour = useUi((s) => s.finishTour);
  if (!open || covered) return null;

  const letsGo = () => {
    finishTour();
    startWelcomeTour();
  };

  return (
    <Modal id="welcome-tour" role="dialog" aria-modal="true" aria-labelledby="wt-title" onClose={finishTour}>
      <div className="wt-card">
        <button type="button" className="wt-skip" onClick={finishTour}>
          Skip
        </button>
        <div className="wt-content">
          <div className="wt-head">
            <Logo />
            <h2 className="wt-title" id="wt-title">
              {WELCOME_TITLE}
            </h2>
          </div>
          {WELCOME_LINES.map((line) => (
            <p key={line} className="wt-body">
              {line}
            </p>
          ))}
        </div>
        <div className="wt-foot">
          {/* autoFocus: Enter is "Let's go", the one thing to do here. */}
          <button type="button" className="wt-btn primary wt-go" autoFocus onClick={letsGo}>
            Let's go
          </button>
        </div>
      </div>
    </Modal>
  );
}
