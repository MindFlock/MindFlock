/** The Flights glyph: a small bird in flight (a flight is a flock's, never a
 * plane's). currentColor, ~16px, inline wherever a flight or a flyway is
 * named — the top bar, the pane strip, the Board, the tabs. */

import type { ReactNode } from "react";

export function Bird({ size = 16, className = "" }: { size?: number; className?: string }) {
  return (
    <svg
      className={"fl-bird" + (className ? " " + className : "")}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {/* A gull on the wing: two swept wings meeting at the body. */}
      <path d="M2 9.5c3.4-2.8 6.8-2.3 10 2 3.2-4.3 6.6-4.8 10-2" />
    </svg>
  );
}

/** The bird on a line (the Sky, the pane's Flight tab): the glyph, bigger,
 * standing on the box the flight is at. It bobs while the flock works, sits
 * gold and still when perched on you, red when halted; placed by `left` /
 * `top` so a move to the next box is a hop (a CSS transition), not a jump. */
export function BirdMarker({ tone, x, y, label }: { tone: string; x: number; y: number; label: string }) {
  return (
    <span className={"fl-bird-marker tone-" + tone} style={{ left: x, top: y }} role="img" aria-label={label} title={label} data-tour="flights-bird">
      <Bird size={22} className="fl-bird-big" />
    </span>
  );
}

/** A stop's icon by its kind (the palette, the boxes, the inspector's head).
 * Stroke icons in the pane tabs' hand; a perch is the bird on its branch. */
export function StopIcon({ kind, size = 15 }: { kind: string; size?: number }) {
  if (kind === "you" || kind === "perch")
    return (
      <svg className="fl-stop-icon fl-stop-icon-perch" width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="M9 11.5a3.5 3.5 0 0 1 6.6-1.6l2.4.6-2.1 1.2A4.5 4.5 0 0 1 11 16H9.5" fill="currentColor" />
        <path d="M3 18.5h18M15 18.5l2.5-2.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
    );
  const paths: Record<string, ReactNode> = {
    agent: <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" />,
    session: (
      <>
        <rect x="3" y="4" width="18" height="16" rx="2.5" />
        <polyline points="7 10 10 13 7 16" />
        <line x1="12" y1="16" x2="16" y2="16" />
      </>
    ),
    ship: (
      <>
        <path d="M12 16V4" />
        <polyline points="7 9 12 4 17 9" />
        <path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" />
      </>
    ),
    verify: (
      <>
        <rect x="3.5" y="3.5" width="17" height="17" rx="3" />
        <polyline points="8 12.5 11 15.5 16.5 9" />
      </>
    ),
    wait: (
      <>
        <circle cx="12" cy="12" r="8.5" />
        <polyline points="12 7.5 12 12 15 14" />
      </>
    ),
    reviews: (
      <>
        <path d="M20 14a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z" />
        <line x1="8" y1="9" x2="16" y2="9" />
        <line x1="8" y1="12.5" x2="13" y2="12.5" />
      </>
    ),
    checks: (
      <>
        <path d="M12 3l7.5 3v5.5c0 4.4-3.2 8.2-7.5 9.5-4.3-1.3-7.5-5.1-7.5-9.5V6z" />
        <polyline points="8.5 12 11 14.5 15.5 10" />
      </>
    ),
    command: (
      <>
        <polyline points="4 17 10 11 4 5" />
        <line x1="12" y1="19" x2="20" y2="19" />
      </>
    ),
    ticket: (
      <>
        <path d="M3 8a2 2 0 0 0 0 4v0a2 2 0 0 0 0 4v2h18v-2a2 2 0 0 1 0-4 2 2 0 0 1 0-4V6H3z" />
        <line x1="14" y1="6" x2="14" y2="18" strokeDasharray="2 2" />
      </>
    ),
    notify: (
      <>
        <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
        <path d="M13.7 21a2 2 0 0 1-3.4 0" />
      </>
    ),
    add: (
      <>
        <line x1="12" y1="5" x2="12" y2="19" />
        <line x1="5" y1="12" x2="19" y2="12" />
      </>
    ),
  };
  return (
    <svg
      className="fl-stop-icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {paths[kind] || <circle cx="12" cy="12" r="6" />}
    </svg>
  );
}
