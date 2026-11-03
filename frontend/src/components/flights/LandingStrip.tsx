/** The Finished strip (docs/flights.md "Round 15" → `objects`, "As built:
 * round 15 objects"; reworked in round-consolidate): a sky object placed
 * right of every line (layout key "landing", lib/skyLayout `LANDING_KEY`),
 * HIDDEN by default — the Sky's ⋯ › Show the Finished strip turns it on
 * (remembered per device, SkyView `FINISHED_KEY`), its × turns it off. It
 * says what it is under its title (`FINISHED_EXPLAIN`) and shows, per repo in
 * the sky, its default branch — the name, the checks of its head (green /
 * red / pending / none), the age of its last merge (or of its last commit,
 * said so, when it has none — `mergeWords`) — and the chains merged today:
 * the flights finished `done` whose last ship step merged, in the last 24 h
 * (the session's name, a PR link). Data: `GET /api/sky/landing`
 * (api/skyApi), which the host fetches only while it is shown and passes in
 * (`data`), refetched every minute.
 *
 * The look is ux-lab SKY_OBJECT_STYLE.md (LandingStrip.css): the session
 * frame's calm box — one hairline, radius 14px, a 30px title strip with the
 * title in normal case and quiet `fl-icon-btn`s — and quiet rows separated
 * by hairlines. It drags by its title bar (`onHeadDown`, unless `draggable`
 * is false — click mode), expands from it (▸ / ▾, `open`: every merged row
 * instead of the first three per repo), sizes by its corner (`onResize`,
 * the delta over the zoom), and measures itself for the host (`onMeasure`). */

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
  /** Its ×: hide the strip (the ⋯ menu brings it back). */
  onHide?(): void;
  onHeadDown?(e: ReactPointerEvent): void;
  draggable?: boolean;
  onResize?(w: number, h: number): void;
  onMeasure?(w: number, h: number): void;
  /** For the words ("2h ago"): unix seconds, else now. */
  now?: number;
}

/** The line under its title: what the strip is for. */
export const FINISHED_EXPLAIN = "Each repo's main branch: its CI and its last merge — and routes merged today";

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

/** What the server's `last_merge_at` is: a merge commit's time, or — on a
 * branch with no merge commit yet (a fresh repo, a squash-merge habit) — its
 * last commit's (backend/web/core/landing.py `last_merge_at`). A server that
 * says which sends `last_merge_kind`; an older one doesn't, and its time may
 * be either, so the words never claim a merge they can't vouch for. */
export type MergeKind = "merge" | "commit";

/** "merged 2h ago" (a merge commit), "last commit 2h ago" (no merge yet),
 * "updated 2h ago" (the server didn't say which), or "no merges yet". */
export function mergeWords(at: number | null, now?: number, kind?: MergeKind): string {
  if (!at) return "no merges yet";
  const ago = relTime(at, { now });
  if (kind === "merge") return "merged " + ago;
  if (kind === "commit") return "last commit " + ago;
  return "updated " + ago;
}

/** The landed rows the strip shows: every one expanded, the first
 * `LANDED_FOLDED` folded, newest first. */
export function shownLanded(repo: Pick<LandingRepo, "landed">, open: boolean): { shown: LandedFlight[]; more: number } {
  const all = [...(repo.landed || [])].sort((a, b) => b.at - a.at);
  const shown = open ? all : all.slice(0, LANDED_FOLDED);
  return { shown, more: all.length - shown.length };
}

/** Every landed flight across the strip's repos. */
export const landedCount = (data: LandingData | null | undefined): number => (data?.repos || []).reduce((n, r) => n + (r.landed || []).length, 0);

/** A press on the head that is the head's own (not one of its buttons). */
const headPress = (e: ReactPointerEvent) => e.button === 0 && !(e.target as Element).closest("button, a, input, summary, [data-no-drag]");

export function LandingStrip({ data, error = "", width, height, zoom = 1, open = false, onToggleOpen, onHide, onHeadDown, draggable = true, onResize, onMeasure, now }: LandingStripProps) {
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
  }, []);
  const headDown = (e: ReactPointerEvent) => {
    if (draggable && onHeadDown && headPress(e)) onHeadDown(e);
  };
  const moveHint = draggable && onHeadDown ? "Drag to move the Finished strip" : "";
  const total = landedCount(data);

  return (
    <nav
      ref={rootRef}
      className="sky-landing"
      aria-label="Finished"
      data-open={open ? "" : undefined}
      style={{ width: frameNow.w > 0 ? frameNow.w : undefined, height: frameNow.h > 0 ? frameNow.h : undefined }}
    >
      <div
        className="sky-landing-bar"
        data-sky-head
        title={[moveHint, onToggleOpen ? "double-click to " + (open ? "fold" : "expand") : ""].filter(Boolean).join(" · ") || undefined}
        onPointerDown={headDown}
        onDoubleClick={(e) => {
          if (!onToggleOpen || (e.target as Element).closest("button")) return;
          e.stopPropagation();
          onToggleOpen();
        }}
      >
        <span className="sky-landing-title">Finished</span>
        {total > 0 && (
          <span className="sky-landing-count" title={total + " merged today"}>
            {total} today
          </span>
        )}
        <span className="sky-landing-tools">
          {onToggleOpen && (
            <button
              type="button"
              className="fl-icon-btn sky-landing-expand"
              aria-expanded={open}
              aria-label={open ? "Fold the Finished strip's rows" : "Expand the Finished strip: every finished route"}
              title={open ? "Fold: the first rows of each repo" : "Expand: every finished route"}
              onClick={onToggleOpen}
            >
              {open ? "▾" : "▸"}
            </button>
          )}
          {onHide && (
            <button type="button" className="fl-icon-btn sky-landing-hide" aria-label="Hide the Finished strip" title="Hide the Finished strip — ⋯ › Show the Finished strip brings it back" onClick={onHide}>
              ×
            </button>
          )}
        </span>
      </div>
      <p className="sky-landing-explain">{FINISHED_EXPLAIN}</p>
      <div className="sky-landing-rows">
        {error ? (
          <p className="sky-landing-empty">{error}</p>
        ) : !data ? (
          <p className="sky-landing-empty">Looking at the repos…</p>
        ) : !(data.repos || []).length ? (
          <p className="sky-landing-empty">No repo in the sky yet.</p>
        ) : (
          data.repos.map((r) => <RepoRow key={r.root} repo={r} open={open} now={now} />)
        )}
      </div>
      {onResize && (
        <span
          className="sky-landing-resize skl-resize"
          role="separator"
          tabIndex={0}
          data-no-drag
          aria-label="Resize the Finished strip"
          title="Drag to resize the Finished strip"
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
      <div className="sky-landing-merge" title="The default branch's last merge (or, with none yet, its last commit)">
        {mergeWords(repo.last_merge_at, now, repo.last_merge_kind ?? undefined)}
      </div>
      {(repo.landed || []).length > 0 && (
        <ul className="sky-landing-landed" aria-label={"Merged today in " + repo.name}>
          <li className="sky-landing-landed-cap" aria-hidden="true">
            Merged today
          </li>
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
              <span className="sky-landing-at">{relTime(l.at, { now })}</span>
            </li>
          ))}
          {more > 0 && <li className="sky-landing-more">+{more} more — expand ▸</li>}
        </ul>
      )}
    </section>
  );
}
