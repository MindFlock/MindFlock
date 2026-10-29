/** The Sky's ambient flock: a few MindFlock birds crossing the empty sky
 * behind the plane — the same raven sprite, species plumage and wing beat as
 * the app's flock (lib/flock.ts), but a handful of them (~8, `density` scales
 * it) rather than a murmuration, steering gently round the middle of the sky
 * where the lines are. Decoration only:
 *
 *  - one rAF, capped at 60 fps, and no work at all while the tab is hidden;
 *  - `paused` = no rAF (the last frame stays painted);
 *  - `prefers-reduced-motion` = nothing at all (no canvas);
 *  - DPR-aware, `pointer-events: none`, quieter in the light theme
 *    (SkyFlock.css).
 *
 * The simulation (`flockSize`, `seedBirds`, `stepBirds`) and the frame loop
 * (`skyLoop`) are pure, so the tests drive them without a DOM. */

import { useEffect, useRef, useState, type JSX } from "react";
import { tinted } from "../../lib/flock";
import { HINGE, SPRITE_H, SPRITE_W, prefersReducedMotion } from "./Bird";
import "./SkyFlock.css";

/** flock.ts's species — the app's bird-named theme presets: dark plumage,
 * light plumage. */
const SPECIES: ReadonlyArray<readonly [string, string]> = [
  ["#7d56f4", "#422d8a"],
  ["#3d8bfd", "#144791"],
  ["#18b3dd", "#095c73"],
  ["#2ec2b3", "#09635b"],
  ["#44b556", "#1a5825"],
  ["#e8b71e", "#675007"],
  ["#a8c332", "#48560c"],
  ["#f07b3c", "#8a3d0d"],
  ["#d444f1", "#70148a"],
  ["#e5484d", "#841419"],
  ["#ee5d8f", "#8c1e47"],
  ["#97a1b5", "#3d4554"],
];

export const FRAME_MS = 1000 / 60;
const BASE_BIRDS = 8;
const MAX_BIRDS = 40;

export interface SkyBird {
  x: number;
  y: number;
  vx: number;
  vy: number;
  species: number;
  size: number;
  phase: number;
  freq: number;
  /** Wander target, and the frames left before a new one is picked. */
  tx: number;
  ty: number;
  tt: number;
}

/** How many birds a sky of this size flies: ~8, times `density`. */
export function flockSize(width: number, height: number, density = 1): number {
  if (!(width > 0) || !(height > 0) || !(density > 0)) return 0;
  return Math.max(1, Math.min(MAX_BIRDS, Math.round(BASE_BIRDS * density)));
}

function retarget(b: SkyBird, w: number, h: number, rand: () => number) {
  b.tx = rand() * w;
  b.ty = rand() * h;
  b.tt = 300 + rand() * 500;
}

/** `n` birds scattered over the sky, each heading somewhere. */
export function seedBirds(n: number, w: number, h: number, rand: () => number = Math.random): SkyBird[] {
  const out: SkyBird[] = [];
  for (let i = 0; i < n; i++) {
    const a = rand() * Math.PI * 2;
    const b: SkyBird = {
      x: rand() * w,
      y: rand() * h,
      vx: Math.cos(a) * 0.8,
      vy: Math.sin(a) * 0.8,
      species: Math.floor(rand() * SPECIES.length),
      size: 1.1 + rand() * 0.7,
      phase: rand() * Math.PI * 2,
      freq: 0.07 + rand() * 0.05,
      tx: 0,
      ty: 0,
      tt: 0,
    };
    retarget(b, w, h, rand);
    out.push(b);
  }
  return out;
}

/** The flock resized to `n` birds inside a new sky: the ones kept stay where
 * they are (folded inside it), so a resize never teleports the flock. */
export function fitBirds(birds: SkyBird[], n: number, w: number, h: number, rand: () => number = Math.random): SkyBird[] {
  const kept = birds.slice(0, n);
  for (const b of kept) {
    b.x = ((b.x % w) + w) % w;
    b.y = ((b.y % h) + h) % h;
  }
  return n > kept.length ? kept.concat(seedBirds(n - kept.length, w, h, rand)) : kept;
}

/** One frame of flight: casual flocking (a wide radius — there are few of
 * them), a wander target each, a gentle push out of the middle of the sky,
 * cruising speed clamped, wrapping at the edges so they cross the sky rather
 * than turn back. */
export function stepBirds(birds: SkyBird[], w: number, h: number, rand: () => number = Math.random): void {
  const R2 = 90 * 90;
  const SEP = 22 * 22;
  const cx = w / 2;
  const cy = h / 2;
  const calm = Math.min(w, h) * 0.3;
  for (const b of birds) {
    let ax = 0;
    let ay = 0;
    let mx = 0;
    let my = 0;
    let sx = 0;
    let sy = 0;
    let n = 0;
    for (const o of birds) {
      if (o === b) continue;
      const dx = o.x - b.x;
      const dy = o.y - b.y;
      const d2 = dx * dx + dy * dy;
      if (d2 >= R2) continue;
      mx += o.x;
      my += o.y;
      ax += o.vx;
      ay += o.vy;
      n++;
      if (d2 < SEP && d2 > 0) {
        sx -= dx / d2;
        sy -= dy / d2;
      }
    }
    if (n) {
      b.vx += (mx / n - b.x) * 0.0006 + (ax / n - b.vx) * 0.02 + sx * 2.5;
      b.vy += (my / n - b.y) * 0.0006 + (ay / n - b.vy) * 0.02 + sy * 2.5;
    }
    if ((b.tt -= 1) <= 0) retarget(b, w, h, rand);
    b.vx += (b.tx - b.x) * 0.0002;
    b.vy += (b.ty - b.y) * 0.0002;
    // The middle of the sky is where the lines are: ease away from it.
    const dx = b.x - cx;
    const dy = b.y - cy;
    const d = Math.hypot(dx, dy);
    if (calm > 0 && d < calm && d > 0) {
      const push = (1 - d / calm) * 0.05;
      b.vx += (dx / d) * push;
      b.vy += (dy / d) * push;
    }
    const sp = Math.hypot(b.vx, b.vy) || 1;
    const k = sp > 1.3 ? 1.3 / sp : sp < 0.45 ? 0.45 / sp : 1;
    b.vx *= k;
    b.vy *= k;
    b.x += b.vx;
    b.y += b.vy;
    if (b.x < -30) b.x = w + 30;
    else if (b.x > w + 30) b.x = -30;
    if (b.y < -30) b.y = h + 30;
    else if (b.y > h + 30) b.y = -30;
  }
}

