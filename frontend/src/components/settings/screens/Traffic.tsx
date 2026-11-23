/** Settings → Site traffic (dev shell only): GitHub stars/forks, per-release
 * download counts, and click/visitor totals for the mindflock.ai/go/ tracked
 * links (macOS/Windows/Linux buttons, GitHub, Product Hunt, NxGn — see the
 * Worker in the webpage repo's worker/ directory). This is the maintainer's own
 * reach dashboard, not something an end user's build needs, hence the dev-shell
 * gate in SettingsDialog.tsx via isDevShell().
 *
 * The screen leads with FIRST-TIME visitors, because "is anyone new showing up"
 * is the question it exists to answer. Two things about that are worth knowing
 * before reading any number here:
 *
 * - Unique counts are not additive. The Worker counts each grain separately and
 *   this screen never sums one grain to make another — see the note on
 *   TrafficVisitorDay in api/types.ts.
 * - GitHub's download counters carry NO identity. They cannot be split into new
 *   versus updating users by any means, so they are labelled as the raw tallies
 *   they are, and the new-user funnel is built from download-button clicks
 *   (which the Worker can attribute) instead. Most release assets are not
 *   installers at all (update manifests, checksums, packages), so the Downloads
 *   tile counts the three installers and shows the rest as what it is.
 * - Most raw clicks are automated. A Worker that classifies clicks (human /
 *   unverified / automated) sends human-only copies of every click section;
 *   the screen leads with those, shows the automated share beside them, and
 *   labels them "since <date>" while the window is only partly classified.
 *   Against an older Worker it shows the unfiltered figures and says so.
 *   lib/traffic.ts `clickView` makes that choice once for the whole screen.
 */

import { useMemo, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import { refreshTraffic, useTraffic } from "../../../state/queries";
import type {
  TrafficClassification,
  TrafficClickRow,
  TrafficDownloadFunnel,
  TrafficOsMatch,
  TrafficRelease,
  TrafficStarPoint,
  TrafficVisitorDay,
} from "../../../api/types";
import {
  clickView,
  niceTicks,
  osMatchedDownloads,
  releaseDownloads,
  sharePct,
  shownMetric,
  type ClickView,
  type TrafficMetric,
} from "../../../lib/traffic";
import type { ScreenProps } from "../SettingsDialog";

const DAYS_OPTIONS = [30, 90] as const;

const SLUG_LABEL: Record<string, string> = {
  mac: "macOS download",
  windows: "Windows download",
  linux: "Linux download",
  github: "GitHub",
  releases: "Releases page",
  latest: "Latest release",
  producthunt: "Product Hunt",
  nxgn: "NxGn Tools",
  viberank: "VibeRank",
  launchbuff: "LaunchBuff",
};

/** The Worker's reason codes (worker/README.md "Human or automated"), in the
 * words the evidence table uses. */
const TRAFFIC_REASON_LABEL: Record<string, string> = {
  navigation: "browser link click",
  "bot-ua": "declared bot or crawler",
  prefetch: "prefetch / link preview",
  head: "HEAD request (link checker)",
  "automation-ua": "automation tool or HTTP library",
  "verified-bot": "Cloudflare verified bot",
  "bot-score": "Cloudflare bot score",
  "no-fetch-meta": "claims a modern browser, sends no browser headers",
  datacenter: "hosting / cloud network",
  "not-navigation": "not a page navigation",
  insecure: "plain HTTP",
  "old-browser": "old browser or in-app webview",
  direct: "typed, bookmarked or opened from an app",
  "no-user-activation": "no user gesture",
  accept: "not asking for a page",
  "no-accept-language": "no Accept-Language",
};

const TRAFFIC_CLASS_LABEL: Record<string, string> = {
  human: "Human",
  unverified: "Unverified",
  automated: "Automated",
};

/** The scope every click figure on the screen is counted over, in words. */
function scopeTag(view: ClickView, days: number): string {
  switch (view.state) {
    case "old-worker":
      return `${days}d, all clicks incl. automated`;
    case "pending":
      return "human clicks — none recorded yet";
    case "partial":
      return `human clicks since ${fmtDate(view.since)}`;
    default:
      return `human clicks, ${days}d`;
  }
}

function slugLabel(slug: string): string {
  return SLUG_LABEL[slug] || slug;
}

function fmtInt(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toLocaleString();
}

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  try {
    // A bare "YYYY-MM-DD" (the Worker's UTC day keys) parses as UTC midnight,
    // which west of Greenwich formats as the PREVIOUS day — so "since Nov 24"
    // read "since Nov 23". A calendar day is shown as that calendar day.
    const day = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
    const when = day ? new Date(Number(day[1]), Number(day[2]) - 1, Number(day[3])) : new Date(iso);
    return when.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  } catch {
    return iso;
  }
}

function fmtPct(part: number, whole: number): string {
  if (!whole) return "—";
  return ((part / whole) * 100).toFixed(1) + "%";
}

function plural(n: number, word: string): string {
  return `${fmtInt(n)} ${word}${n === 1 ? "" : "s"}`;
}

function fmtShare(part: number, whole: number): string {
  const pct = sharePct(part, whole);
  return pct == null ? "—" : `${pct}%`;
}

/** A release's installer downloads, falling back to every asset against a
 * backend from before the installer split. */
function releaseCount(r: TrafficRelease): number {
  return r.installer_downloads ?? r.total_downloads;
}

/* --- Shared chart geometry -------------------------------------------------
 * All four charts here are the same box, so the box is defined once. The user
 * unit is the SVG unit; the charts stretch horizontally (preserveAspectRatio
 * none) but their CSS height matches CHART_H, so one vertical unit is one CSS
 * pixel — which is what lets the y-axis ticks be positioned as a percentage of
 * the same height and stay in register with the gridlines. */
const CHART_W = 640;
const CHART_H = 160;
const PAD_L = 4;
/** Room under the baseline. The date labels live in HTML below the SVG, so this
 * is just breathing space, not a text box. */
