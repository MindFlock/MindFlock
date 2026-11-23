/** Pure helpers for Settings → Site traffic.
 *
 * The visitor sections of /api/traffic are optional — they are absent, rather
 * than zero, against a click Worker deployed before visitor attribution
 * existed. Deciding what the screen may show is therefore a real rule with a
 * wrong answer, so it lives here where it can be tested rather than inline in
 * the component. */

import type {
  TrafficClickRow,
  TrafficClickTotals,
  TrafficDownloadFunnel,
  TrafficOsMatch,
  TrafficResponse,
  TrafficVisitorDay,
  TrafficVisitorSlug,
} from "../api/types";

export type TrafficMetric = "people" | "clicks";

/** Whether the payload can support the people-shaped views at all.
 *
 * `totals` is the marker: the Worker emits it alongside the other visitor
 * sections or emits none of them, and it is absent (not zero) when visitor
 * attribution has not been deployed. */
export function hasVisitorData(data: TrafficResponse | undefined | null): boolean {
  return !!data?.clicks?.totals;
}

/** The metric the chart may actually draw, given what the user asked for and
 * what the payload can support.
 *
 * Deriving this instead of trusting the raw state is what keeps the heading,
 * the toggle and the chart from disagreeing. `metric` initialises to "people",
 * so without this a payload carrying only clicks drew a clicks chart under a
 * "Visitors over time" heading, with People styled as the active tab.
 *
 * It also closes a one-way door. The toggle used to render People `disabled`
 * whenever visitor data was missing — but "people" is the INITIAL state, so
 * pressing Clicks moved you somewhere you could not come back from. A control
 * whose default value is the one it disables is a trap; the fix is that the
 * toggle is only offered when both options are real, and this function makes
 * the single-option case render correctly on its own. */
export function shownMetric(
  requested: TrafficMetric,
  data: TrafficResponse | undefined | null
): TrafficMetric {
  return hasVisitorData(data) ? requested : "clicks";
}

/** Y-axis ticks for a count axis: 0 up to a round ceiling at or above `rawMax`.
 *
 * Every quantity on this screen is a count of whole things — stars, clicks,
 * visitors, downloads — so the step is forced to a whole number. Without that,
 * a chart whose peak is 3 gets ticks at 0.75 / 1.5 / 2.25, labelling positions
 * no data point can occupy. That is the case the naive 1/2/5×10ⁿ ladder gets
 * wrong, since below a peak of ~10 the "nice" step it wants is fractional.
 *
 * The returned `max` is the axis top, NOT the data max: scaling bars against a
 * round ceiling is what makes the top gridline land on a labelled value. It is
 * always ≥ 1, so a chart with no data (or an all-zero window) still draws a
 * 0–1 axis rather than dividing by zero.
 *
 * `targetCount` is the number of intervals aimed for, not a guarantee — the
 * step is snapped to a round number first, so the result can come back with one
 * more or fewer gap than asked for. */
export function niceTicks(rawMax: number, targetCount = 4): { max: number; ticks: number[] } {
  const hi = Number.isFinite(rawMax) && rawMax > 0 ? Math.ceil(rawMax) : 1;
  const intervals = Math.max(1, Math.round(targetCount));
  const rough = hi / intervals;
  const pow = Math.pow(10, Math.floor(Math.log10(rough)));
  // 2.5 earns its place at 10ⁿ ≥ 10 (250, 2,500 — familiar tick values) but is
  // dropped below that, where it would be a fractional step.
  const step =
    [1, 2, 2.5, 5, 10]
      .map((m) => m * pow)
      .filter((s) => Number.isInteger(s) && s >= 1)
      .find((s) => s >= rough) ?? Math.max(1, Math.ceil(rough));
  const max = Math.ceil(hi / step) * step;
  const ticks: number[] = [];
  for (let v = 0; v <= max; v += step) ticks.push(v);
  return { max, ticks };
}

/** Where the click Worker is with human/automated classification:
 *
 * - "old-worker": it predates classification (`clicks.classification` absent),
 *   so every click figure includes automated traffic and must say so.
 * - "pending": classification is deployed but has not seen a click in this
 *   window yet.
 * - "partial": classified clicks exist, but part of the window predates
 *   classification — human figures cover `since` onwards only.
 * - "full": the whole window is classified. */
export type ClassificationState = "old-worker" | "pending" | "partial" | "full";

/** The click figures the screen should lead with, and how to label them.
 *
 * Against a classifying Worker these are the HUMAN-only sections, with the
 * automated and unverified shares beside them; against an older Worker they
 * are the unfiltered sections, and `scope: "all"` tells the screen to say so.
 * Deciding this once here keeps the tiles, the funnel, the chart and the
 * by-link table from mixing scopes — a human visitor count next to an
 * unfiltered click count would be a ratio of two different populations. */
