/** The Sky's weather: what the plan window and the clock say, painted behind
 * the flock (SkyFlock.tsx) and named in one word in the Sky's title bar.
 *
 *  - clouds: the plan window's usage — one bank past 60 %, a thicker one past
 *    85 %;
 *  - night: the local hour falls in the notification quiet hours (a window
 *    that may wrap midnight, 22 → 7);
 *  - the word: "usage 87 %" while there are clouds, "quiet hours" at night,
 *    otherwise nothing.
 *
 * Everything here is pure: `weatherFor` decides, `windowUsagePct` and
 * `quietHours` read the server's payloads, and the clouds (`seedClouds`,
 * `fitClouds`, `stepClouds`) are a tiny simulation the tests drive without a
 * DOM. Nothing here ever moves a box. */

export interface QuietHours {
  /** Local hours, 0–24 (fractions are minutes: 22.5 = 22:30). */
  from: number;
  to: number;
}

export interface Weather {
  clouds: 0 | 1 | 2;
  night: boolean;
  /** The title bar's word: "usage 87 %", "quiet hours" or "". */
  word: string;
  /** Whether the clouds drift (never under `prefers-reduced-motion`). */
  drift: boolean;
}

export const CLEAR: Weather = { clouds: 0, night: false, word: "", drift: false };

/** Clouds drift in past this much of the plan window… */
export const CLOUDS_AT = 60;
/** …and thicken past this. */
export const STORM_AT = 85;

/** Whether `hour` falls in [from, to), wrapping midnight when from > to. An
 * empty window (from === to) is never quiet. */
export function inQuietHours(hour: number, quiet: QuietHours | null | undefined): boolean {
  if (!quiet) return false;
  const { from, to } = quiet;
  if (!Number.isFinite(from) || !Number.isFinite(to) || from === to) return false;
  const h = ((hour % 24) + 24) % 24;
  return from < to ? h >= from && h < to : h >= from || h < to;
}

export function weatherFor({
  usagePct,
  hour,
  quiet,
  reducedMotion = false,
}: {
  usagePct: number | null | undefined;
  hour: number;
  quiet: QuietHours | null | undefined;
  reducedMotion?: boolean;
}): Weather {
  const pct = typeof usagePct === "number" && Number.isFinite(usagePct) ? usagePct : null;
  const clouds: Weather["clouds"] = pct == null ? 0 : pct > STORM_AT ? 2 : pct > CLOUDS_AT ? 1 : 0;
  const night = inQuietHours(hour, quiet);
  const word = clouds ? `usage ${Math.round(pct as number)} %` : night ? "quiet hours" : "";
  return { clouds, night, word, drift: clouds > 0 && !reducedMotion };
}

// --- Reading the server -------------------------------------------------------- //

interface WindowLike {
  end?: number | null;
  percent_used?: number | null;
}
interface ProviderLike {
  mode?: string | null;
  window?: WindowLike | null;
}

/** The fullest live plan window in an /api/usage payload (the top bar's
 * estimate), or null when no provider is on a plan with a window open. The
 * fullest, not the focused one: the Sky has no focused pane, and the weather
 * is about whatever is about to run out. */
export function windowUsagePct(usage: unknown): number | null {
  const provs = (usage as { providers?: unknown } | null | undefined)?.providers;
  if (!Array.isArray(provs)) return null;
  let best: number | null = null;
  for (const p of provs as ProviderLike[]) {
    const w = p?.mode === "windowed" ? p.window : null;
    if (!w || !w.end || typeof w.percent_used !== "number" || !Number.isFinite(w.percent_used)) continue;
    if (best == null || w.percent_used > best) best = w.percent_used;
  }
  return best;
}

function hourOf(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) && v >= 0 && v <= 24 ? v : null;
  if (typeof v !== "string") return null;
  const m = /^\s*(\d{1,2})(?::(\d{2}))?\s*$/.exec(v);
  if (!m) return null;
  const h = Number(m[1]) + (m[2] ? Number(m[2]) / 60 : 0);
  return h <= 24 && (!m[2] || Number(m[2]) < 60) ? h : null;
}