const PAD_B = 18;
/** Room above the topmost tick, so the highest gridline's label has somewhere
 * to sit without being cut off by the top of the plot. */
const PAD_T = 10;
const PLOT_H = CHART_H - PAD_B;

/** Pixels-per-unit mapping onto the plot, against a ROUND axis maximum rather
 * than the data's own peak — that is what puts the top gridline on a labelled
 * value instead of at whatever height the biggest bar happened to reach. */
function scaleFor(axisMax: number) {
  return (v: number) => (v / axisMax) * (PLOT_H - PAD_T);
}

/** Horizontal gridlines at each labelled tick.
 *
 * Hairline, solid, one step off the surface (see traffic.css) — recessive
 * enough to read the value off without competing with the marks. Zero is
 * skipped: the baseline is already drawn there, at full border weight.
 *
 * Rendered inside the SVG (the geometry has to stretch with the plot) while the
 * numbers themselves are HTML in YAxis below (text must NOT stretch). */
function Gridlines({ ticks, yAt }: { ticks: number[]; yAt: (v: number) => number }) {
  return (
    <g aria-hidden="true">
      {ticks
        .filter((t) => t > 0)
        .map((t) => (
          <line key={t} x1={0} y1={yAt(t)} x2={CHART_W} y2={yAt(t)} className="traffic-gridline" />
        ))}
    </g>
  );
}

/** The y-axis tick labels: HTML in a gutter beside the plot, not SVG <text>.
 *
 * The charts scale to their container with preserveAspectRatio="none", so
 * anything drawn inside the SVG is stretched horizontally by whatever the panel
 * width happens to be — fine for lines and rectangles, ruinous for glyphs. The
 * numbers therefore sit outside the SVG and are positioned as a percentage of
 * the plot's height, which the gutter shares with it.
 *
 * aria-hidden because each chart's role="img" label already states the range in
 * words; the visible ticks would otherwise read out as a row of bare numbers. */
function YAxis({ ticks, yAt }: { ticks: number[]; yAt: (v: number) => number }) {
  // Absolutely-positioned children contribute nothing to the gutter's width, so
  // it is sized from the widest label. `ch` is the width of a "0", and the ticks
  // are tabular, so digits are exact and separators come in slightly under.
  const width = Math.max(...ticks.map((t) => fmtInt(t).length));
  return (
    <div className="traffic-yaxis" style={{ width: `${width}ch` }} aria-hidden="true">
      {ticks.map((t) => (
        <span key={t} className="traffic-ytick" style={{ top: `${(yAt(t) / CHART_H) * 100}%` }}>
          {fmtInt(t)}
        </span>
      ))}
    </div>
  );
}

/** Corner radius for a bar of this width and height.
 *
 * Bounded by the HEIGHT as well as the width, which the width-only version
 * wasn't: at a 90-day range the bars are ~5px wide, so a radius of half the
 * width turned every short bar into a lozenge — a 4px-tall count rendered as
 * an oval reads as a blob rather than as a magnitude anchored to the baseline.
 * Dividing by 3 keeps the softening visible on tall bars and negligible on
 * short ones. */
function barRadius(width: number, height: number): number {
  return Math.max(0, Math.min(3, width / 2, height / 3));
}

/** Below this the bars are too narrow to outline: a 1px stroke on each side of
 * a ~5px bar is most of the bar, so the recessive segment fills in solid and
 * stops being distinguishable from the segment it's stacked against. */
const OUTLINE_MIN_BAR_W = 8;

/** Collapse the flat (day, slug) rows into one totals-per-day series for the
 * bar chart — a single magnitude over time, so one hue (--accent) is the
 * correct encoding; per-slug identity lives in the table below instead of a
 * categorical palette this design system doesn't otherwise have. */
function dailyTotals(series: TrafficClickRow[]): Array<{ day: string; total: number; bySlug: Record<string, number> }> {
  const byDay = new Map<string, { day: string; total: number; bySlug: Record<string, number> }>();
  for (const row of series) {
    let bucket = byDay.get(row.day);
    if (!bucket) {
      bucket = { day: row.day, total: 0, bySlug: {} };
      byDay.set(row.day, bucket);
    }
    bucket.total += row.clicks;
    bucket.bySlug[row.slug] = (bucket.bySlug[row.slug] || 0) + row.clicks;
  }
  return Array.from(byDay.values()).sort((a, b) => a.day.localeCompare(b.day));
}

/** Cumulative GitHub stars over time — the one line chart on this screen,
 * next to the bar charts for clicks and downloads. A running total is
 * naturally a line (it only goes up), whereas per-day clicks and per-release
 * downloads are discrete counts better read as bars — so the mark follows
 * the data's shape rather than matching the neighboring charts for
 * consistency's own sake. Tracks the pointer continuously (a crosshair)
 * instead of the bar charts' per-bar hover, since there's no discrete mark to
 * land on between points. */
