/** The landing strip (docs/flights.md "Round 15" → `objects`, "As built:
 * round 15 objects"): a sky object placed right of every line (layout key
 * "landing", lib/skyLayout `LANDING_KEY`) that shows, per repo in the sky,
 * its default branch — the name, the checks of its head (green / red /
 * pending / none), the last merge's age — and **Landed today**: the flights
 * finished `done` whose last ship step merged, in the last 24 h (the
 * session's name, a PR link). Data: `GET /api/sky/landing` (api/skyApi),
 * which the host fetches and passes in (`data`), refetched every minute.
 *
 * It behaves like the intake lane: drags by its head (`onHeadDown`, unless
 * `draggable` is false — click mode), folds to a rail (‹ / ›, remembered per
 * device), expands from its head (▸ / ▾, `open`: every landed row instead of
 * the first three per repo), sizes by its corner (`onResize`, the delta over
 * the zoom), and measures itself for the host (`onMeasure`). */

import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { CheckTone, LandedFlight, LandingData, LandingRepo } from "../../api/skyApi";
import { relTime } from "../../lib/format";
import { LANDING_MIN } from "../../lib/skyLayout";
import { windowName } from "../../lib/windowName";
import "./LandingStrip.css";

export interface LandingStripProps {
  data: LandingData | null;
  error?: string;
  /** The frame the host keeps (its layout entry's w / h), else its own size. */
  width?: number;
  height?: number;
  zoom?: number;
  /** Expanded: every landed row (folded: the first three per repo). */
  open?: boolean;
  onToggleOpen?(): void;
  onHeadDown?(e: ReactPointerEvent): void;
  draggable?: boolean;
  onResize?(w: number, h: number): void;
  onMeasure?(w: number, h: number): void;
  /** For the words ("2h ago"): unix seconds, else now. */
  now?: number;
}

const FOLDED_KEY = "mf_sky_landing_folded";
const readFolded = () => {
  try {
    return typeof localStorage !== "undefined" && localStorage.getItem(FOLDED_KEY) === "1";
  } catch {
    return false;
  }
};
const writeFolded = (v: boolean) => {
  try {
    localStorage.setItem(FOLDED_KEY, v ? "1" : "0");
  } catch {
    /* a private window */
  }
};

/** How many landed rows a folded strip shows per repo. */
export const LANDED_FOLDED = 3;

/** The checks' word and tone for the dot. */
export function checksWords(tone: CheckTone): string {
  switch (tone) {
    case "green":
      return "checks green";
    case "red":
      return "checks red";
    case "pending":
      return "checks running";
    default:
      return "no checks";
  }
}

/** "merged 2h ago", or "no merges yet". */
export function mergeWords(at: number | null, now?: number): string {
  if (!at) return "no merges yet";
  return "merged " + relTime(at, { now });
}

/** The landed rows the strip shows: every one expanded, the first
 * `LANDED_FOLDED` folded, newest first. */
export function shownLanded(repo: Pick<LandingRepo, "landed">, open: boolean): { shown: LandedFlight[]; more: number } {
  const all = [...repo.landed].sort((a, b) => b.at - a.at);
  const shown = open ? all : all.slice(0, LANDED_FOLDED);
  return { shown, more: all.length - shown.length };
}

/** Every landed flight across the strip's repos. */
export const landedCount = (data: LandingData | null | undefined): number => (data?.repos || []).reduce((n, r) => n + r.landed.length, 0);

/** A press on the head that is the head's own (not one of its buttons). */
const headPress = (e: ReactPointerEvent) => e.button === 0 && !(e.target as Element).closest("button, a, input, summary, [data-no-drag]");