/** The notification quiet hours from the settings document —
 * `notifications.quiet_hours: {from, to}`, each an hour (22) or "HH:MM"
 * ("22:30") — or null when unset or unreadable. */
export function quietHours(settings: unknown): QuietHours | null {
  const q = (settings as { notifications?: { quiet_hours?: unknown } } | null | undefined)?.notifications?.quiet_hours;
  if (!q || typeof q !== "object") return null;
  const from = hourOf((q as { from?: unknown }).from);
  const to = hourOf((q as { to?: unknown }).to);
  return from == null || to == null || from === to ? null : { from, to };
}

/** The local hour as a fraction (14:30 = 14.5). */
export function localHour(now: number | Date = Date.now()): number {
  const d = now instanceof Date ? now : new Date(now);
  return d.getHours() + d.getMinutes() / 60;
}

// --- The clouds ---------------------------------------------------------------- //

export interface Puff {
  dx: number;
  dy: number;
  r: number;
}

export interface Cloud {
  x: number;
  y: number;
  /** Pixels per frame, always rightwards: weather comes from the west. */
  v: number;
  /** Half its width — how far off an edge it must be to be out of sight. */
  span: number;
  puffs: Puff[];
  /** The weather cleared: it drifts off the right edge and is gone. */
  leaving?: boolean;
}

/** How many clouds a bank of this weather has. */
export function cloudCount(clouds: Weather["clouds"]): number {
  return clouds === 2 ? 6 : clouds === 1 ? 3 : 0;
}

/** `n` clouds over a sky `w`×`h`: scattered across it, or — `enter` — waiting
 * just past its left edge, so they drift in rather than appear. */
export function seedClouds(n: number, w: number, h: number, enter = false, rand: () => number = Math.random): Cloud[] {
  const out: Cloud[] = [];
  const base = Math.max(40, Math.min(w, h) * 0.12);
  for (let i = 0; i < n; i++) {
    const r = base * (0.8 + rand() * 0.6);
    const puffs: Puff[] = [];
    const k = 3 + Math.floor(rand() * 3);
    for (let j = 0; j < k; j++) {
      puffs.push({ dx: (j - (k - 1) / 2) * r * 0.7 + (rand() - 0.5) * r * 0.3, dy: (rand() - 0.5) * r * 0.5, r: r * (0.6 + rand() * 0.5) });
    }
    const span = Math.max(...puffs.map((p) => Math.abs(p.dx) + p.r));
    out.push({
      x: enter ? -span - rand() * w * 0.4 : rand() * w,
      y: h * (0.08 + rand() * 0.84),
      v: 0.08 + rand() * 0.08,
      span,
      puffs,
    });
  }
  return out;
}

/** The bank resized to `n` clouds: the ones kept stay put, new ones `enter`
 * from the left, and surplus ones leave by drifting off the right — or, when
 * nothing drifts (`enter` false), simply go. */
export function fitClouds(clouds: Cloud[], n: number, w: number, h: number, enter = true, rand: () => number = Math.random): Cloud[] {
  const staying = clouds.filter((c) => !c.leaving);
  if (n >= staying.length) {
    const fresh = n > staying.length ? seedClouds(n - staying.length, w, h, enter, rand) : [];
    return (enter ? clouds : staying).concat(fresh);
  }
  if (!enter) return staying.slice(0, n);
  staying.slice(n).forEach((c) => (c.leaving = true));
  return clouds;
}

/** One frame of drift: rightwards; a cloud gone past the right edge comes
 * round again from the left, unless it is leaving. Returns the bank. */
export function stepClouds(clouds: Cloud[], w: number): Cloud[] {
  for (const c of clouds) {
    c.x += c.v;
    if (c.x - c.span > w && !c.leaving) c.x = -c.span;
  }
  return clouds.some((c) => c.leaving && c.x - c.span > w) ? clouds.filter((c) => !(c.leaving && c.x - c.span > w)) : clouds;
}