function StarsChart({ points }: { points: TrafficStarPoint[] }) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  if (points.length === 0) {
    return (
      <p className="set-hint">
        No star history available — the stargazers API requires a GitHub token (Settings →
        Connections). The current total above still works without one.
      </p>
    );
  }
  if (points.length === 1) {
    return <p className="set-hint">Only one day of star history so far — check back once there's a trend.</p>;
  }

  const { max, ticks } = niceTicks(Math.max(...points.map((p) => p.stars)));
  const scale = scaleFor(max);
  const stepX = (CHART_W - PAD_L) / (points.length - 1);
  const xAt = (i: number) => PAD_L + i * stepX;
  const yAt = (v: number) => PLOT_H - scale(v);
  const pathD = points.map((p, i) => `${i === 0 ? "M" : "L"}${xAt(i).toFixed(2)},${yAt(p.stars).toFixed(2)}`).join(" ");

  const handleMove = (e: ReactMouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const relX = ((e.clientX - rect.left) / rect.width) * CHART_W;
    const idx = Math.max(0, Math.min(points.length - 1, Math.round((relX - PAD_L) / stepX)));
    setHoverIdx(idx);
  };

  const hovered = hoverIdx != null ? points[hoverIdx] : null;

  return (
    <div className="traffic-chart-wrap">
      <div className="traffic-plot">
        <YAxis ticks={ticks} yAt={yAt} />
        <svg
          className="traffic-chart"
          viewBox={`0 0 ${CHART_W} ${CHART_H}`}
          preserveAspectRatio="none"
          role="img"
          aria-label={`Cumulative GitHub stars, ${fmtDate(points[0].day)} through ${fmtDate(points[points.length - 1].day)}, on a vertical scale of 0 to ${fmtInt(max)} stars`}
          onMouseMove={handleMove}
          onMouseLeave={() => setHoverIdx(null)}
        >
          <Gridlines ticks={ticks} yAt={yAt} />
          <path d={pathD} className="traffic-line" fill="none" />
          {hoverIdx != null && (
            <>
              <line x1={xAt(hoverIdx)} y1={PAD_T} x2={xAt(hoverIdx)} y2={PLOT_H} className="traffic-crosshair" />
              <circle cx={xAt(hoverIdx)} cy={yAt(points[hoverIdx].stars)} r={4} className="traffic-line-dot" />
            </>
          )}
          <line x1={0} y1={PLOT_H} x2={CHART_W} y2={PLOT_H} className="traffic-baseline" />
        </svg>
        <div className="traffic-chart-axis">
          <span>{fmtDate(points[0].day)}</span>
          <span>{fmtDate(points[points.length - 1].day)}</span>
        </div>
      </div>
      <div className="traffic-tooltip" aria-live="polite">
        {hovered ? (
          <>
            <strong>{fmtDate(hovered.day)}</strong> — {fmtInt(hovered.stars)} star{hovered.stars === 1 ? "" : "s"}
          </>
        ) : (
          <span className="set-hint">Hover the line for the star count on that date.</span>
        )}
      </div>
    </div>
  );
}

/** Oldest-first releases with a known publish date — undated (draft) releases
 * have no time-axis position, so the chart drops them; the table below still
 * lists everything. */
function releasesChronological(releases: TrafficRelease[]): TrafficRelease[] {
  return releases
    .filter((r) => r.published_at)
    .slice()
    .sort((a, b) => (a.published_at! < b.published_at! ? -1 : a.published_at! > b.published_at! ? 1 : 0));
}

function ReleasesChart({ releases }: { releases: TrafficRelease[] }) {
  const chrono = useMemo(() => releasesChronological(releases), [releases]);
  const [hover, setHover] = useState<number | null>(null);

  if (!chrono.length) {
    return <p className="set-hint">No published releases yet.</p>;
  }

  const { max, ticks } = niceTicks(Math.max(...chrono.map(releaseCount)));
  const scale = scaleFor(max);
  const barGap = 3;
  const barW = Math.max(1, (CHART_W - PAD_L) / chrono.length - barGap);

  const hovered = hover != null ? chrono[hover] : null;

  return (
    <div className="traffic-chart-wrap">
      <div className="traffic-plot">
        <YAxis ticks={ticks} yAt={(v) => PLOT_H - scale(v)} />
        <svg
          className="traffic-chart"
          viewBox={`0 0 ${CHART_W} ${CHART_H}`}
          preserveAspectRatio="none"
          role="img"
          aria-label={`Installer downloads per release, ${chrono[0].tag} through ${chrono[chrono.length - 1].tag}, on a vertical scale of 0 to ${fmtInt(max)} downloads`}
          onMouseLeave={() => setHover(null)}
        >
          <Gridlines ticks={ticks} yAt={(v) => PLOT_H - scale(v)} />
          {chrono.map((r, i) => {
            const barH = Math.max(1, scale(releaseCount(r)));
            const x = PAD_L + i * (barW + barGap);
            const y = PLOT_H - barH;
            return (
              <rect
                key={r.tag}
                x={x}
                y={y}
                width={barW}
                height={barH}
                rx={barRadius(barW, barH)}
                className={"traffic-bar" + (hover === i ? " hover" : "")}
                onMouseEnter={() => setHover(i)}
              />
            );
          })}
          <line x1={0} y1={PLOT_H} x2={CHART_W} y2={PLOT_H} className="traffic-baseline" />
        </svg>
        <div className="traffic-chart-axis">
          <span>{fmtDate(chrono[0].published_at)}</span>
          <span>{fmtDate(chrono[chrono.length - 1].published_at)}</span>
        </div>
      </div>
      <div className="traffic-tooltip" aria-live="polite">
        {hovered ? (
          <>
            <strong>{hovered.tag}</strong> ({fmtDate(hovered.published_at)}) —{" "}
            {hovered.installer_downloads != null
              ? `${plural(hovered.installer_downloads, "installer download")} of ${fmtInt(hovered.total_downloads)} asset downloads`
              : plural(hovered.total_downloads, "download")}
            {hovered.assets.length > 0 && (
              <span className="traffic-tooltip-detail">
                {" "}
                (
                {hovered.assets
                  .filter((a) => a.downloads > 0)
                  .sort((a, b) => b.downloads - a.downloads)
                  .map((a) => `${a.name}: ${a.downloads}`)
                  .join(", ") || "no downloads yet"}
                )
              </span>
            )}
          </>
        ) : (
          <span className="set-hint">Hover a bar for that release's per-asset breakdown.</span>
        )}
      </div>
    </div>
  );
}

