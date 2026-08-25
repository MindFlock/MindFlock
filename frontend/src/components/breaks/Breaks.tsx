/** Take a break, and the idle flock — both drawn with the murmuration from the
 * website (lib/flock.ts), and both a setting in Settings → General: the break
 * card is off by default and interrupts you, the flock is on by default and
 * only ever appears in a room you have already left.
 *
 * They share one component because they must not fight over the screen: while
 * the break card is up it already has its own flock, so the idle overlay stays
 * down and its countdown stays disarmed.
 *
 * Neither surface hides anything. There is no scrim on either — the grid,
 * sidebar and panes stay exactly as they were and the birds fly over them. What
 * the break card does take is the pointer and the keyboard, which is what makes
 * it a break rather than a suggestion.
 *
 * The break clock is wall-clock, not "time spent typing": the thing it is
 * counting is how long you have been at the desk, and a session that ran
 * itself for forty minutes while you watched still cost you forty minutes.
 *
 * Both surfaces arrive and leave the same way, and it is a round trip: the
 * flock streams OUT of the MindFlock mark in the top bar when it appears,
 * growing as it spreads, and folds back INTO the mark when dismissed. Birds
 * that simply materialised everywhere read as a bug; birds that come out of the
 * logo read as the app's own.
 */

import { useEffect, useRef, useState } from "react";
import { useUi } from "../../state/store";
import {
  armBreak,
  fmtElapsed,
  loadArm,
  nextArm,
  saveArm,
  snoozeArm,
  type BreakArm,
} from "../../lib/breakTimer";
import type { FlockHandle } from "../../lib/flock";
import { Flock } from "./Flock";
import { useIdle } from "./useIdle";

/** Where the birds go home to: the MindFlock mark in the top bar. Measured
 * rather than assumed, because on macOS the whole cluster is mirrored to the
 * right to clear the traffic lights. The fallback is the top-left corner the
 * mark sits in everywhere else. */
function logoPoint(): { x: number; y: number } {
  const el = document.getElementById("brand-logo");
  if (el) {
    const r = el.getBoundingClientRect();
    if (r.width || r.height) return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }
  return { x: 26, y: 22 };
}

