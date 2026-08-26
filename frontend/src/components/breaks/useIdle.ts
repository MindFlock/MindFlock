/** "Nobody has USED this window" — the signal the idle flock rides on.
 *
 * Deliberate input into MindFlock, and nothing else: a click, a keystroke, a
 * tap, a scroll. Three kinds of activity are deliberately not activity:
 *
 * - **Moving the mouse.** A pointer drifting across the window, or resting on
 *   it while you read something else, is not you working — and it is the one
 *   signal that fires constantly without anyone meaning anything by it. The
 *   flock is supposed to survive a hovering cursor.
 * - **An agent streaming output.** That is the machine working, not you. The
 *   flock is meant to appear over a grid that is busy while the room is empty.
 * - **Anything you do in another window.** Typing in your editor, clicking
 *   around a browser, sitting in a meeting — none of it reaches these
 *   listeners, and none of it may reset the clock. MindFlock idles behind
 *   you, on the other monitor, which is precisely when you want to look over
 *   and see birds. That is why neither `focus` nor `visibilitychange` is
 *   treated as input: the window merely coming back to the front is not you
 *   touching it. The click or keystroke that brings you back lands here on its
 *   own, and that is what wakes it.
 *
 * `wheel` is in the list on purpose, even though it is neither a click nor a
 * keystroke: reading a long diff by scrolling is using the app, and summoning
 * birds over what someone is reading would be the wrong answer.
 */

import { useEffect, useState } from "react";

/** Activity re-arms the timer at most this often — key repeat can fire tens of
 * times a second and the countdown is minutes long, so a second of slop is
 * free. */
const REARM_THROTTLE_MS = 1000;

const ACTIVITY = ["pointerdown", "keydown", "touchstart", "wheel"];

export function useIdle(ms: number, enabled: boolean): boolean {
  const [idle, setIdle] = useState(false);

  useEffect(() => {
    if (!enabled) {
      setIdle(false);
      return;
    }
    let timer = 0;
    let armedAt = 0;
    // Mirrored inside the effect rather than in a ref: the listeners close over
    // it directly, so there is no render-phase write and nothing to go stale.
    let isIdle = false;
    const arm = () => {
      armedAt = Date.now();
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        isIdle = true;
        setIdle(true);
      }, ms);
    };
    const wake = () => {
      // Waking from idle must be instant; re-arming a live countdown can wait,
      // which is what keeps a mousemove storm from re-arming 100 times a second.
      if (!isIdle && Date.now() - armedAt < REARM_THROTTLE_MS) return;
      isIdle = false;
      setIdle(false);
      arm();
    };
    // Capture phase: xterm panes and dialogs stop plenty of events from
    // bubbling, and a keystroke swallowed by a terminal is still a human.
    const opts = { capture: true, passive: true } as AddEventListenerOptions;
    for (const e of ACTIVITY) window.addEventListener(e, wake, opts);
    arm();
    return () => {
      window.clearTimeout(timer);
      for (const e of ACTIVITY) window.removeEventListener(e, wake, opts);
    };
  }, [ms, enabled]);

  return idle;
}