/** Visitors per day, split first-time vs returning.
 *
 * Stacked, and stacked with ONE hue at two weights rather than two colors. The
 * split is part-to-whole with a clear focus — first-timers are the thing being
 * watched, returning visitors are the context they sit in — not two co-equal
 * categories, so a categorical pair would overstate the second series. It also
 * has to survive Settings → Appearance repainting --accent to any hue, which a
 * hand-picked companion colour would not.
 *
 * The returning segment carries a full-strength outline because its fill alone
 * lands near 1.6:1 on the panel at every alpha that still reads as recessive —
 * the edge is what makes the mark perceivable, and it keeps the fill quiet. */
function VisitorsChart({ days }: { days: TrafficVisitorDay[] }) {
  const [hover, setHover] = useState<number | null>(null);

  if (!days.length) {
    return <p className="set-hint">No visitors recorded in this window yet.</p>;
  }

  const { max, ticks } = niceTicks(Math.max(...days.map((d) => d.visitors)));
  const barGap = 2;
  const barW = Math.max(1, (CHART_W - PAD_L) / days.length - barGap);
  // Segment spacer from the mark spec — omitted when a bar is too short to
  // give up 2px without the smaller segment vanishing entirely.
  const SEG_GAP = 2;

  const scale = scaleFor(max);
  const yAt = (v: number) => PLOT_H - scale(v);
  const hovered = hover != null ? days[hover] : null;

  return (
    <div className="traffic-chart-wrap">
      <div className="traffic-legend">
        <span className="traffic-legend-item">
          <span className="traffic-swatch new" aria-hidden="true" /> First-time
        </span>
        <span className="traffic-legend-item">
          <span className="traffic-swatch returning" aria-hidden="true" /> Returning
        </span>
      </div>
      <div className="traffic-plot">
        <YAxis ticks={ticks} yAt={yAt} />
        <svg
          className="traffic-chart"
          viewBox={`0 0 ${CHART_W} ${CHART_H}`}
          preserveAspectRatio="none"
          role="img"
          aria-label={`Daily visitors split into first-time and returning, ${days[0].day} through ${days[days.length - 1].day}, on a vertical scale of 0 to ${fmtInt(max)} visitors`}
          onMouseLeave={() => setHover(null)}
        >
          <Gridlines ticks={ticks} yAt={yAt} />
          {days.map((d, i) => {
            const x = PAD_L + i * (barW + barGap);
            const newH = scale(d.new_visitors);
            const restH = scale(d.visitors - d.new_visitors);
            const gap = newH > SEG_GAP && restH > SEG_GAP ? SEG_GAP : 0;
            const outlined = barW >= OUTLINE_MIN_BAR_W ? " outlined" : "";
            return (
              <g key={d.day} onMouseEnter={() => setHover(i)} className={hover === i ? "hover" : ""}>
                {/* An invisible full-height target so thin bars and empty days
                    are still hoverable — the hit area is the column, not the ink. */}
                <rect x={x} y={0} width={barW + barGap} height={PLOT_H} className="traffic-hit" />
                {restH > 0 && (
                  <rect
                    x={x}
                    y={PLOT_H - newH - gap - restH}
                    width={barW}
                    height={restH}
                    rx={barRadius(barW, restH)}
                    className={"traffic-seg-returning" + outlined + (hover === i ? " hover" : "")}
                  />
                )}
                {newH > 0 && (
                  <rect
                    x={x}
                    y={PLOT_H - newH}
                    width={barW}
                    height={newH}
                    rx={barRadius(barW, newH)}
                    className={"traffic-seg-new" + (hover === i ? " hover" : "")}
                  />
                )}
              </g>
            );
          })}
          <line x1={0} y1={PLOT_H} x2={CHART_W} y2={PLOT_H} className="traffic-baseline" />
        </svg>
        <div className="traffic-chart-axis">
          <span>{fmtDate(days[0].day)}</span>
          <span>{fmtDate(days[days.length - 1].day)}</span>
        </div>
      </div>
      <div className="traffic-tooltip" aria-live="polite">
        {hovered ? (
          <>
            <strong>{fmtDate(hovered.day)}</strong> — {plural(hovered.visitors, "visitor")}
            <span className="traffic-tooltip-detail">
              {" "}
              ({fmtInt(hovered.new_visitors)} first-time, {fmtInt(hovered.returning_visitors)}{" "}
              returning
              {hovered.unknown_visitors > 0 && `, ${fmtInt(hovered.unknown_visitors)} unattributed`})
            </span>
          </>
        ) : (
          <span className="set-hint">Hover a bar for that day's first-time / returning split.</span>
        )}
      </div>
    </div>
  );
}

/** How many first-time visitors went on to start a download.
 *
 * This is the screen's answer to "are the downloads new users?", and it is a
 * proxy — it counts people who clicked a platform button on mindflock.ai, not
 * installs. It misses anyone who downloaded straight from the GitHub repo page,
 * and it counts an intent that may never finish. Both are stated on-screen
 * rather than left for the reader to assume, because the honest version of this
 * number is more useful than a flattering one.
 *
 * Bars are drawn against the same denominator so the funnel's two rows are
 * directly comparable in length, which is the whole point of drawing it. */
