/** The picture in one breath, drawn: session → route → Needs you — three
 * tiny inline sketches with their captions (lib/onboarding VISION_PARTS).
 * Shown by the welcome card, Settings → Help → How MindFlock works and the
 * empty Sky. Inline SVG in `currentColor` and the theme tokens (Vision.css),
 * so light and dark both work; no image files. */

import { VISION_PARTS } from "../../lib/onboarding";
import "./Vision.css";

function SessionSketch() {
  // A pane: its bar, the agent's lines, and a branch forking off below.
  return (
    <svg className="vp-sketch" viewBox="0 0 88 56" aria-hidden="true">
      <rect className="vp-pane" x={4} y={4} width={80} height={48} rx={6} />
      <rect className="vp-bar" x={4} y={4} width={80} height={11} rx={6} />
      <circle className="vp-dot-accent" cx={12} cy={9.5} r={2.4} />
      <path className="vp-ink" d="M12 25 H52 M12 33 H64 M12 41 H40" />
      <path className="vp-ink vp-thin" d="M70 46 V30 M70 38 Q70 30 78 28" />
    </svg>
  );
}

function RouteSketch() {
  // A row of stops — agent, checks, your OK, the PR — the bird on the second.
  const xs = [4, 34, 64, 94];
  return (
    <svg className="vp-sketch" viewBox="0 0 120 56" aria-hidden="true">
      {xs.slice(1).map((x) => (
        <line key={x} className="vp-ink vp-thin" x1={x - 8} y1={34} x2={x} y2={34} />
      ))}
      {xs.map((x, i) => (
        <rect key={x} className={"vp-stop" + (i === 0 ? " vp-stop-agent" : i === 1 ? " vp-stop-here" : "")} x={x} y={24} width={22} height={20} rx={4} />
      ))}
      <path className="vp-bird" d="M37 15 Q41 9 45 15 Q49 9 53 15" />
      <path className="vp-ink vp-thin" d="M100 34 l3 3 l6 -7" />
    </svg>
  );
}

function NeedsSketch() {
  // The bell, with the gold dot of something waiting on you.
  return (
    <svg className="vp-sketch" viewBox="0 0 64 56" aria-hidden="true">
      <path className="vp-bell" d="M20 40 V28 a12 12 0 0 1 24 0 V40 l4 4 H16 Z" />
      <path className="vp-ink vp-thin" d="M28 48 a4 4 0 0 0 8 0" />
      <circle className="vp-badge" cx={45} cy={18} r={6} />
    </svg>
  );
}

const SKETCH = { session: SessionSketch, route: RouteSketch, needs: NeedsSketch } as const;

export function VisionPicture({ compact = false }: { compact?: boolean }) {
  return (
    <figure
      className={"vision-picture" + (compact ? " is-compact" : "")}
      aria-label="A session is one agent; its route is the stops its work goes through; Needs you is what waits on you"
    >
      {VISION_PARTS.map((p, i) => {
        const Sketch = SKETCH[p.id];
        return (
          <div className="vp-part" key={p.id} data-part={p.id}>
            {i > 0 && (
              <span className="vp-arrow" aria-hidden="true">
                →
              </span>
            )}
            <div className="vp-cell">
              <Sketch />
              <b className="vp-title">{p.title}</b>
              <span className="vp-line">{p.line}</span>
            </div>
          </div>
        );
      })}
    </figure>
  );
}
