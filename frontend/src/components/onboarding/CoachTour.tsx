/** The coach-mark tour: a ring around one live control and a small card beside
 * it saying what it is for — Back / Next / Done, "2 of 4", Skip. One framework
 * for every tour: the top-bar dialogs' and the Welcome tour (lib/tours, shown
 * by CoachTourHost below from the store's `coachTour`) and the Map tab's
 * (grid/codemap/MapTour.tsx, contained in the Map's own root).
 *
 * Non-modal on purpose: what it points at stays live and clickable (the layer
 * and ring take no pointer events; only the card does), and a click anywhere
 * else is allowed. Esc or Skip ends it, and so does Done; the caller's
 * `onClose` is where "seen" is remembered, so every exit counts. Arrow keys
 * page while the card has focus.
 *
 * Targets are looked up at each step (and re-measured on resize, and every
 * few hundred ms while open — a dialog's rows land after it opens), so a step
 * whose target is absent (an empty list, a fold that isn't open, a hook a
 * surface hasn't added yet) centres the card and still shows the step.
 *
 * Two placements:
 *  - `rootRef` given: targets are found INSIDE that element and the card is
 *    positioned absolutely in it (the Map, whose pane it must not leave).
 *  - no `rootRef`: targets are found in `document`, and the layer is fixed to
 *    the viewport, portalled to <body> above every open `.modal` (which carry
 *    no z-index of their own) and under the bell, toasts and banners. */

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";
import { useUi } from "../../state/store";
import { TOURS } from "../../lib/tours";
import "./CoachTour.css";

export interface CoachStep {
  /** CSS selector (a list is fine: first match wins); "" = nothing to point at. */
  target: string;
  title: string;
  body: ReactNode;
}

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

const CARD_W = 320;
const GAP = 12;

/** Where the card goes for a target rect inside a W×H box: beside a tall
 * target on its left, else below it, else above it, else beside it on the
 * left; centred with no target; always clamped inside. (Pure, for the tests.) */
export function placeCard(t: Rect | null, W: number, H: number, cardH: number): { left: number; top: number } {
  const cw = Math.min(CARD_W, W - 16);
  const clampX = (x: number) => Math.max(8, Math.min(x, W - cw - 8));
  const clampY = (y: number) => Math.max(8, Math.min(y, H - cardH - 8));
  if (!t) return { left: clampX((W - cw) / 2), top: clampY((H - cardH) / 2) };
  // a tall column on the left (a list, a nav): to its right
  if (t.x + t.w + GAP + cw <= W - 8 && t.h > H * 0.4) return { left: t.x + t.w + GAP, top: clampY(t.y) };
  // below
  if (t.y + t.h + GAP + cardH <= H - 8) return { left: clampX(t.x), top: t.y + t.h + GAP };
  // above
  if (t.y - GAP - cardH >= 8) return { left: clampX(t.x), top: t.y - GAP - cardH };
  // a big target that fills the height: beside it, right then left
  if (t.x + t.w + GAP + cw <= W - 8) return { left: t.x + t.w + GAP, top: clampY(t.y) };
  if (t.x - GAP - cw >= 8) return { left: t.x - GAP - cw, top: clampY(t.y) };
  return { left: clampX((W - cw) / 2), top: clampY((H - cardH) / 2) };
}

/** The first VISIBLE match of `selector` under `root` (a selector list
 * matches in document order, and a hidden duplicate must not win), or null.
 * A broken selector is "nothing to point at", never a crash. */
export function findTarget(root: ParentNode, selector: string): HTMLElement | null {
  if (!selector) return null;
  let all: NodeListOf<HTMLElement>;
  try {
    all = root.querySelectorAll<HTMLElement>(selector);
  } catch {
    return null;
  }
  for (const el of Array.from(all)) {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) return el;
  }
  return null;
}

export interface CoachTourProps {
  steps: CoachStep[];
  /** Every exit: Done, Skip, Esc. */
  onClose(): void;
  /** Contain the tour in this element (see the header); omit for the page. */
  rootRef?: RefObject<HTMLElement | null>;
  /** Controlled step (the store's), with its setter; uncontrolled if omitted. */
  step?: number;
  onStep?(step: number): void;
  /** Class-name prefix for the parts (`<prefix>-card`, …). */
  prefix?: string;
  /** Extra class on Back / Next. */
  buttonClass?: string;
  /** The Skip link's words. */
  skipLabel?: string;
  /** Accessible name source id. */
  labelId?: string;
}

