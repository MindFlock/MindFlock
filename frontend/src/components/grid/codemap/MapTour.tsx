/** The Map tab's tour: six short coach marks over the real map, each pointing
 * at the part it explains (the tree, the agents column, ⚠ Could break, the
 * rule tools, find). It opens by itself the first time a Map is shown on any
 * of your devices (the synced `map_tour` pref) and is replayable from the
 * toolbar's "? Guide".
 *
 * Non-modal on purpose: the map stays live underneath (the birds keep
 * flying), and a click anywhere outside the card is allowed. Esc or Skip ends
 * it and counts as seen; so does Done. Targets are found by selector inside
 * the tab's root at each step, so a step whose target is absent (no changes
 * yet, a narrow pane) just points at the tree instead. */

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";

export interface TourStep {
  /** CSS selector inside the Map's root; "" = the tree as a whole. */
  target: string;
  title: string;
  body: ReactNode;
}

export const MAP_TOUR: TourStep[] = [
  {
    target: "",
    title: "Your code, drawn as a tree",
    body: (
      <>
        The <b>trunk</b> is the repo, each <b>branch</b> a folder and each <b>leaf</b> a file. <b>Roots</b>{" "}
        underground are the tests, and the piles on the ground are docs and config. Thicker branches hold
        more code.
      </>
    ),
  },
  {
    target: "",
    title: "Bright leaves are what changed",
    body: (
      <>
        Grey is untouched. A <b>glowing leaf</b> is a file an agent changed, and the <b>lit branches</b> lead
        you to it. Labels like <b>web/core ●4</b> count the changes in that folder. Click a leaf or folder for
        its card.
      </>
    ),
  },
  {
    target: '[data-hud="left"]',
    title: "Each agent is a bird",
    body: (
      <>
        This column lists the agents working on this repo: what each is doing now and what it changed. A bird
        <b> nests</b> where it mostly works. Click a row to follow its bird around the tree.
      </>
    ),
  },
  {
    target: ".ct-bix-risk-tog",
    title: "What could break",
    body: (
      <>
        Hover a bright leaf and the files that <b>import</b> it light up <b>gold</b>. <b>⚠ Could break</b>{" "}
        ranks the changed files by how much depends on them, so you know what to review first.
      </>
    ),
  },
  {
    target: ".ct-rules",
    title: "Fence agents in or out",
    body: (
      <>
        <b>⛔ Keep out</b>: agents may read a folder but never edit it. <b>✓ Only here</b>: they may edit only
        inside it. Pick one, then click a folder on the tree. Every changed folder in the column has the same
        two buttons.
      </>
    ),
  },
  {
    target: ".cm-search",
    title: "Getting around",
    body: (
      <>
        Press <kbd>/</kbd> to find a file or function. Scroll to zoom, drag to pan, double-click a folder to
        zoom into it, and <b>⌂ Whole tree</b> brings you back. <b>?</b> beside Agents is the legend.
      </>
    ),
  },
];

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

const CARD_W = 320;
const GAP = 12;

/** Where the card goes for a target rect inside a W×H root: beside a tall
 * target on the left, else below it, else above it; always clamped inside.
 * (Pure, for the tests.) */
export function placeCard(t: Rect | null, W: number, H: number, cardH: number): { left: number; top: number } {
  const cw = Math.min(CARD_W, W - 16);
  const clampX = (x: number) => Math.max(8, Math.min(x, W - cw - 8));
  const clampY = (y: number) => Math.max(8, Math.min(y, H - cardH - 8));
  if (!t) return { left: clampX((W - cw) / 2), top: clampY((H - cardH) / 2) };
  // a column on the left (the agents list): to its right
  if (t.x + t.w + GAP + cw <= W - 8 && t.h > H * 0.4) return { left: t.x + t.w + GAP, top: clampY(t.y) };
  // below
  if (t.y + t.h + GAP + cardH <= H - 8) return { left: clampX(t.x), top: t.y + t.h + GAP };
  // above
  if (t.y - GAP - cardH >= 8) return { left: clampX(t.x), top: t.y - GAP - cardH };
  return { left: clampX((W - cw) / 2), top: clampY((H - cardH) / 2) };
}

export function MapTour({
  rootRef,
  onClose,
  steps = MAP_TOUR,
}: {
  rootRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  steps?: TourStep[];
}) {
  const [i, setI] = useState(0);
  const [rect, setRect] = useState<Rect | null>(null);
  const [box, setBox] = useState({ W: 0, H: 0 });
  const [cardH, setCardH] = useState(180);
  const cardRef = useRef<HTMLDivElement | null>(null);
  const step = steps[Math.min(i, steps.length - 1)];
  const last = i >= steps.length - 1;

  const measure = useCallback(() => {
    const root = rootRef.current;
    if (!root) return;
    const r0 = root.getBoundingClientRect();
    setBox({ W: r0.width, H: r0.height });
    const el = step.target ? (root.querySelector(step.target) as HTMLElement | null) : null;
    const r = el ? el.getBoundingClientRect() : null;
    setRect(r && r.width > 0 && r.height > 0 ? { x: r.left - r0.left, y: r.top - r0.top, w: r.width, h: r.height } : null);
    if (cardRef.current) setCardH(cardRef.current.offsetHeight || 180);
  }, [rootRef, step.target]);

  useLayoutEffect(measure, [measure, i]);
  useEffect(() => {
    const root = rootRef.current;
    if (!root || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => measure());
    ro.observe(root);
    return () => ro.disconnect();
  }, [rootRef, measure]);
  useEffect(() => {
    cardRef.current?.focus();
  }, [i]);

  const onKey = (e: React.KeyboardEvent) => {
    // the Map's own keys (Esc closes panels, arrows pan) must not fire under the tour
    e.stopPropagation();
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    } else if (e.key === "ArrowRight" || e.key === "Enter") {
      e.preventDefault();
      if (last) onClose();
      else setI(i + 1);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      if (i > 0) setI(i - 1);
    }
  };

  const pos = placeCard(rect, box.W, box.H, cardH);
  return (
    <div className="mt-layer" aria-live="polite">
      {rect && (
        <div
          className="mt-ring"
          aria-hidden="true"
          style={{ left: rect.x - 4, top: rect.y - 4, width: rect.w + 8, height: rect.h + 8 }}
        />
      )}
      <div
        ref={cardRef}
        className="mt-card"
        role="dialog"
        aria-modal="false"
        aria-labelledby="mt-title"
        tabIndex={-1}
        style={{ left: pos.left, top: pos.top }}
        onKeyDown={onKey}
      >
        <div className="mt-head">
          <span className="mt-step">
            {i + 1} of {steps.length}
          </span>
          <button type="button" className="mt-skip" onClick={onClose}>
            Skip tour
          </button>
        </div>
        <h3 id="mt-title">{step.title}</h3>
        <p className="mt-body">{step.body}</p>
        <div className="mt-foot">
          <span className="mt-dots" aria-hidden="true">
            {steps.map((_, k) => (
              <i key={k} className={k === i ? "on" : ""} />
            ))}
          </span>
          <button type="button" className="cm-btn mt-back" disabled={i === 0} onClick={() => setI(i - 1)}>
            Back
          </button>
          <button type="button" className="cm-btn on mt-next" onClick={() => (last ? onClose() : setI(i + 1))}>
            {last ? "Done" : "Next"}
          </button>
        </div>
      </div>
    </div>
  );
}