export interface ClickView {
  state: ClassificationState;
  scope: "human" | "all";
  /** First classified UTC day, when `scope` is "human" and there is one. */
  since: string | null;
  series: TrafficClickRow[];
  visitors_by_day: TrafficVisitorDay[];
  visitors_by_slug: TrafficVisitorSlug[];
  totals: TrafficClickTotals | null;
  downloads: TrafficDownloadFunnel | null;
  /** Classified clicks (all three classes) and each class's share of them —
   * the same period as the human figures, so the shares are honest. Null
   * against an old Worker. */
  classified: {
    clicks: number;
    human: { clicks: number; visitors: number };
    unverified: { clicks: number; visitors: number };
    automated: { clicks: number; visitors: number };
  } | null;
}

export function clickView(data: TrafficResponse | undefined | null): ClickView | null {
  const clicks = data?.clicks;
  if (!clicks) return null;
  const c = clicks.classification;
  const h = clicks.human;
  if (!c || !h) {
    return {
      state: "old-worker",
      scope: "all",
      since: null,
      series: clicks.series ?? [],
      visitors_by_day: clicks.visitors_by_day ?? [],
      visitors_by_slug: clicks.visitors_by_slug ?? [],
      totals: clicks.totals ?? null,
      downloads: clicks.downloads ?? null,
      classified: null,
    };
  }
  const zero = { clicks: 0, visitors: 0 };
  const human = c.by_class?.human ?? zero;
  const unverified = c.by_class?.unverified ?? zero;
  const automated = c.by_class?.automated ?? zero;
  const state: ClassificationState = !c.since
    ? "pending"
    : c.unclassified_clicks > 0
      ? "partial"
      : "full";
  return {
    state,
    scope: "human",
    since: c.since ?? null,
    series: h.series ?? [],
    visitors_by_day: h.visitors_by_day ?? [],
    visitors_by_slug: h.visitors_by_slug ?? [],
    totals: h.totals ?? null,
    downloads: h.downloads ?? null,
    classified: {
      clicks: human.clicks + unverified.clicks + automated.clicks,
      human,
      unverified,
      automated,
    },
  };
}

/** The slugs whose destination is a desktop installer, mapped to the UA OS
 * that installer runs on (the Worker's `osFromUA` names). */
export const INSTALLER_SLUG_OS: Record<string, string> = { mac: "mac", windows: "windows", linux: "linux" };

/** Download-button clicks, and how many came from a UA on the installer's own
 * OS. Uses the Worker's `downloads.os_match` when it sends one, else folds the
 * click series — clicks are plain tallies, so unlike visitor counts they may
 * be summed, which is what lets this work against an older Worker too.
 *
 * A mismatch is not proof of a bot (people fetch an installer for another
 * machine), but a crawler clicking all three installers from one UA can match
 * at most one in three, and a phone fetching a desktop installer is rarely
 * about to install it. */
export function osMatchedDownloads(
  series: TrafficClickRow[],
  fromWorker?: TrafficOsMatch | null
): TrafficOsMatch {
  if (fromWorker && typeof fromWorker.clicks === "number") return fromWorker;
  const by = new Map(
    Object.keys(INSTALLER_SLUG_OS).map((slug) => [slug, { slug, clicks: 0, matched: 0 }])
  );
  for (const r of series) {
    const b = by.get(r.slug);
    if (!b) continue;
    b.clicks += r.clicks;
    if (r.os === INSTALLER_SLUG_OS[r.slug]) b.matched += r.clicks;
  }
  const by_slug = Array.from(by.values());
  return {
    clicks: by_slug.reduce((n, b) => n + b.clicks, 0),
    matched: by_slug.reduce((n, b) => n + b.matched, 0),
    by_slug,
  };
}

/** Release-asset downloads split into the installers and the rest, by kind.
 *
 * GitHub's counters carry no identity, so none of this is people: the
 * installer figure still includes Windows and Linux auto-updates (the updater
 * fetches the same .exe and .AppImage) and anything that crawls release
 * assets. It is only the most honest of GitHub's numbers, not a real-downloads
 * number. `installers` is null against a backend from before the split. */
export function releaseDownloads(data: TrafficResponse | undefined | null): {
  installers: number | null;
  other: number | null;
  byKind: Record<string, number> | null;
} {
  const installers = data?.installer_downloads_total;
  if (typeof installers !== "number") return { installers: null, other: null, byKind: null };
  return {
    installers,
    other: Math.max(0, (data?.downloads_total ?? 0) - installers),
    byKind: data?.downloads_by_kind ?? null,
  };
}

/** `part` as a whole-number percent of `whole`, or null when there is no
 * whole to be a share of. Rounded, except that a non-zero share never shows
 * as 0% and a share short of everything never shows as 100%. */
export function sharePct(part: number, whole: number): number | null {
  if (!whole || !Number.isFinite(part) || !Number.isFinite(whole)) return null;
  const pct = Math.round((part / whole) * 100);
  if (part > 0 && pct === 0) return 1;
  if (part < whole && pct === 100) return 99;
  return pct;
}