export function Breaks() {
  const on = useUi((s) => s.breakReminder);
  const every = useUi((s) => s.breakEveryMin);
  const flockOn = useUi((s) => s.idleFlock);
  const flockAfter = useUi((s) => s.idleFlockAfterMin);

  const [dueAt, setDueAt] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  /** The break screen is on its way out: birds flying home, scrim fading, and
   * no further presses accepted. */
  const [leaving, setLeaving] = useState(false);
  const breakFlock = useRef<FlockHandle | null>(null);
  const exitTimer = useRef(0);

  // Arm at mount and re-arm whenever the setting changes. nextArm() holds the
  // rule for which of those two a given (on, every) actually is.
  useEffect(() => {
    if (!on) {
      saveArm(null);
      setDueAt(null);
      return;
    }
    const armed = nextArm(Date.now(), every, loadArm());
    saveArm(armed);
    setDueAt(armed.at);
    setNow(Date.now());
  }, [on, every]);

  // 1 Hz while reminders are on — cheap, and it is what keeps the away-time
  // on the card honest without a second timer.
  useEffect(() => {
    if (!on) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [on]);

  useEffect(() => () => window.clearTimeout(exitTimer.current), []);

  const onBreak = on && dueAt !== null && now >= dueAt;
  // Two things disarm the countdown: the switch in Settings → General, and the
  // break card, which is already flying its own denser flock and must not have
  // a second one land on top of it.
  const idle = useIdle(flockAfter * 60_000, flockOn && !onBreak);

  /** Fly the flock home, then re-arm the clock — which is what actually takes
   * the screen down, since `onBreak` is derived from the deadline. */
  const dismiss = (kind: "snooze" | "resume") => {
    if (leaving) return;
    const home = logoPoint();
    const flight = breakFlock.current?.gather(home.x, home.y) ?? 0;
    const rearm = () => {
      setLeaving(false);
      // Read the switch live: the flight home takes most of a second, and the
      // user can turn reminders off inside it (the arm effect has already
      // cleared the deadline, and writing a new one would undo that).
      if (!useUi.getState().breakReminder) return;
      const arm: BreakArm =
        kind === "snooze" ? snoozeArm(Date.now(), every) : armBreak(Date.now(), every);
      saveArm(arm);
      setDueAt(arm.at);
      setNow(Date.now());
    };
    if (!flight) {
      rearm();
      return;
    }
    setLeaving(true);
    exitTimer.current = window.setTimeout(rearm, flight);
  };

  return (
    <>
      {onBreak && (
        <BreakScreen
          away={now - (dueAt as number)}
          leaving={leaving}
          flockRef={breakFlock}
          onSnooze={() => dismiss("snooze")}
          onResume={() => dismiss("resume")}
        />
      )}
      {/* No `flockOn` guard here on purpose: switching the flock off in
          Settings drops `idle`, and IdleFlock stays mounted just long enough to
          fly the birds home. A hard cut mid-air reads as a crash. */}
      {!onBreak && <IdleFlock idle={idle} />}
    </>
  );
}

interface ScreenProps {
  /** ms since the break came due — "how long you've been away". */
  away: number;
  leaving: boolean;
  flockRef: React.RefObject<FlockHandle | null>;
  onSnooze(): void;
  onResume(): void;
}

function BreakScreen({ away, leaving, flockRef, onSnooze, onResume }: ScreenProps) {
  const resumeRef = useRef<HTMLButtonElement | null>(null);
  // Latest handler behind a stable ref: the card re-renders every second to
  // tick its clock, and the Escape listener must not be re-registered for that.
  const resumeNow = useRef(onResume);
  useEffect(() => {
    resumeNow.current = onResume;
  });

  // Take the keyboard (a pane's terminal usually has it) so the card's own
  // buttons answer Enter/Space, and hand it back on the way out.
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    resumeRef.current?.focus();
    return () => {
      // The card outlives the click that dismissed it by the length of the
      // flight home, during which the app is already clickable — so only take
      // the keyboard back if nothing else has claimed it. By the time a passive
      // cleanup runs, focus lost with the removed button has fallen to <body>.
      const active = document.activeElement;
      if (active && active !== document.body && active !== document.documentElement) return;
      try {
        prev?.focus?.();
      } catch {
        /* the element went away with its pane */
      }
    };
  }, []);

  // Escape, bound where every other dialog in the app binds it: on the
  // document, in the capture phase. A React onKeyDown on the overlay only
  // fires while focus is inside it, and one click on the scrim (or on the
  // card's own text) drops focus to <body> — which killed Escape for the rest
  // of the break AND let the Escape listener of a dialog hidden behind the
  // scrim answer instead.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      resumeNow.current();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, []);

  return (
    <div
      id="break-screen"
      className={"modal break-screen" + (leaving ? " break-leaving" : "")}
      role="dialog"
      aria-modal="true"
      aria-labelledby="break-title"
    >
      <Flock
        className="break-flock"
        apiRef={flockRef}
        /* The densest of the two: this one is the event, not the ambience.
           `emergeFrom` is read once, at mount — which is the moment the card
           appears, so the mark is where it will be. */
        options={{
          areaPerBird: 11000,
          min: 55,
          max: 190,
          alpha: 1,
          emergeFrom: logoPoint(),
        }}
      />
      <div className="break-card">
        <h2 id="break-title" className="break-title">
          Time for a break
        </h2>
        <p className="break-body">
          Stand up, look at something further away than this screen. The flock keeps
          flying without you — nothing you have running needs you for the next few
          minutes.
        </p>
        <p className="break-clock">Away {fmtElapsed(away)}</p>
        <div className="break-actions">
          <button type="button" className="break-btn" disabled={leaving} onClick={onSnooze}>
            Snooze 5 min
          </button>
          <button
            type="button"
            className="break-btn primary"
            ref={resumeRef}
            disabled={leaving}
            onClick={onResume}
          >
            Resumed work
          </button>
        </div>
      </div>
    </div>
  );
}

/** The idle overlay, kept mounted for the length of its flight home so the
 * birds aren't cut off mid-air the instant somebody touches the mouse. */
function IdleFlock({ idle }: { idle: boolean }) {
  const [shown, setShown] = useState(false);
  const flock = useRef<FlockHandle | null>(null);

  useEffect(() => {
    if (idle) {
      setShown(true);
      return;
    }
    if (!shown) return;
    const home = logoPoint();
    const flight = flock.current?.gather(home.x, home.y) ?? 0;
    if (!flight) {
      setShown(false);
      return;
    }
    const t = window.setTimeout(() => setShown(false), flight);
    return () => window.clearTimeout(t);
  }, [idle, shown]);

  if (!shown) return null;
  return (
    <Flock
      id="idle-flock"
      className="idle-flock"
      apiRef={flock}
      /* A shade sparser than the break screen's — this one flies over work
         somebody may walk back to mid-thought — but still a flock, not a
         handful of dots. */
      options={{
        areaPerBird: 14000,
        min: 45,
        max: 150,
        alpha: 0.95,
        emergeFrom: logoPoint(),
      }}
    />
  );
}