export function CoachTour({
  steps,
  onClose,
  rootRef,
  step: controlled,
  onStep,
  prefix = "coach",
  buttonClass = "coach-btn",
  skipLabel = "Skip tour",
  labelId = prefix + "-title",
}: CoachTourProps) {
  const [own, setOwn] = useState(0);
  const i = Math.max(0, Math.min(controlled ?? own, steps.length - 1));
  const go = (n: number) => (onStep ? onStep(n) : setOwn(n));
  const [rect, setRect] = useState<Rect | null>(null);
  const [box, setBox] = useState({ W: 0, H: 0 });
  const [cardH, setCardH] = useState(180);
  const cardRef = useRef<HTMLDivElement | null>(null);
  const step = steps[i];
  const last = i >= steps.length - 1;
  const contained = !!rootRef;

  const measure = useCallback(() => {
    if (!step) return;
    let x0 = 0;
    let y0 = 0;
    let W: number;
    let H: number;
    let scope: ParentNode;
    if (rootRef) {
      const root = rootRef.current;
      if (!root) return;
      const r0 = root.getBoundingClientRect();
      x0 = r0.left;
      y0 = r0.top;
      W = r0.width;
      H = r0.height;
      scope = root;
    } else {
      if (typeof window === "undefined") return;
      W = window.innerWidth;
      H = window.innerHeight;
      scope = document;
    }
    setBox((b) => (b.W === W && b.H === H ? b : { W, H }));
    const el = findTarget(scope, step.target);
    const r = el ? el.getBoundingClientRect() : null;
    const next = r ? { x: r.left - x0, y: r.top - y0, w: r.width, h: r.height } : null;
    setRect((p) =>
      p === next || (p && next && p.x === next.x && p.y === next.y && p.w === next.w && p.h === next.h) ? p : next
    );
    if (cardRef.current) {
      const h = cardRef.current.offsetHeight || 180;
      setCardH((p) => (p === h ? p : h));
    }
  }, [rootRef, step]);

  useLayoutEffect(measure, [measure, i]);
  useEffect(() => {
    const root = rootRef?.current;
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => measure()) : null;
    if (root && ro) ro.observe(root);
    if (contained) return () => ro?.disconnect();
    // The page: follow the window, any scroll, and targets that render late
    // or move as a dialog fills in.
    const onMove = () => measure();
    window.addEventListener("resize", onMove);
    window.addEventListener("scroll", onMove, true);
    const t = window.setInterval(measure, 400);
    return () => {
      ro?.disconnect();
      window.removeEventListener("resize", onMove);
      window.removeEventListener("scroll", onMove, true);
      window.clearInterval(t);
    };
  }, [rootRef, contained, measure]);

  // Focus the card on each step (so the arrows and Esc reach it) and hand
  // focus back to wherever it was when the tour ends.
  const backTo = useRef<Element | null>(null);
  useEffect(() => {
    backTo.current = typeof document !== "undefined" ? document.activeElement : null;
    return () => {
      const el = backTo.current as HTMLElement | null;
      if (el && el.isConnected && typeof el.focus === "function") el.focus();
    };
  }, []);
  useEffect(() => {
    cardRef.current?.focus({ preventScroll: true });
  }, [i]);

  if (!step) return null;

  const onKey = (e: React.KeyboardEvent) => {
    // The surface's own keys (a dialog's Esc, the Map's arrows) must not fire
    // under the tour: stopping it here keeps the native event from reaching
    // the window listeners (components/Modal.tsx) too.
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      onClose();
    } else if (e.key === "ArrowRight" || (e.key === "Enter" && e.target === e.currentTarget)) {
      e.preventDefault();
      e.stopPropagation();
      if (last) onClose();
      else go(i + 1);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      e.stopPropagation();
      if (i > 0) go(i - 1);
    }
  };

  const pos = placeCard(rect, box.W, box.H, cardH);
  const c = (part: string) => prefix + "-" + part;
  const layer = (
    <div className={c("layer")} aria-live="polite">
      {rect && (
        <div
          className={c("ring")}
          aria-hidden="true"
          style={{ left: rect.x - 4, top: rect.y - 4, width: rect.w + 8, height: rect.h + 8 }}
        />
      )}
      <div
        ref={cardRef}
        className={c("card") + (rect ? "" : " is-centred")}
        role="dialog"
        aria-modal="false"
        aria-labelledby={labelId}
        tabIndex={-1}
        style={{ left: pos.left, top: pos.top }}
        onKeyDown={onKey}
      >
        <div className={c("head")}>
          <span className={c("step")}>
            {i + 1} of {steps.length}
          </span>
          <button type="button" className={c("skip")} onClick={onClose}>
            {skipLabel}
          </button>
        </div>
        <h3 id={labelId}>{step.title}</h3>
        <p className={c("body")}>{step.body}</p>
        <div className={c("foot")}>
          <span className={c("dots")} aria-hidden="true">
            {steps.map((_, k) => (
              <i key={k} className={k === i ? "on" : ""} />
            ))}
          </span>
          <button type="button" className={buttonClass + " " + c("back")} disabled={i === 0} onClick={() => go(i - 1)}>
            Back
          </button>
          <button
            type="button"
            className={buttonClass + " on " + c("next")}
            onClick={() => (last ? onClose() : go(i + 1))}
          >
            {last ? "Done" : "Next"}
          </button>
        </div>
      </div>
    </div>
  );
  return contained || typeof document === "undefined" ? layer : createPortal(layer, document.body);
}

/** The page's one coach tour: whatever `useUi().coachTour` names, from
 * lib/tours. Mounted once in App. */
export function CoachTourHost() {
  const tour = useUi((s) => s.coachTour);
  const setCoachStep = useUi((s) => s.setCoachStep);
  const closeCoachTour = useUi((s) => s.closeCoachTour);
  if (!tour) return null;
  const def = TOURS[tour.id];
  if (!def) return null;
  return (
    <CoachTour
      key={tour.id}
      steps={def.steps}
      step={tour.step}
      onStep={setCoachStep}
      onClose={closeCoachTour}
      labelId="coach-title"
    />
  );
}
