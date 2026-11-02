/** The Flights glyph and the bird on a line: the MindFlock bird — the raven
 * of the website's hero and the app's flock (lib/flock.ts, `/bird.png`), not
 * a drawn gull. The sprite is one frame, white stripped to alpha, so it is
 * used as a CSS MASK over `currentColor`: the bird takes the colour of
 * wherever it sits (the top bar, the pane strip, the Sky, a tone). Its wings
 * are the part above the hinge line, so the marker flaps by scaling that band
 * about the hinge — the same beat flock.ts draws on its canvas. */

import type { CSSProperties, HTMLAttributes, ReactNode } from "react";
import "./SkyFlock.css";

/** The sprite (backend/web/static/bird.png) and its wing hinge, as flock.ts
 * reads them. */
export const SPRITE_URL = "/bird.png";
export const SPRITE_W = 216;
export const SPRITE_H = 160;
export const HINGE = 0.56;

/** The sprite at `width` px: its height, and the wing band (above the hinge)
 * and body band (below it) the marker draws as two masked halves. */
export function spriteBox(width: number): { w: number; h: number; wings: number; body: number } {
  const h = Math.round(((width * SPRITE_H) / SPRITE_W) * 10) / 10;
  const wings = Math.round(h * HINGE * 10) / 10;
  return { w: width, h, wings, body: Math.round((h - wings) * 10) / 10 };
}

/** `currentColor` through the sprite: the mask, at `size` / `position`. */
export function spriteMask(size: string, position: string): CSSProperties {
  const img = "url(" + SPRITE_URL + ")";
  return {
    backgroundColor: "currentColor",
    WebkitMaskImage: img,
    maskImage: img,
    WebkitMaskRepeat: "no-repeat",
    maskRepeat: "no-repeat",
    WebkitMaskSize: size,
    maskSize: size,
    WebkitMaskPosition: position,
    maskPosition: position,
  };
}

/** The viewer asked for no motion (false on a server render). */
export function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** The still bird, ~16px, inline wherever a flight or a flyway is named. */
export function Bird({ size = 16, className = "" }: { size?: number; className?: string }) {
  return (
    <svg
      className={"fl-bird" + (className ? " " + className : "")}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      style={spriteMask("contain", "center")}
      aria-hidden="true"
      focusable="false"
    />
  );
}

/** The marker's look for a tone: the class the canvas colours it by
 * (FlywayCanvas.css: working green, `you` gold, halted red, paused grey), the
 * sprite's class, and whether its wings beat — only while the flock works,
 * and never for a viewer who asked for no motion. */
export function birdLook(tone: string, reduced = prefersReducedMotion()): { className: string; sprite: string; flaps: boolean } {
  const flaps = tone === "working" && !reduced;
  return { className: "fl-bird-marker tone-" + tone, sprite: "fl-bird-big fl-sprite-bird" + (flaps ? " fl-flapping" : ""), flaps };
}

/** The bird in hand (round 12): what a press on it says (`title`, lib/
 * stopEditing `birdAction`) and the pointer / key handlers the canvas drives
 * its click and its drag with. Without it the bird is a picture. */
export type BirdHand = { title: string; className?: string } & Pick<
  HTMLAttributes<HTMLSpanElement>,
  "onClick" | "onKeyDown" | "onPointerDown" | "onPointerMove" | "onPointerUp" | "onPointerCancel" | "onContextMenu"
>;

/** The bird on a line (the Sky, the pane's Flight tab), standing on the box
 * the flight is at: flapping while the flock works, still and gold when
 * perched on you, red when halted, grey when paused. Placed by `left` / `top`
 * so a move to the next box is a hop (a CSS transition), not a jump. With a
 * `hand` it is a button: a click halts / carries on / retries, a drag jumps
 * the flight (FlywayCanvas). Its name stays where it is; what a press does is
 * its title and its description. */
