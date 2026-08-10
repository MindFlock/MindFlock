/** Settings → Site traffic (dev shell only): GitHub stars/forks, per-release
 * download counts, and click totals for the mindflock.ai/go/ tracked links
 * (macOS/Windows/Linux buttons, GitHub, Product Hunt, NxGn — see the Worker in
 * the webpage repo's worker/ directory). This is the maintainer's own reach
 * dashboard, not something an end user's build needs, hence the dev-shell
 * gate in SettingsDialog.tsx via isDevShell(). */

import { useMemo, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import { refreshTraffic, useTraffic } from "../../../state/queries";
import type { TrafficClickRow, TrafficRelease, TrafficStarPoint } from "../../../api/types";
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
};

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
    return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  } catch {
    return iso;
  }
}

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

  const w = 640;
  const h = 160;
  const padL = 4;
  const padB = 18;
  const plotH = h - padB;
  const max = Math.max(1, ...points.map((p) => p.stars));
  const stepX = (w - padL) / (points.length - 1);
  const xAt = (i: number) => padL + i * stepX;
  const yAt = (v: number) => plotH - (v / max) * (plotH - 8);
  const pathD = points.map((p, i) => `${i === 0 ? "M" : "L"}${xAt(i).toFixed(2)},${yAt(p.stars).toFixed(2)}`).join(" ");

  const handleMove = (e: ReactMouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const relX = ((e.clientX - rect.left) / rect.width) * w;
    const idx = Math.max(0, Math.min(points.length - 1, Math.round((relX - padL) / stepX)));
    setHoverIdx(idx);
  };

  const hovered = hoverIdx != null ? points[hoverIdx] : null;

  return (
    <div className="traffic-chart-wrap">
      <svg
        className="traffic-chart"
        viewBox={`0 0 ${w} ${h}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`Cumulative GitHub stars, ${fmtDate(points[0].day)} through ${fmtDate(points[points.length - 1].day)}`}
        onMouseMove={handleMove}
        onMouseLeave={() => setHoverIdx(null)}
      >
        <path d={pathD} className="traffic-line" fill="none" />
        {hoverIdx != null && (
          <>
            <line x1={xAt(hoverIdx)} y1={0} x2={xAt(hoverIdx)} y2={plotH} className="traffic-crosshair" />
            <circle cx={xAt(hoverIdx)} cy={yAt(points[hoverIdx].stars)} r={4} className="traffic-line-dot" />
          </>
        )}
        <line x1={0} y1={plotH} x2={w} y2={plotH} className="traffic-baseline" />
      </svg>
      <div className="traffic-chart-axis">
        <span>{fmtDate(points[0].day)}</span>
        <span>{fmtDate(points[points.length - 1].day)}</span>
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

  const max = Math.max(1, ...chrono.map((r) => r.total_downloads));
  const w = 640;
  const h = 160;
  const padL = 4;
  const padB = 18;
  const plotH = h - padB;
  const barGap = 3;
  const barW = Math.max(1, (w - padL) / chrono.length - barGap);

  const hovered = hover != null ? chrono[hover] : null;

  return (
    <div className="traffic-chart-wrap">
      <svg
        className="traffic-chart"
        viewBox={`0 0 ${w} ${h}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`Downloads per release, ${chrono[0].tag} through ${chrono[chrono.length - 1].tag}`}
        onMouseLeave={() => setHover(null)}
      >
        {chrono.map((r, i) => {
          const barH = Math.max(1, (r.total_downloads / max) * (plotH - 8));
          const x = padL + i * (barW + barGap);
          const y = plotH - barH;
          return (
            <rect
              key={r.tag}
              x={x}
              y={y}
              width={barW}
              height={barH}
              rx={Math.min(3, barW / 2)}
              className={"traffic-bar" + (hover === i ? " hover" : "")}
              onMouseEnter={() => setHover(i)}
            />
          );
        })}
        <line x1={0} y1={plotH} x2={w} y2={plotH} className="traffic-baseline" />
      </svg>
      <div className="traffic-chart-axis">
        <span>{fmtDate(chrono[0].published_at)}</span>
        <span>{fmtDate(chrono[chrono.length - 1].published_at)}</span>
      </div>
      <div className="traffic-tooltip" aria-live="polite">
        {hovered ? (
          <>
            <strong>{hovered.tag}</strong> ({fmtDate(hovered.published_at)}) —{" "}
            {fmtInt(hovered.total_downloads)} download{hovered.total_downloads === 1 ? "" : "s"}
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

function ClicksChart({ series }: { series: TrafficClickRow[] }) {
  const days = useMemo(() => dailyTotals(series), [series]);
  const [hover, setHover] = useState<number | null>(null);

  if (!days.length) {
    return <p className="set-hint">No clicks recorded in this window yet.</p>;
  }

  const max = Math.max(1, ...days.map((d) => d.total));
  const w = 640;
  const h = 160;
  const padL = 4;
  const padB = 18;
  const plotH = h - padB;
  const barGap = 2;
  const barW = Math.max(1, (w - padL) / days.length - barGap);

  const hovered = hover != null ? days[hover] : null;

  return (
    <div className="traffic-chart-wrap">
      <svg
        className="traffic-chart"
        viewBox={`0 0 ${w} ${h}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`Daily clicks across all tracked links, ${days[0].day} through ${days[days.length - 1].day}`}
        onMouseLeave={() => setHover(null)}
      >
        {days.map((d, i) => {
          const barH = Math.max(1, (d.total / max) * (plotH - 8));
          const x = padL + i * (barW + barGap);
          const y = plotH - barH;
          return (
            <rect
              key={d.day}
              x={x}
              y={y}
              width={barW}
              height={barH}
              rx={Math.min(3, barW / 2)}
              className={"traffic-bar" + (hover === i ? " hover" : "")}
              onMouseEnter={() => setHover(i)}
            />
          );
        })}
        <line x1={0} y1={plotH} x2={w} y2={plotH} className="traffic-baseline" />
      </svg>
      <div className="traffic-chart-axis">
        <span>{fmtDate(days[0].day)}</span>
        <span>{fmtDate(days[days.length - 1].day)}</span>
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

export function Traffic(_: ScreenProps) {
  const [days, setDays] = useState<(typeof DAYS_OPTIONS)[number]>(90);
  const [refreshing, setRefreshing] = useState(false);
  const q = useTraffic(true, days);
  const data = q.data;

  const clickTotalsSorted = useMemo(() => {
    if (!data) return [];
    return Object.entries(data.clicks.totals_by_slug).sort((a, b) => b[1] - a[1]);
  }, [data]);

  const doRefresh = async () => {
    setRefreshing(true);
    try {
      await refreshTraffic(days);
      await q.refetch();
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <>
      <h3 className="set-section-title">Site traffic</h3>
      <p className="set-hint">
        Reach across mindflock.ai and the GitHub repo — pulled live from GitHub's API and the
        click-tracking Worker on mindflock.ai/go/*. Visible in this dev build only.
      </p>

      {q.isLoading && !data && <p className="set-hint">Loading…</p>}
      {q.isError && !data && (
        <p className="set-hint">Could not load traffic data. Check your connection and retry.</p>
      )}

      {data && (
        <>
          {(data.errors.github || data.errors.clicks) && (
            <div className="traffic-warn">
              {data.errors.github && <div>GitHub: {data.errors.github}</div>}
              {data.errors.clicks && <div>Click tracking: {data.errors.clicks}</div>}
            </div>
          )}

          <div className="traffic-tiles">
            <div className="traffic-tile">
              <span className="traffic-tile-num">{fmtInt(data.repo?.stars)}</span>
              <span className="traffic-tile-label">GitHub stars</span>
            </div>
            <div className="traffic-tile">
              <span className="traffic-tile-num">{fmtInt(data.repo?.forks)}</span>
              <span className="traffic-tile-label">Forks</span>
            </div>
            <div className="traffic-tile">
              <span className="traffic-tile-num">{fmtInt(data.downloads_total)}</span>
              <span className="traffic-tile-label">Total downloads (all releases)</span>
            </div>
            <div className="traffic-tile">
              <span className="traffic-tile-num">
                {fmtInt(clickTotalsSorted.reduce((s, [, n]) => s + n, 0))}
              </span>
              <span className="traffic-tile-label">Tracked link clicks ({data.clicks.days}d)</span>
            </div>
          </div>

          <h3 className="set-section-title">GitHub stars over time</h3>
          {/* A server running code from before this field existed answers without
              it — a real possibility for a locally-running dev backend, not just
              a type-system technicality — so this is the one spot the API
              response is treated as untrusted rather than the TrafficResponse
              type. */}
          <StarsChart points={data.star_history ?? []} />

          <div className="set-row traffic-range-row">
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
            <span className="test-row">
              <button type="button" className="test-btn" onClick={doRefresh} disabled={refreshing}>
                {refreshing ? "Refreshing…" : "Refresh"}
              </button>
              <span className="set-hint">
                Cached ~5 min server-side to stay under GitHub's rate limit — this bypasses it.
              </span>
            </span>
          </div>

          <ClicksChart series={data.clicks.series} />

          {clickTotalsSorted.length > 0 && (
            <>
              <h3 className="set-section-title">Clicks by link ({data.clicks.days}d)</h3>
              <table className="traffic-table">
                <thead>
                  <tr>
                    <th>Link</th>
                    <th className="num">Clicks</th>
                  </tr>
                </thead>
                <tbody>
                  {clickTotalsSorted.map(([slug, n]) => (
                    <tr key={slug}>
                      <td>{slugLabel(slug)}</td>
                      <td className="num">{fmtInt(n)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          <h3 className="set-section-title">Downloads over time</h3>
          {data.releases.length === 0 ? (
            <p className="set-hint">No releases found.</p>
          ) : (
            <ReleasesChart releases={data.releases} />
          )}

          <h3 className="set-section-title">Downloads by version</h3>
          {data.releases.length === 0 ? (
            <p className="set-hint">No releases found.</p>
          ) : (
            <table className="traffic-table">
              <thead>
                <tr>
                  <th>Version</th>
                  <th>Published</th>
                  <th className="num">Downloads</th>
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
