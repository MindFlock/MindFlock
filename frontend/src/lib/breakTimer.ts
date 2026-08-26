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

export function saveArm(arm: BreakArm | null) {
  try {
    if (arm) localStorage.setItem(BREAK_ARM_KEY, JSON.stringify(arm));
    else localStorage.removeItem(BREAK_ARM_KEY);
  } catch {
    /* storage unavailable — the clock just restarts on the next load */
  }
}
