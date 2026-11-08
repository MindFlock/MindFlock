/** The welcome card — ONE card on a first launch: the picture in one breath
 * (docs/vocabulary.md — MindFlock runs your coding agents side by side; a
 * session is one agent on its own branch; its route is the stops the work
 * goes through until it is merged; the Sky shows every route and what needs
 * you), the three-part sketch of it (VisionPicture: session → route → Needs
 * you), and one button. "Let's go" hands over to the Welcome tour
 * (lib/tours/welcome.ts): three coach marks over the Get set up checklist,
 * the three steps to a first session. Skip, Esc and the backdrop just close
 * it.
 *
 * Every exit marks it seen (the synced `welcome_tour` pref, on every device),
 * so it never comes back by itself — there is no button here that leaves it
 * half-finished and re-armed. It used to be five slides describing a grid,
 * shipping and Intake that weren't on screen yet, over the checklist that
 * was the actual path; those surfaces now have their own tours, from the
 * "?" in each dialog's header. The picture alone is reopenable from
 * Settings → General → Help → How MindFlock works (./HowItWorks).
 *
 * Opens by itself ONCE, on a first launch (`useFirstLaunchTour` below: the
 * synced pref, never a per-browser flag), and is replayable from the palette
 * ("Replay welcome tour"), the ? sheet and Settings → General → Tours. */

import { useEffect, useRef, useState } from "react";
import { useUi } from "../../state/store";
import { useConfig, useDoctor, useInstances } from "../../state/queries";
import { isReleaseVersion, laterVersion } from "../../lib/whatsNew";
import { readLocalSeen, saveSeen, useWhatsNew } from "../whatsNew/seen";
import { useSetup } from "../setup/useSetup";
import { firstLaunchTour } from "../../lib/calm";
import { firstPrefsPull } from "../../lib/prefsSync";
import { Modal } from "../Modal";
import { findTarget } from "./CoachTour";
import { VISION_BREATH } from "../../lib/onboarding";
import { VisionPicture } from "./VisionPicture";
import "./WelcomeTour.css";

/** The card's words (pinned by the tests): the picture in one breath — its
 * first sentence as the lead, the rest as one paragraph — then what happens
 * next. */
export const WELCOME_TITLE = "Welcome to MindFlock";
export const WELCOME_LINES = [
  VISION_BREATH[0],
  VISION_BREATH.slice(1).join(" "),
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

/** The What's new version a newcomer's first welcome settles on: the
 * version they just installed (never lower than one already seen), so What's
 * new doesn't then list every release up to it as news. `null` = nothing to
 * record (a source tree, or the version isn't known). */
export function newcomerSeen(running: string | null | undefined, seen: string | null | undefined): string | null {
  return isReleaseVersion(running) ? laterVersion(running, seen) : null;
}

/** Record that a newcomer has nothing new to read (see newcomerSeen). Why it
 * is needed: What's new decides once GET /api/doctor gives it the running
 * version, which on a first launch takes seconds — and by then the welcome
 * has usually been closed, which makes What's new read the person as someone
 * who used MindFlock before and pop the whole 0.11–0.12 changelog over the
 * Get set up checklist. */
export function settleWhatsNewForNewcomer(running: string | null | undefined): void {
  const seen = newcomerSeen(running, laterVersion(useWhatsNew.getState().seen, readLocalSeen()));
  if (!seen) return;
  saveSeen(seen);
  useWhatsNew.setState({ seen });
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
  // Opened by itself (a first launch), not replayed by hand.
  const firstLaunch = useRef(false);
  useEffect(() => {
    if (!due) return;
    firstLaunch.current = true;
    const ui = useUi.getState();
    if (!ui.tourOpen) useUi.setState({ tourOpen: true });
  }, [due]);
  // Seen once it is CLOSED, by any exit (Let's go, Skip, Esc, the backdrop).
  const open = useUi((s) => s.tourOpen);
  const [wasOpen, setWasOpen] = useState(false);
  const [settle, setSettle] = useState(false);
  useEffect(() => {
    if (open) setWasOpen(true);
    else if (wasOpen) {
      setWasOpen(false);
      useUi.getState().markWelcomeTourSeen();
      if (firstLaunch.current) {
        firstLaunch.current = false;
        setSettle(true);
      }
    }
  }, [open, wasOpen]);
  // A first launch's welcome closed: nothing in What's new is news to them
  // (settleWhatsNewForNewcomer) — recorded as soon as the version is known.
  const version = useDoctor().data?.version;
  useEffect(() => {
    if (!settle || version === undefined) return;
    setSettle(false);
    settleWhatsNewForNewcomer(version);
  }, [settle, version]);
}

export function WelcomeTour() {
  useFirstLaunchTour();
  const open = useUi((s) => s.tourOpen);
  // Hidden (not closed) while a setup dialog is up over it: .modal carries no
  // z-index and this renders last in App.tsx, so it would paint on top of
  // Settings, Flights (Intake) or the Setup checklist opened from elsewhere.
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
          <p className="wt-body wt-lead">
            <b>{WELCOME_LINES[0]}</b>
          </p>
          <VisionPicture />
          {WELCOME_LINES.slice(1).map((line) => (
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