function DownloadFunnel({
  funnel,
  scope,
  osMatch,
}: {
  funnel: TrafficDownloadFunnel;
  /** What the figures are counted over, e.g. "human clicks since Nov 25". */
  scope: string;
  osMatch: TrafficOsMatch;
}) {
  const { new_visitors: total, new_visitors_clicked: clicked } = funnel;
  const matchBySlug = new Map(osMatch.by_slug.map((m) => [m.slug, m]));

  if (!total) {
    return (
      <p className="set-hint">
        No first-time visitors recorded in this window yet — the funnel fills in as the tracked
        links get clicked.
      </p>
    );
  }

  const rows = [
    { label: "First-time visitors", value: total },
    { label: "…who started a download", value: clicked, rate: fmtPct(clicked, total) },
  ];

  // The funnel's axis runs 0 to a round number at or above the first-time count,
  // so the bars land against labelled ticks. That means the top bar no longer
  // fills its track — the leftover is the rounding headroom, not missing data,
  // and the two rows stay comparable because both are drawn to the same scale.
  // Five intervals rather than the charts' four: these bars are as wide as the
  // panel, so there is room for more ticks, and the extra one keeps the ceiling
  // closer to the count (45 rounds to 50 rather than to 60, which would leave a
  // quarter of the track looking like missing data).
  const { max: axisMax, ticks } = niceTicks(total, 5);

  return (
    <div className="traffic-funnel">
      {rows.map((r) => (
        <div className="traffic-funnel-row" key={r.label}>
          <span className="traffic-funnel-label">{r.label}</span>
          <span className="traffic-funnel-track">
            {ticks.slice(1, -1).map((t) => (
              <span
                key={t}
                className="traffic-funnel-grid"
                style={{ left: `${(t / axisMax) * 100}%` }}
              />
            ))}
            <span
              className="traffic-funnel-fill"
              style={{ width: `${(r.value / axisMax) * 100}%` }}
            />
          </span>
          <span className="traffic-funnel-num">{fmtInt(r.value)}</span>
          <span className="traffic-funnel-rate">{r.rate || ""}</span>
        </div>
      ))}

      {/* The tick scale, in the funnel row's own grid so the numbers sit under
          the track they measure. The end labels are pinned inside the track's
          edges rather than centred on their ticks, which would hang them over
          the label and count columns either side. */}
      <div className="traffic-funnel-axis" aria-hidden="true">
        <span />
        <span className="traffic-funnel-ticks">
          {ticks.map((t, i) => (
            <span
              key={t}
              className={
                "traffic-funnel-tick" +
                (i === 0 ? " first" : i === ticks.length - 1 ? " last" : "")
              }
              style={{ left: `${(t / axisMax) * 100}%` }}
            >
              {fmtInt(t)}
            </span>
          ))}
        </span>
      </div>

      <table className="traffic-table traffic-funnel-table">
        <thead>
          <tr>
            <th>Platform</th>
            <th className="num">First-time</th>
            <th className="num">All visitors</th>
            <th className="num">Clicks</th>
            <th className="num">From that OS</th>
          </tr>
        </thead>
        <tbody>
          {funnel.by_slug.map((s) => (
            <tr key={s.slug}>
              <td>{slugLabel(s.slug)}</td>
              <td className="num">{fmtInt(s.new_visitors)}</td>
              <td className="num">{fmtInt(s.visitors)}</td>
              <td className="num">{fmtInt(s.clicks)}</td>
              <td className="num">
                {fmtShare(matchBySlug.get(s.slug)?.matched ?? 0, matchBySlug.get(s.slug)?.clicks ?? 0)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <p className="set-hint">
        Download <em>intent</em>, not installs ({scope}): someone pressing a platform button on
        mindflock.ai. It misses anyone who downloaded straight from the GitHub repo page, and a
        click is not a finished install. Per-platform first-timers can overlap (one person pressing
        both macOS and Linux is in both rows), so they may add up to more than the{" "}
        {fmtInt(clicked)} above — that figure is the deduplicated one. "From that OS" is the share
        of that button's clicks whose browser runs the installer's own OS: people do fetch an
        installer for another machine, but a crawler pressing every button can't match more than
        one in three.
      </p>
    </div>
  );
}

function ClicksChart({ series }: { series: TrafficClickRow[] }) {
  const days = useMemo(() => dailyTotals(series), [series]);
  const [hover, setHover] = useState<number | null>(null);

  if (!days.length) {
    return <p className="set-hint">No clicks recorded in this window yet.</p>;
  }

  const { max, ticks } = niceTicks(Math.max(...days.map((d) => d.total)));
  const scale = scaleFor(max);
  const yAt = (v: number) => PLOT_H - scale(v);
  const barGap = 2;
  const barW = Math.max(1, (CHART_W - PAD_L) / days.length - barGap);

  const hovered = hover != null ? days[hover] : null;

  return (
    <div className="traffic-chart-wrap">
      <div className="traffic-plot">
        <YAxis ticks={ticks} yAt={yAt} />
        <svg
          className="traffic-chart"
          viewBox={`0 0 ${CHART_W} ${CHART_H}`}
          preserveAspectRatio="none"
          role="img"
          aria-label={`Daily clicks across all tracked links, ${days[0].day} through ${days[days.length - 1].day}, on a vertical scale of 0 to ${fmtInt(max)} clicks`}
          onMouseLeave={() => setHover(null)}
        >
          <Gridlines ticks={ticks} yAt={yAt} />
          {days.map((d, i) => {
            const barH = Math.max(1, scale(d.total));
            const x = PAD_L + i * (barW + barGap);
            const y = PLOT_H - barH;
            return (
              <rect
                key={d.day}
                x={x}
                y={y}
                width={barW}
                height={barH}
                rx={barRadius(barW, barH)}
                className={"traffic-bar" + (hover === i ? " hover" : "")}
                onMouseEnter={() => setHover(i)}
              />
            );
          })}
          <line x1={0} y1={PLOT_H} x2={CHART_W} y2={PLOT_H} className="traffic-baseline" />
        </svg>
        <div className="traffic-chart-axis">
          <span>{fmtDate(days[0].day)}</span>
          <span>{fmtDate(days[days.length - 1].day)}</span>
        </div>
      </div>
      <div className="traffic-tooltip" aria-live="polite">
        {hovered ? (
          <>
            <strong>{fmtDate(hovered.day)}</strong> — {fmtInt(hovered.total)} click
            {hovered.total === 1 ? "" : "s"}
            {Object.keys(hovered.bySlug).length > 0 && (
              <span className="traffic-tooltip-detail">
                {" "}
                (
                {Object.entries(hovered.bySlug)
                  .sort((a, b) => b[1] - a[1])
                  .map(([slug, n]) => `${slugLabel(slug)}: ${n}`)
                  .join(", ")}
                )
              </span>
            )}
          </>
        ) : (
          <span className="set-hint">Hover a bar for that day's breakdown.</span>
        )}
      </div>
    </div>
  );
}

/** Who is clicking: the classified clicks by class, and the rules that decided
 * the non-human ones. This is the evidence behind every human-only figure on
 * the screen, so it is shown rather than summarised away. Shares are of
 * classified clicks only — the same period the human figures cover. */
function WhoIsClicking({ c, scope }: { c: TrafficClassification; scope: string }) {
  const classes = ["human", "unverified", "automated"] as const;
  const total = classes.reduce((n, k) => n + (c.by_class?.[k]?.clicks ?? 0), 0);
  const reasons = (c.reasons ?? []).filter((r) => r.class !== "human" && r.clicks > 0).slice(0, 6);

  if (!total) {
    return (
      <p className="set-hint">
        No classified clicks yet. Every click recorded from now on is labelled human, unverified or
        automated.
      </p>
    );
  }

  return (
    <>
      <table className="traffic-table">
        <thead>
          <tr>
            <th>Class</th>
            <th className="num">Clicks</th>
            <th className="num">Share</th>
            <th className="num">Visitors</th>
          </tr>
        </thead>
        <tbody>
          {classes.map((k) => (
            <tr key={k}>
              <td>{TRAFFIC_CLASS_LABEL[k]}</td>
              <td className="num">{fmtInt(c.by_class?.[k]?.clicks ?? 0)}</td>
              <td className="num">{fmtShare(c.by_class?.[k]?.clicks ?? 0, total)}</td>
              <td className="num">{fmtInt(c.by_class?.[k]?.visitors ?? 0)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {reasons.length > 0 && (
        <table className="traffic-table">
          <thead>
            <tr>
              <th>Why not counted as human</th>
              <th>Class</th>
              <th className="num">Clicks</th>
            </tr>
          </thead>
          <tbody>
            {reasons.map((r) => (
              <tr key={`${r.class}|${r.reason}`}>
                <td>{TRAFFIC_REASON_LABEL[r.reason] || r.reason}</td>
                <td>{TRAFFIC_CLASS_LABEL[r.class] || r.class}</td>
                <td className="num">{fmtInt(r.clicks)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="set-hint">
        Classified by the click Worker from the headers a browser sends with a real link click
        (Fetch Metadata, Accept-Language) and the network it came from ({scope}). <em>Human</em>{" "}
        needs all of it. <em>Unverified</em> is real-browser-shaped but unproven: typed or
        bookmarked URLs, links opened from Slack or a terminal, old browsers, VPNs.{" "}
        <em>Automated</em> has positive evidence of a machine. Visitors are distinct within each
        class and don't add up across them. Rule and blind spots: <code>worker/README.md</code> in
        the webpage repo.
      </p>
    </>
  );
}

export function Traffic(_: ScreenProps) {
  const [days, setDays] = useState<(typeof DAYS_OPTIONS)[number]>(90);
  const [metric, setMetric] = useState<TrafficMetric>("people");
  const [refreshing, setRefreshing] = useState(false);
  const q = useTraffic(true, days);
  const data = q.data;

  // Which click figures lead (human-only, or unfiltered against an older
  // Worker) — decided once so no two parts of the screen mix scopes.
  const view = useMemo(() => clickView(data), [data]);
  const classification = data?.clicks.classification ?? null;

  /** Per-link rows in the screen's scope: human clicks joined to the
   * automated/unverified counts when classified, else the unfiltered totals. */
  const linkRows = useMemo(() => {
    if (!data || !view) return [];
    const visitors = new Map(view.visitors_by_slug.map((r) => [r.slug, r]));
    if (view.scope === "all") {
      return Object.entries(data.clicks.totals_by_slug)
        .sort((a, b) => b[1] - a[1])
        .map(([slug, n]) => ({ slug, clicks: n, visitors: visitors.get(slug), unverified: null, automated: null }));
    }
    const human = new Map<string, number>();
    for (const r of view.series) human.set(r.slug, (human.get(r.slug) || 0) + r.clicks);
    const other = new Map((classification?.by_slug ?? []).map((r) => [r.slug, r]));
    const slugs = new Set([...human.keys(), ...other.keys()]);
    return Array.from(slugs)
      .map((slug) => ({
        slug,
        clicks: human.get(slug) ?? 0,
        visitors: visitors.get(slug),
        unverified: other.get(slug)?.unverified ?? 0,
        automated: other.get(slug)?.automated ?? 0,
      }))
      .sort((a, b) => b.clicks - a.clicks || b.automated - a.automated);
  }, [data, view, classification]);

  const people = view?.totals ?? null;
  const funnel = view?.downloads ?? null;
  const osMatch = useMemo(
    () => osMatchedDownloads(view?.series ?? [], view?.downloads?.os_match),
    [view]
  );
  const dl = releaseDownloads(data);

  // What the chart may actually draw, as opposed to what was last asked for —
  // see lib/traffic.ts for why this is derived rather than read off state.
  const shown = shownMetric(metric, data);

  const doRefresh = async () => {
    setRefreshing(true);
    try {
      await refreshTraffic(days);
      await q.refetch();
    } finally {
      setRefreshing(false);
    }
  };

  const tag = view && data ? scopeTag(view, data.clicks.days) : "";
  const cls = view?.classified ?? null;

  return (
    <>
      <h3 className="set-section-title">Site traffic</h3>
      <p className="set-hint">
        Clicks on the tracked links on mindflock.ai (download buttons, GitHub, directory badges —
        mindflock.ai/go/*) and GitHub's own counters, pulled live from GitHub's API and the
        click-tracking Worker. Page views aren't measured at all: the site runs no analytics.
        Visible in this dev build only.
      </p>

      {q.isLoading && !data && <p className="set-hint">Loading…</p>}
      {q.isError && !data && (
        <p className="set-hint" id="traffic-load-state" role="alert">
          Couldn't load traffic data.{" "}
          <button type="button" className="linklike" onClick={() => void q.refetch()}>
            Retry
          </button>
        </p>
      )}

      {data && view && (
        <>
          {(data.errors.github || data.errors.clicks) && (
            <div className="traffic-warn">
              {data.errors.github && <div>GitHub: {data.errors.github}</div>}
              {data.errors.clicks && <div>Click tracking: {data.errors.clicks}</div>}
            </div>
          )}

          <div className="traffic-tiles">
            <div className="traffic-tile primary">
              <span className="traffic-tile-num">{fmtInt(people?.new_visitors)}</span>
              <span className="traffic-tile-label">First-time visitors ({tag})</span>
            </div>
            <div className="traffic-tile">
              <span className="traffic-tile-num">{fmtInt(people?.visitors)}</span>
              <span className="traffic-tile-label">Unique visitors ({tag})</span>
              {cls && cls.clicks > 0 && (
                <span className="traffic-tile-sub">
                  {plural(cls.automated.visitors, "automated visitor")} left out
                </span>
              )}
            </div>
            <div className="traffic-tile">
              <span className="traffic-tile-num">
                {fmtInt(people?.clicks ?? view.series.reduce((n, r) => n + r.clicks, 0))}
              </span>
              <span className="traffic-tile-label">Tracked link clicks ({tag})</span>
              {cls && cls.clicks > 0 && (
                <span className="traffic-tile-sub">
                  +{fmtInt(cls.automated.clicks)} automated ({fmtShare(cls.automated.clicks, cls.clicks)}),{" "}
                  {fmtInt(cls.unverified.clicks)} unverified
                </span>
              )}
            </div>
            <div className="traffic-tile">
              <span className="traffic-tile-num">{fmtShare(osMatch.matched, osMatch.clicks)}</span>
              <span className="traffic-tile-label">Download clicks from the installer's OS ({tag})</span>
              <span className="traffic-tile-sub">
                {fmtInt(osMatch.matched)} of {plural(osMatch.clicks, "download click")}
              </span>
            </div>
            <div className="traffic-tile">
              <span className="traffic-tile-num">{fmtInt(dl.installers ?? data.downloads_total)}</span>
              <span className="traffic-tile-label">
                {dl.installers != null
                  ? "Installer downloads (GitHub, all-time)"
                  : "Release asset downloads (GitHub, all-time)"}
              </span>
              {dl.other != null && (
                <span className="traffic-tile-sub">
                  +{fmtInt(dl.other)} other files: update checks, checksums, packages
                </span>
              )}
            </div>
            <div className="traffic-tile">
              <span className="traffic-tile-num">{fmtInt(data.repo?.stars)}</span>
              <span className="traffic-tile-label">GitHub stars</span>
            </div>
            <div className="traffic-tile">
              <span className="traffic-tile-num">{fmtInt(data.repo?.forks)}</span>
              <span className="traffic-tile-label">Forks</span>
            </div>
          </div>

          {!data.errors.clicks && view.state === "old-worker" && (
            <p className="set-hint" id="traffic-scope-note">
              <strong>These click and visitor figures are unfiltered.</strong> The click Worker on
              mindflock.ai doesn't yet tell people from automated traffic, and most of this is
              automated: every link draws about the same count, and phones "click" desktop
              installers. Deploy the classifying Worker (<code>worker/README.md</code> in the
              webpage repo) for human-only figures; they cover clicks from the deploy onwards.
            </p>
          )}
          {!data.errors.clicks && view.state === "pending" && (
            <p className="set-hint" id="traffic-scope-note">
              The click Worker classifies clicks, but none have been recorded since it was deployed.
              The figures above fill in as people click.
            </p>
          )}
          {!data.errors.clicks && view.state === "partial" && (
            <p className="set-hint" id="traffic-scope-note">
              Click and visitor figures count <strong>human clicks only</strong>, from{" "}
              {fmtDate(view.since)} — when the Worker started classifying. The{" "}
              {fmtInt(classification?.unclassified_clicks ?? 0)} clicks before then in this window
              can't be told apart and are left out.
            </p>
          )}
          {!people && !data.errors.clicks && view.state === "old-worker" && (
            <p className="set-hint">
              Visitor counts are empty because the click Worker hasn't been redeployed with visitor
              attribution yet (see <code>worker/README.md</code> in the webpage repo — it needs the{" "}
              <code>VISITORS</code> KV namespace and the <code>VISITOR_SALT</code> secret). Clicks
              below are unaffected.
            </p>
          )}

          {classification && (
            <>
              <h3 className="set-section-title">Who's clicking</h3>
              <WhoIsClicking
                c={classification}
                scope={
                  view.state === "partial"
                    ? `since ${fmtDate(view.since)}`
                    : `last ${data.clicks.days} days`
                }
              />
            </>
          )}

          <h3 className="set-section-title">New user funnel ({tag})</h3>
          {funnel ? (
            <DownloadFunnel funnel={funnel} scope={tag} osMatch={osMatch} />
          ) : (
            <p className="set-hint">
              Not available until the click Worker reports visitor attribution. GitHub's download
              counters can't stand in for it — they're opaque tallies that include updates and
              re-downloads, with no way to tell one person from another.
            </p>
          )}

          <h3 className="set-section-title">GitHub stars over time</h3>
          {/* A server running code from before this field existed answers without
              it — a real possibility for a locally-running dev backend, not just
              a type-system technicality — so this is the one spot the API
              response is treated as untrusted rather than the TrafficResponse
              type. */}
          <StarsChart points={data.star_history ?? []} />

          <h3 className="set-section-title">
            {shown === "people" ? "Visitors over time" : "Clicks over time"} ({tag})
          </h3>

          <div className="set-row traffic-range-row">
            <div className="traffic-controls">
              <div className="usage-periods traffic-range">
                {DAYS_OPTIONS.map((d) => (
                  <button
                    key={d}
                    type="button"
                    className={"usage-period" + (days === d ? " active" : "")}
                    onClick={() => setDays(d)}
                  >
                    {d}d
                  </button>
                ))}
              </div>
              {/* People and clicks are different quantities on different scales
                  (one person can click many links), so they get one chart and a
                  switch rather than two y-axes on the same plot.

                  The switch only exists when there is something to switch
                  between. It used to render with People disabled whenever the
                  Worker had no visitor data, which was a one-way door: `metric`
                  starts at "people", so People drew as active, and pressing
                  Clicks moved you to a state you could not leave. A control
                  whose default value is the one it disables is a trap, so when
                  there is one option it is simply not a control. */}
              {data.clicks.totals ? (
                <div className="usage-periods traffic-range">
                  <button
                    type="button"
                    className={"usage-period" + (shown === "people" ? " active" : "")}
                    onClick={() => setMetric("people")}
                  >
                    People
                  </button>
                  <button
                    type="button"
                    className={"usage-period" + (shown === "clicks" ? " active" : "")}
                    onClick={() => setMetric("clicks")}
                  >
                    Clicks
                  </button>
                </div>
              ) : null}
            </div>
            <span className="test-row">
              <button type="button" className="test-btn" onClick={doRefresh} disabled={refreshing}>
                {refreshing ? "Refreshing…" : "Refresh"}
              </button>
              <span className="set-hint">
                Cached ~5 min server-side to stay under GitHub's rate limit — this bypasses it.
              </span>
            </span>
          </div>

          {shown === "people" ? (
            <VisitorsChart days={view.visitors_by_day} />
          ) : (
            <ClicksChart series={view.series} />
          )}

          {linkRows.length > 0 && (
            <>
              <h3 className="set-section-title">By link ({tag})</h3>
              <table className="traffic-table">
                <thead>
                  <tr>
                    <th>Link</th>
                    {people && <th className="num">First-time</th>}
                    {people && <th className="num">Visitors</th>}
                    <th className="num">Clicks</th>
                    {view.scope === "human" && <th className="num">Unverified</th>}
                    {view.scope === "human" && <th className="num">Automated</th>}
                  </tr>
                </thead>
                <tbody>
                  {linkRows.map((r) => (
                    <tr key={r.slug}>
                      <td>{slugLabel(r.slug)}</td>
                      {people && <td className="num">{fmtInt(r.visitors?.new_visitors ?? 0)}</td>}
                      {people && <td className="num">{fmtInt(r.visitors?.visitors ?? 0)}</td>}
                      <td className="num">{fmtInt(r.clicks)}</td>
                      {view.scope === "human" && <td className="num">{fmtInt(r.unverified)}</td>}
                      {view.scope === "human" && <td className="num">{fmtInt(r.automated)}</td>}
                    </tr>
                  ))}
                </tbody>
              </table>
              {people && (
                <p className="set-hint">
                  Visitor columns are deduplicated per link over the window, so they don't add up
                  down the column — one person clicking two links is one visitor on each row and one
                  visitor overall.
                  {view.scope === "all" &&
                    " Near-equal counts on every link, the directory badges included, are the sign of something following every link on the page."}
                </p>
              )}
            </>
          )}

          <h3 className="set-section-title">Installer downloads over time</h3>
          {data.releases.length === 0 ? (
            <p className="set-hint">No releases found.</p>
          ) : (
            <ReleasesChart releases={data.releases} />
          )}

          <h3 className="set-section-title">Downloads by version</h3>
          <p className="set-hint">
            GitHub's raw asset counters, which are <em>not</em> people: GitHub attaches no identity,
            so nothing here can be split into new versus returning users, or people versus crawlers.
            <em> Installers</em> are <code>MindFlock.dmg</code>, <code>MindFlock-Setup.exe</code>{" "}
            and <code>MindFlock.AppImage</code> — but the app's auto-updater downloads the same .exe
            and .AppImage to update an existing install, so on Windows and Linux an update looks
            exactly like a first download. <em>Other files</em> are the <code>latest*.yml</code>{" "}
            every running app reads when it checks for an update, the macOS update{" "}
            <code>MindFlock.zip</code> and block maps, checksums, and the Python packages. Near-equal
            installer counts for all three platforms on a release are more likely a crawler
            fetching every file than three sets of people.
          </p>
          {dl.byKind && (
            <p className="set-hint">
              All-time: {fmtInt(dl.byKind.installer)} installers · {fmtInt(dl.byKind.update)} update
              files · {fmtInt(dl.byKind.update_check)} update checks · {fmtInt(dl.byKind.checksum)}{" "}
              checksums · {fmtInt(dl.byKind.package)} Python packages
              {dl.byKind.other ? ` · ${fmtInt(dl.byKind.other)} other` : ""}.
            </p>
          )}
          {data.releases.length === 0 ? (
            <p className="set-hint">No releases found.</p>
          ) : (
            <table className="traffic-table">
              <thead>
                <tr>
                  <th>Version</th>
                  <th>Published</th>
                  {dl.installers != null && <th className="num">Installers</th>}
                  {dl.installers != null && <th className="num">Other files</th>}
                  <th className="num">All assets</th>
                </tr>
              </thead>
              <tbody>
                {data.releases.map((r) => (
                  <tr key={r.tag}>
                    <td>
                      {r.tag}
                      {r.prerelease && <span className="traffic-pre"> pre-release</span>}
                    </td>
                    <td>{fmtDate(r.published_at)}</td>
                    {dl.installers != null && <td className="num">{fmtInt(r.installer_downloads ?? 0)}</td>}
                    {dl.installers != null && (
                      <td className="num">{fmtInt(r.total_downloads - (r.installer_downloads ?? 0))}</td>
                    )}
                    <td className="num">{fmtInt(r.total_downloads)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </>
  );
}
