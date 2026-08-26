/** Take-a-break clock — the pure half, so the arming rules can be tested
 * without a DOM or a wall clock.
 *
 * The whole model is one persisted record: WHEN the next break is due, and the
 * interval it was armed for. Storing the interval alongside the deadline is
 * what lets a reload tell two situations apart that otherwise look identical:
 *
 *   - the page reloaded mid-countdown → keep the deadline, or a refresh (or a
 *     server restart, which reloads every tab) becomes a way to dodge breaks
 *     forever;
 *   - the user changed the interval in Settings → re-arm from now, because a
 *     deadline measured against the old interval means nothing.
 *
 * THE CLOCK ONLY RUNS WHILE THE APP DOES. A deadline is wall-clock, so on its
 * own it keeps counting through a shut-down machine, a closed window and a
 * slept laptop — and whoever comes back in the morning is met by a card that
 * claims they have been at the desk for nine hours. They have not: time away
 * from the app is time away from the desk, which is the break itself. So the
 * app records that it is running (:data:`BREAK_SEEN_KEY`, a heartbeat), and a
 * gap in that record re-arms the interval from now instead of resuming it.
 *
 * That is deliberately NOT the same as a refresh, which the rules above still
 * refuse to let you dodge a break with: a reload lands seconds after the last
 * heartbeat, a reopened app lands minutes or hours after it.
 */

export const BREAK_MIN_MINUTES = 5;
export const BREAK_MAX_MINUTES = 480;
export const BREAK_DEFAULT_MINUTES = 60;

/** Snooze pushes the break back by five minutes — the user's "kick it back". */
export const SNOOZE_MS = 5 * 60_000;

/** No pointer, key, wheel or touch anywhere for this long and the flock takes
 * the screen. Ten minutes by default: long enough that it means you actually
 * walked away, short enough that a coffee brings you back to birds. */
export const IDLE_MIN_MINUTES = 1;
export const IDLE_MAX_MINUTES = 480;
export const IDLE_DEFAULT_MINUTES = 10;

export const BREAK_ARM_KEY = "mf_break_due";
/** Last time the app was known to be running — the heartbeat the away-rule
 * reads. Its own key rather than a field on the arm: it is written every few
 * seconds and the arm is not, and a stored deadline must stay readable even if
 * this one is missing (an older build, cleared storage, a first run). */
export const BREAK_SEEN_KEY = "mf_break_seen";

/** How long the heartbeat can go silent before the app is assumed to have
 * stopped counting. Comfortably longer than a hidden tab's throttled timer
 * (browsers slow a background interval to about one tick a minute, which must
 * not read as "away") and comfortably shorter than any real absence. */
export const AWAY_GAP_MS = 5 * 60_000;

export interface BreakArm {
  /** Epoch ms at which the break screen is due. */
  at: number;
  /** The interval (minutes) this deadline was armed for. */
  every: number;
}

/** Settings accepts anything an <input type=number> can produce. Bring it back
 * to a whole number of minutes inside the supported range.
 *
 * "Nothing" and "not a number" both mean the DEFAULT, not the floor: `Number()`
 * reads an empty field and a missing localStorage value as 0, and silently
 * turning a cleared field into a five-minute reminder is not a clamp, it's a
 * different setting. */
export function clampBreakMinutes(value: unknown): number {
  if (value === null || value === undefined) return BREAK_DEFAULT_MINUTES;
  if (typeof value === "string" && value.trim() === "") return BREAK_DEFAULT_MINUTES;
  const n = Math.round(Number(value));
  if (!isFinite(n)) return BREAK_DEFAULT_MINUTES;
  return Math.min(BREAK_MAX_MINUTES, Math.max(BREAK_MIN_MINUTES, n));
}

/** Same rules as {@link clampBreakMinutes}, for the idle flock's own delay:
 * empty or unreadable means the DEFAULT, not the floor. Its floor is a single
 * minute — someone who wants birds the moment they step away should get them,
 * and unlike a break reminder the flock costs nothing to be wrong about. */
export function clampIdleMinutes(value: unknown): number {
  if (value === null || value === undefined) return IDLE_DEFAULT_MINUTES;
  if (typeof value === "string" && value.trim() === "") return IDLE_DEFAULT_MINUTES;
  const n = Math.round(Number(value));
  if (!isFinite(n)) return IDLE_DEFAULT_MINUTES;
  return Math.min(IDLE_MAX_MINUTES, Math.max(IDLE_MIN_MINUTES, n));
}

/** A fresh countdown: one full interval from now. */
export function armBreak(now: number, everyMin: number): BreakArm {
  const every = clampBreakMinutes(everyMin);
  return { at: now + every * 60_000, every };
}