/** Run `tick` once per animation frame, at most 60 times a second and never
 * while the tab is hidden. `paused` never asks for a frame. Returns stop(). */
export function skyLoop(
  tick: (now: number) => void,
  opts: {
    paused?: boolean;
    raf?: (cb: (now: number) => void) => number;
    cancel?: (id: number) => void;
    hidden?: () => boolean;
  } = {}
): () => void {
  if (opts.paused) return () => {};
  const raf = opts.raf ?? ((cb) => requestAnimationFrame(cb));
  const cancel = opts.cancel ?? ((id) => cancelAnimationFrame(id));
  const hidden = opts.hidden ?? (() => typeof document !== "undefined" && document.hidden);
  let live = true;
  let last = -Infinity;
  let id = 0;
  const frame = (now: number) => {
    if (!live) return;
    if (!hidden() && now - last >= FRAME_MS - 1) {
      last = now;
      tick(now);
    }
    id = raf(frame);
  };
  id = raf(frame);
  return () => {
    live = false;
    cancel(id);
  };
}

/** Paint the flock: each bird along its heading, mirrored when it flies left
 * (back to the sky), wings beating about the hinge — flock.ts's drawing. A
 * bird whose sprite hasn't decoded yet is a small vector silhouette. */
function paint(g: CanvasRenderingContext2D, birds: readonly SkyBird[], w: number, h: number, zoom: number, frame: number) {
  const light = document.documentElement.classList.contains("light");
  const scale = Math.min(1.3, Math.max(0.7, Math.sqrt(zoom || 1)));
  g.clearRect(0, 0, w, h);
  for (const b of birds) {
    const color = SPECIES[b.species][light ? 1 : 0];
    const img = tinted(color);
    const bw = 15 * b.size * scale;
    const bh = (bw * SPRITE_H) / SPRITE_W;
    const hy = Math.round(SPRITE_H * HINGE);
    const hp = bh * HINGE;
    const f = Math.max(0.2, 0.65 + 0.4 * Math.sin(frame * b.freq + b.phase));
    g.save();
    g.translate(b.x, b.y);
    g.rotate(Math.atan2(b.vy, b.vx));
    if (b.vx < 0) g.scale(1, -1);
    if (img) {
      g.drawImage(img, 0, hy, SPRITE_W, SPRITE_H - hy, -bw / 2, hp - bh / 2, bw, bh - hp);
      g.translate(0, hp - bh / 2);
      g.scale(1, f);
      g.drawImage(img, 0, 0, SPRITE_W, hy, -bw / 2, -hp, bw, hp);
    } else {
      g.fillStyle = color;
      g.beginPath();
      g.ellipse(0, 0, bw * 0.42, bw * 0.13, 0, 0, Math.PI * 2);
      g.fill();
    }
    g.restore();
  }
}

export function SkyFlock({
  width,
  height,
  zoom,
  paused = false,
  density = 1,
}: {
  width: number;
  height: number;
  zoom: number;
  paused?: boolean;
  density?: number;
}): JSX.Element {
  const ref = useRef<HTMLCanvasElement>(null);
  const [reduced, setReduced] = useState(prefersReducedMotion);
  const birds = useRef<SkyBird[]>([]);
  const frame = useRef(0);
  const view = useRef({ width, height, zoom });

  // The preference can change under a mounted sky.
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const on = () => setReduced(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  // Size (DPR-aware), fit the flock to the sky, and paint a frame — so a
  // paused sky, and a zoom while paused, still shows its birds.
  useEffect(() => {
    view.current = { width, height, zoom };
    const c = ref.current;
    const g = c?.getContext("2d");
    if (!c || !g || reduced) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const pw = Math.round(width * dpr);
    const ph = Math.round(height * dpr);
    if (c.width !== pw || c.height !== ph) {
      c.width = pw;
      c.height = ph;
    }
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    birds.current = fitBirds(birds.current, flockSize(width, height, density), width, height);
    paint(g, birds.current, width, height, zoom, frame.current);
  }, [width, height, zoom, density, reduced]);

  useEffect(() => {
    if (reduced) return;
    return skyLoop(
      () => {
        const g = ref.current?.getContext("2d");
        if (!g) return;
        const v = view.current;
        stepBirds(birds.current, v.width, v.height);
        paint(g, birds.current, v.width, v.height, v.zoom, ++frame.current);
      },
      { paused }
    );
  }, [paused, reduced]);

  if (reduced) return <></>;
  return <canvas ref={ref} className="sky-flock" style={{ width, height }} aria-hidden="true" />;
}
