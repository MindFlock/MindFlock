/** Pure helpers for Settings → Site traffic.
 *
 * The visitor sections of /api/traffic are optional — they are absent, rather
 * than zero, against a click Worker deployed before visitor attribution
 * existed. Deciding what the screen may show is therefore a real rule with a
 * wrong answer, so it lives here where it can be tested rather than inline in
 * the component. */

import type { TrafficResponse } from "../api/types";

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