export function BirdMarker({ tone, x, y, label, size = 24, hand, className = "", formation = false }: { tone: string; x: number; y: number; label: string; size?: number; hand?: BirdHand; className?: string; /** Round 15: flying in the V behind the leader (`data-formation`). */ formation?: boolean }) {
  const look = birdLook(tone);
  const box = spriteBox(size);
  const whole = box.w + "px " + box.h + "px";
  const { title, className: held, ...on } = hand || { title: label };
  const cls = [look.className, className, held].filter(Boolean).join(" ");
  return (
    <span
      className={cls}
      style={{ left: x, top: y }}
      role={hand ? "button" : "img"}
      aria-label={label}
      aria-description={hand ? title : undefined}
      tabIndex={hand ? 0 : undefined}
      title={title}
      data-tour="flights-bird"
      data-formation={formation ? "v" : undefined}
      {...on}
    >
      <span className={look.sprite} style={{ width: box.w, height: box.h }}>
        <span className="fl-sprite-wings" style={{ top: 0, height: box.wings, ...spriteMask(whole, "0 0") }} />
        <span className="fl-sprite-body" style={{ top: box.wings, height: box.body, ...spriteMask(whole, "0 " + -box.wings + "px") }} />
      </span>
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
    // What has flown: two ticks, one behind the other.
    flown: (
      <>
        <polyline points="2.5 12.5 6.5 16.5 14 8" />
        <polyline points="10 15.5 11 16.5 21.5 6" />
      </>
    ),
    // The line splits into workers, and comes back together.
    split: (
      <>
        <path d="M4 12h5" />
        <path d="M9 12c3 0 4-6 8-6h3" />
        <path d="M9 12c3 0 4 6 8 6h3" />
      </>
    ),
    join: (
      <>
        <path d="M4 6h3c4 0 5 6 8 6" />
        <path d="M4 18h3c4 0 5-6 8-6" />
        <path d="M15 12h5" />
      </>
    ),
    // --- Round 15's stops (docs/flights.md "The new stops") -------------------
    // A flask: the test run.
    test: (
      <>
        <path d="M9.5 3h5" />
        <path d="M10 3v6.2L5 18.5A1.8 1.8 0 0 0 6.6 21h10.8a1.8 1.8 0 0 0 1.6-2.5L14 9.2V3" />
        <line x1="7.5" y1="15.5" x2="16.5" y2="15.5" />
      </>
    ),
    // Two arrows merging into one: sync with the base branch.
    sync: (
      <>
        <path d="M4 6h4c3 0 4 6 8 6h4" />
        <path d="M4 18h4c3 0 4-6 8-6" />
        <polyline points="17 9 20 12 17 15" />
      </>
    ),
    // A flag planted: the checkpoint.
    checkpoint: (
      <>
        <line x1="6" y1="21" x2="6" y2="3.5" />
        <path d="M6 4.5h11.5l-2.5 4 2.5 4H6" />
      </>
    ),
    // A plane coming down onto the strip: land.
    land: (
      <>
        <path d="M3.5 9.5l3-1 4 3.5 6.5-2.3a1.6 1.6 0 0 1 1.1 3L8 16.5l-3.5-1" />
        <line x1="4" y1="20" x2="20" y2="20" />
      </>
    ),
    // A coin: the budget.
    budget: (
      <>
        <circle cx="12" cy="12" r="8.5" />
        <path d="M14.5 9.3c-.5-.9-1.4-1.3-2.5-1.3-1.6 0-2.6.8-2.6 1.9 0 2.6 5.3 1.3 5.3 4 0 1.2-1.1 2.1-2.7 2.1-1.3 0-2.3-.5-2.8-1.5" />
        <line x1="12" y1="6" x2="12" y2="18" />
      </>
    ),
    // An eye: the review.
    review: (
      <>
        <path d="M2.5 12s3.5-6.5 9.5-6.5 9.5 6.5 9.5 6.5-3.5 6.5-9.5 6.5S2.5 12 2.5 12z" />
        <circle cx="12" cy="12" r="2.8" />
      </>
    ),
    // Scales: the judge.
    judge: (
      <>
        <line x1="12" y1="4" x2="12" y2="20" />
        <line x1="8" y1="20" x2="16" y2="20" />
        <line x1="5" y1="7" x2="19" y2="7" />
        <path d="M2.5 13l2.5-6 2.5 6a2.5 2.5 0 0 1-5 0z" />
        <path d="M16.5 13l2.5-6 2.5 6a2.5 2.5 0 0 1-5 0z" />
      </>
    ),
    // A plug with the arrow out: the call.
    call: (
      <>
        <path d="M7 8V4.5M11 8V4.5" />
        <path d="M4.5 8h9v3a4.5 4.5 0 0 1-9 0z" />
        <path d="M9 15.5V20" />
        <path d="M15 12h5.5" />
        <polyline points="18 9.5 20.5 12 18 14.5" />
      </>
    ),
    // An antenna: listen for the webhook.
    listen: (
      <>
        <line x1="12" y1="11" x2="12" y2="21" />
        <circle cx="12" cy="9.5" r="1.6" />
        <path d="M8.5 6a5 5 0 0 0 0 7" />
        <path d="M15.5 6a5 5 0 0 1 0 7" />
        <path d="M5.5 3.5a9 9 0 0 0 0 12" />
        <path d="M18.5 3.5a9 9 0 0 1 0 12" />
      </>
    ),
    // A person: someone else.
    someone: (
      <>
        <circle cx="12" cy="8" r="3.6" />
        <path d="M4.5 20.5c.8-4 3.9-6 7.5-6s6.7 2 7.5 6" />
      </>
    ),
    // A monitor with a globe on it: the preview.
    preview: (
      <>
        <rect x="3" y="4" width="18" height="12" rx="2" />
        <path d="M9 20h6M12 16v4" />
        <circle cx="12" cy="10" r="3.6" />
        <path d="M8.4 10h7.2M12 6.4c1.6 1.8 1.6 5.4 0 7.2M12 6.4c-1.6 1.8-1.6 5.4 0 7.2" />
      </>
    ),
    // ↻ — an open circle with its arrowhead: the Loop (round 16).
    loop: (
      <>
        <path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3" />
        <polyline points="18.5 3 18.5 7.2 14.3 7.2" />
      </>
    ),
    // A camera: the screenshot.
    screenshot: (
      <>
        <path d="M4 8.5h3l1.6-2.5h6.8L17 8.5h3a1.5 1.5 0 0 1 1.5 1.5v8A1.5 1.5 0 0 1 20 19.5H4A1.5 1.5 0 0 1 2.5 18v-8A1.5 1.5 0 0 1 4 8.5z" />
        <circle cx="12" cy="13.5" r="3.2" />
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