export function LandingStrip({ data, error = "", width, height, zoom = 1, open = false, onToggleOpen, onHeadDown, draggable = true, onResize, onMeasure, now }: LandingStripProps) {
  const [folded, setFolded] = useState(readFolded);
  const rootRef = useRef<HTMLElement>(null);
  const [live, setLive] = useState<{ w: number; h: number } | null>(null);
  const grab = useRef<{ id: number; sx: number; sy: number; w: number; h: number } | null>(null);
  const frameNow = live || { w: width ?? 0, h: height ?? 0 };
  const resizeDown = (e: ReactPointerEvent<HTMLSpanElement>) => {
    if (e.button !== 0 || !onResize) return;
    e.preventDefault();
    e.stopPropagation();
    const el = rootRef.current;
    grab.current = { id: e.pointerId, sx: e.clientX, sy: e.clientY, w: width ?? (el ? el.offsetWidth : 300), h: height ?? (el ? el.offsetHeight : 240) };
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* a synthetic event has no capture */
    }
  };
  const resizeMove = (e: ReactPointerEvent<HTMLSpanElement>) => {
    const g = grab.current;
    if (!g || g.id !== e.pointerId) return;
    const z = zoom > 0 ? zoom : 1;
    setLive({ w: Math.max(LANDING_MIN.w, Math.round(g.w + (e.clientX - g.sx) / z)), h: Math.max(LANDING_MIN.h, Math.round(g.h + (e.clientY - g.sy) / z)) });
  };
  const resizeUp = (e: ReactPointerEvent<HTMLSpanElement>) => {
    const g = grab.current;
    if (!g || g.id !== e.pointerId) return;
    grab.current = null;
    if (live && onResize) onResize(live.w, live.h);
    setLive(null);
  };
  // The host lays the strip out from what it measures (plane px), as a line's.
  const measureRef = useRef(onMeasure);
  measureRef.current = onMeasure;
  useEffect(() => {
    const el = rootRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const read = () => {
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      if (w > 0 && h > 0) measureRef.current?.(w, h);
    };
    read();
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, [folded]);
  const fold = (v: boolean) => {
    setFolded(v);
    writeFolded(v);
  };
  const headDown = (e: ReactPointerEvent) => {
    if (draggable && onHeadDown && headPress(e)) onHeadDown(e);
  };
  const moveHint = draggable && onHeadDown ? "Drag to move the landing strip" : "";
  const total = landedCount(data);

  if (folded)
    return (
      <nav ref={rootRef} className="sky-lane sky-lane-rail sky-landing" aria-label="Landing" data-collapsed="true" title={moveHint || undefined} onPointerDown={headDown}>
        <button type="button" className="sky-lane-toggle" aria-expanded={false} title="Open the landing strip" onClick={() => fold(false)}>
          ›
        </button>
        <button type="button" className="sky-lane-rail-icon fl-kind-badge sky-landing-glyph" title={"Landing — " + (total ? total + " landed today" : "nothing landed today")} onClick={() => fold(false)}>
          ⤓{total > 0 && <span className="sky-lane-rail-count">{total}</span>}
        </button>
      </nav>
    );

  return (
    <nav
      ref={rootRef}
      className="sky-lane sky-landing"
      aria-label="Landing"
      data-open={open ? "" : undefined}
      style={{ width: frameNow.w > 0 ? frameNow.w : undefined, height: frameNow.h > 0 ? frameNow.h : undefined }}
    >
      <div
        className="sky-lane-head"
        data-sky-head
        title={[moveHint, onToggleOpen ? "double-click to " + (open ? "fold" : "expand") : ""].filter(Boolean).join(" · ") || undefined}
        onPointerDown={headDown}
        onDoubleClick={(e) => {
          if (!onToggleOpen || (e.target as Element).closest("button")) return;
          e.stopPropagation();
          onToggleOpen();
        }}
      >
        <span className="sky-lane-title">Landing</span>
        {total > 0 && (
          <span className="fl-count" title={total + " landed today"}>
            {total}
          </span>
        )}
        {onToggleOpen && (
          <button
            type="button"
            className="sky-lane-toggle sky-lane-expand"
            aria-expanded={open}
            aria-label={open ? "Fold the landing strip's rows" : "Expand the landing strip: every landed flight"}
            title={open ? "Fold: the first rows of each repo" : "Expand: every landed flight"}
            onClick={onToggleOpen}
          >
            {open ? "▾" : "▸"}
          </button>
        )}
        <button type="button" className="sky-lane-toggle" aria-expanded={true} title="Fold the landing strip" onClick={() => fold(true)}>
          ‹
        </button>
      </div>
      {error ? (
        <p className="sky-lane-empty muted">{error}</p>
      ) : !data ? (
        <p className="sky-lane-empty muted">Looking at the repos…</p>
      ) : data.repos.length === 0 ? (
        <p className="sky-lane-empty muted">No repo in the sky yet.</p>
      ) : (
        data.repos.map((r) => <RepoRow key={r.root} repo={r} open={open} now={now} />)
      )}
      {onResize && (
        <span
          className="sky-lane-resize skl-resize"
          role="separator"
          tabIndex={0}
          data-no-drag
          aria-label="Resize the landing strip"
          title="Drag to resize the landing strip"
          onPointerDown={resizeDown}
          onPointerMove={resizeMove}
          onPointerUp={resizeUp}
          onPointerCancel={resizeUp}
        />
      )}
    </nav>
  );
}

function RepoRow({ repo, open, now }: { repo: LandingRepo; open: boolean; now?: number }) {
  const { shown, more } = shownLanded(repo, open);
  return (
    <section className="sky-landing-repo" data-repo={repo.root} aria-label={repo.name}>
      <div className="sky-landing-row">
        <span className={"sky-landing-dot is-" + repo.checks} title={checksWords(repo.checks) + (repo.head ? " · " + repo.head.slice(0, 7) : "")} aria-label={checksWords(repo.checks)} />
        <strong className="sky-landing-name" title={repo.root}>
          {repo.name}
        </strong>
        <code className="sky-landing-branch">{repo.branch || "?"}</code>
      </div>
      <div className="sky-landing-merge muted">{mergeWords(repo.last_merge_at, now)}</div>
      {repo.landed.length > 0 && (
        <ul className="sky-landing-landed" aria-label={"Landed today in " + repo.name}>
          {shown.map((l) => (
            <li key={l.flight} className="sky-landing-flight">
              <span className="sky-landing-flight-name" title={l.title}>
                {windowName(l.title)}
              </span>
              {l.pr_url && (
                <a className="sky-landing-pr" href={l.pr_url} target="_blank" rel="noreferrer" title={l.pr_url} data-no-drag>
                  PR ↗
                </a>
              )}
              <span className="sky-landing-at muted">{relTime(l.at, { now })}</span>
            </li>
          ))}
          {more > 0 && <li className="sky-landing-more muted">+{more} more — expand ▸</li>}
        </ul>
      )}
    </section>
  );
}