/** Snoozing keeps the armed interval — only the deadline moves. */
export function snoozeArm(now: number, everyMin: number): BreakArm {
  return { at: now + SNOOZE_MS, every: clampBreakMinutes(everyMin) };
}

/** What the clock should read at mount, or after the interval changed.
 *
 * `saved` is kept only when it belongs to the interval in force AND is not
 * further away than a whole interval — the second test throws out a deadline
 * left behind by a longer setting, and a clock skewed forward and back. An
 * already-overdue deadline is kept on purpose: the break you were owed when
 * you closed the tab is still owed when you open it. */
export function nextArm(now: number, everyMin: number, saved: BreakArm | null): BreakArm {
  const every = clampBreakMinutes(everyMin);
  if (
    saved &&
    saved.every === every &&
    typeof saved.at === "number" &&
    isFinite(saved.at) &&
    saved.at <= now + every * 60_000
  )
    return { at: saved.at, every };
  return armBreak(now, every);
}

/** Was the app not running when it should have been counting?
 *
 * True for a heartbeat that is missing (nothing has ever run, or storage was
 * cleared), older than {@link AWAY_GAP_MS}, or in the FUTURE — a clock that
 * jumped backwards leaves a stamp no elapsed time can explain, and re-arming is
 * the safe answer to "I cannot tell how long that was".
 */
export function wasAway(now: number, seen: number | null | undefined): boolean {
  if (seen === null || seen === undefined || !isFinite(seen)) return true;
  const gap = now - seen;
  return gap < 0 || gap > AWAY_GAP_MS;
}

/** The clock a freshly loaded page should start with.
 *
 * One rule table, because the three ways a page can arrive want three different
 * answers and only the middle one is obvious:
 *
 *   - **The app opened** (the shell launched, or a new tab): a fresh interval.
 *     Opening MindFlock is the moment the countdown starts — you have just sat
 *     down, whatever the deadline left behind by yesterday says.
 *   - **The page reloaded after the app had stopped** (closed for an hour, or a
 *     slept machine woken and refreshed): also fresh, for the same reason. The
 *     heartbeat is what tells these apart from…
 *   - **…a reload mid-countdown** — keep the deadline. This is the case the
 *     stored arm exists for: a refresh, a server restart or a crash must not
 *     buy anyone a new hour ({@link nextArm} owns the rest of that rule).
 */
export function openArm(
  now: number,
  everyMin: number,
  saved: BreakArm | null,
  seen: number | null | undefined,
  reloaded: boolean
): BreakArm {
  const every = clampBreakMinutes(everyMin);
  if (!reloaded) return armBreak(now, every);
  if (wasAway(now, seen)) return armBreak(now, every);
  return nextArm(now, every, saved);
}

/** Did this page arrive by refresh rather than by the app opening?
 *
 * The Navigation Timing entry answers it directly: `reload` covers F5, Ctrl+R,
 * the palette's Reload, `location.reload()` from the API client on a lost
 * session, and the reload every tab does when the server restarts — every path
 * the anti-dodge rule was written for. A shell launch, a new tab and a
 * `loadURL()` are `navigate`, which is the app opening.
 *
 * Unreadable (an old browser, a stripped Performance API) answers `true`: the
 * conservative half is to keep the existing deadline, since the worst outcome
 * is being asked to take a break slightly early.
 */
export function wasReload(): boolean {
  try {
    const nav = performance.getEntriesByType("navigation")[0] as
      | PerformanceNavigationTiming
      | undefined;
    if (!nav || typeof nav.type !== "string") return true;
    return nav.type === "reload";
  } catch {
    return true;
  }
}

/** m:ss for the "you've been away N" line on the break screen. */
export function fmtElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m + ":" + String(s).padStart(2, "0");
}

export function loadArm(): BreakArm | null {
  try {
    const raw = localStorage.getItem(BREAK_ARM_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw) as BreakArm;
    if (!v || typeof v.at !== "number" || typeof v.every !== "number") return null;
    return v;
  } catch {
    return null;
  }
}

export function loadSeen(): number | null {
  try {
    const raw = localStorage.getItem(BREAK_SEEN_KEY);
    if (!raw) return null;
    const n = Number(raw);
    return isFinite(n) ? n : null;
  } catch {
    return null;
  }
}

export function saveSeen(now: number) {
  try {
    localStorage.setItem(BREAK_SEEN_KEY, String(now));
  } catch {
    /* storage unavailable — every load then reads as "away", which re-arms:
       the same answer this whole rule gives when it cannot tell. */
  }
}

export function saveArm(arm: BreakArm | null) {
  try {
    if (arm) localStorage.setItem(BREAK_ARM_KEY, JSON.stringify(arm));
    else localStorage.removeItem(BREAK_ARM_KEY);
  } catch {
    /* storage unavailable — the clock just restarts on the next load */
  }
}
