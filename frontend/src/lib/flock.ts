/** The murmuration — the boids flock from the mindflock.ai hero, brought into
 * the app so the birds you see here are literally the ones on the website:
 * same raven sprite, same species palette, same flocking constants.
 *
 * Framework-free on purpose. It owns a <canvas> and a rAF loop and nothing
 * else, so every surface that wants birds (the break screen, the idle overlay)
 * mounts one with a single call and drops it with the returned stop().
 *
 * Three behaviours are worth knowing before you tune it:
 *
 * - The flock makes a ROUND TRIP through the MindFlock mark in the top bar. It
 *   streams out of the logo when it appears and folds back into it when
 *   dismissed. The two halves are not mirror images: the gather is a single
 *   whole-flock move that takes under a second, while the emergence is a slow
 *   stream across twenty, with each bird flying its own short arc and joining
 *   the live flock the moment it lands. See beginHatch for why.
 * - `prefers-reduced-motion` does NOT mean "no birds". It means a flock that
 *   has already settled: 900 steps are run at once and a single static frame
 *   is painted, which is what the hero does too. A break screen with a blank
 *   rectangle where the flock should be reads as broken. There is no entrance
 *   or exit animation in that mode at all.
 * - The sprite is fetched from `/bird.png`, so the first frame or two can land
 *   before it decodes. Those frames paint a plain vector silhouette rather
 *   than nothing, so the flock never flickers in from an empty canvas.
 */

/** Species = the app's bird-named theme presets (theme.css). Each entry is
 * {d: dark-mode plumage (the preset's accent), l: light-mode plumage}.
 *
 * The light column is deliberately much deeper than the hero's — roughly the
 * preset's accent-deep taken down another quarter. The website flies its flock
 * over one flat marketing page; here they cross a wall of syntax-highlit
 * terminal text, and at the hero's weight they simply disappeared into it. Deep
 * enough to read as a silhouette, not so deep that a cardinal and a quetzal
 * become the same black bird. */
const SPECIES: ReadonlyArray<{ d: string; l: string }> = [
  { d: "#7d56f4", l: "#422d8a" } /* violetear   */,
  { d: "#3d8bfd", l: "#144791" } /* bluejay     */,
  { d: "#18b3dd", l: "#095c73" } /* kingfisher  */,
  { d: "#2ec2b3", l: "#09635b" } /* peacock     */,
  { d: "#44b556", l: "#1a5825" } /* quetzal     */,
  { d: "#e8b71e", l: "#675007" } /* goldfinch   */,
  { d: "#a8c332", l: "#48560c" } /* budgie      */,
  { d: "#f07b3c", l: "#8a3d0d" } /* oriole      */,
  { d: "#d444f1", l: "#70148a" } /* hummingbird */,
  { d: "#e5484d", l: "#841419" } /* cardinal    */,
  { d: "#ee5d8f", l: "#8c1e47" } /* flamingo    */,
  { d: "#97a1b5", l: "#3d4554" } /* heron       */,
];

/* The sprite is the raven silhouette with white stripped to alpha, so it can
   be tinted per species. Wings beat about a hinge at the shoulder line. */
const SPRITE_URL = "/bird.png";
const SPRITE_W = 216;
const SPRITE_H = 160;
const HINGE = 0.56;

let sprite: HTMLImageElement | null = null;
const tints = new Map<string, HTMLCanvasElement>();

/** One <img> for the whole app: several flocks can be alive at once (the break
 * screen over the idle overlay, briefly) and they share the decode. */
function birdSprite(): HTMLImageElement | null {
  if (typeof Image === "undefined") return null;
  if (!sprite) {
    sprite = new Image();
    sprite.src = SPRITE_URL;
  }
  return sprite;
}

/** Tint the alpha-only sprite on an offscreen canvas, once per colour. Returns
 * null until the sprite has decoded — the caller falls back to vectors. */
function tinted(color: string): HTMLCanvasElement | null {
  const img = birdSprite();
  if (!img || !img.complete || !img.naturalWidth) return null;
  const cached = tints.get(color);
  if (cached) return cached;
  const c = document.createElement("canvas");
  c.width = SPRITE_W;
  c.height = SPRITE_H;
  const g = c.getContext("2d");
  if (!g) return null;
  g.drawImage(img, 0, 0, SPRITE_W, SPRITE_H);
  g.globalCompositeOperation = "source-in";
  g.fillStyle = color;
  g.fillRect(0, 0, SPRITE_W, SPRITE_H);
  tints.set(color, c);
  return c;
}

interface Boid {
  x: number;
  y: number;
  vx: number;
  vy: number;
  sp: { d: string; l: string };
  size: number;
  phase: number;
  freq: number;
  /** Wander target + how many frames are left before a new one is picked. */
  tx: number;
  ty: number;
  tt: number;
  /** Flight state, for both journeys through the logo. `gx/gy` is this bird's
   * own end of it — where it set off from on the way in, where it is headed on
   * the way out; `curve` bows its path off the straight line; `delay` staggers
   * the gather; `s0`/`f0` are the scale and opacity it started the gather at,
   * so an interrupted flight carries on from what is on screen. */
  gx: number;
  gy: number;
  curve: number;
  delay: number;
  shrink: number;
  fade: number;
  s0: number;
  f0: number;
  /** Emergence: 0 once this bird has landed and joined the flock, otherwise the
   * timestamp at which it leaves the mark. Its own flight is HATCH_FLIGHT_MS
   * long whenever in the window that happens to fall. */
  hatchAt: number;
}

/** How long the flock takes to reach the mark in the top bar. Long enough to
 * read as flight, short enough that it never delays getting back to work. */
export const GATHER_MS = 820;

/** …and how long the flock takes to come back out of it. Twenty seconds is a
 * deliberate, slow reveal rather than an entrance: birds trickle out of the
 * mark the whole time, so the window keeps filling for as long as you are away
 * from it. It is a duration for the WHOLE flock, not for any one bird. */
export const EMERGE_MS = 20_000;

/** How long one bird's own flight out of the mark takes. Launches are spread
 * across `EMERGE_MS - HATCH_FLIGHT_MS`, so the last bird lands exactly on
 * EMERGE_MS. Short next to the window on purpose: a bird that took the full
 * twenty seconds to cross the screen would crawl, and would spend all of it
 * outside the flocking rules. */
const HATCH_FLIGHT_MS = 4000;

export interface FlockHandle {
  stop(): void;
  /** Send every bird streaming to a point — the MindFlock mark in the top bar
   * — shrinking as they arrive, so the flock reads as folding back into the
   * logo. Returns how long that will take in ms, which is 0 when the viewer
   * asked for reduced motion and nothing is going to move. Called during an
   * emergence it takes over from it; called twice it keeps the first flight. */
  gather(x: number, y: number): number;
}

export interface FlockOptions {
  /** Canvas px² per bird — lower is denser. */
  areaPerBird?: number;
  /** Flock-size floor and ceiling, whatever the canvas measures. */
  min?: number;
  max?: number;
  /** Painting opacity (the hero flies at 0.7). */
  alpha?: number;
  /** Hatch the flock out of this point instead of having it simply BE there:
   * the birds leave the mark one at a time across EMERGE_MS, each flying its
   * own short arc and joining the flock the moment it lands. Pass the logo's
   * centre. Ignored under reduced motion, where there is no entrance to
   * animate. */
  emergeFrom?: { x: number; y: number };
}

/** Start a flock on `canvas`. `stop()` is safe to call twice. The canvas is
 * sized from its own CSS box, so give it one. */
export function startFlock(canvas: HTMLCanvasElement, opts: FlockOptions = {}): FlockHandle {
  const ctx = canvas.getContext("2d");
  if (!ctx) return { stop: () => {}, gather: () => 0 };

  const areaPerBird = opts.areaPerBird ?? 14000;
  const minBirds = opts.min ?? 45;
  const maxBirds = opts.max ?? 160;
  // Near-opaque. The hero's 0.7 is a wash over a marketing page; over a grid of
  // terminals a translucent bird reads as a smudge on the text behind it.
  const alpha = opts.alpha ?? 0.95;

  const boids: Boid[] = [];
  let W = 0;
  let H = 0;
  let frame = 0;
  let raf = 0;
  let stopped = false;
  let light = document.documentElement.classList.contains("light");
  /** The flight home. While this exists the flocking rules are off entirely and
   * the whole flock is converging on the mark. */
  let transit: { x: number; y: number; t0: number } | null = null;
  /** The emergence: the mark birds are streaming out of, and when the last one
   * lands. Unlike the gather this runs ALONGSIDE the flocking rules — see
   * beginHatch. */
  let hatchFrom: { x: number; y: number } | null = null;
  let hatchEnd = 0;

  const reduced =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* Steer each bird toward a random point on the canvas so the flock crosses
     the whole screen instead of settling into one corner. */
  function pickTarget(b: Boid) {
    b.tx = Math.random() * W;
    b.ty = Math.random() * H;
    b.tt = 200 + Math.random() * 400;
  }

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (!w || !h) return;
    W = w;
    H = h;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
    // Mid-flight — on the way into the mark, or still streaming out of it —
    // every bird is interpolating between a recorded position and the mark, so
    // re-seeding the flock now would teleport half of it. The canvas still gets
    // its new size, and hatchStep calls back here when the stream ends.
    if (transit || hatchFrom) return;
    const n = Math.min(maxBirds, Math.max(minBirds, Math.round((W * H) / areaPerBird)));
    for (const b of boids) pickTarget(b);
    while (boids.length < n) {
      const b: Boid = {
        x: Math.random() * W,
        y: Math.random() * H,
        vx: (Math.random() - 0.5) * 2,
        vy: (Math.random() - 0.5) * 2,
        sp: SPECIES[Math.floor(Math.random() * SPECIES.length)],
        size: 1.35 + Math.random() * 0.95,
        phase: Math.random() * Math.PI * 2,
        // Slow flapping, to match the slow flight speed below.
        freq: 0.07 + Math.random() * 0.05,
        tx: 0,
        ty: 0,
        tt: 0,
        gx: 0,
        gy: 0,
        curve: (Math.random() - 0.5) * 2,
        delay: 0,
        shrink: 1,
        fade: 1,
        s0: 1,
        f0: 1,
        hatchAt: 0,
      };
      pickTarget(b);
      boids.push(b);
    }
    boids.length = n;
    if (reduced) {
      // No rAF loop runs in this mode, so step() — the only code that folds a
      // bird back inside the canvas — will never run again on its own. Without
      // this, shrinking the window strands every bird the new box left outside
      // it, permanently, and birds appended on a grow paint as a raw scatter
      // beside the settled ones. A short settle costs a couple of ms.
      for (const b of boids) {
        b.x = ((b.x % W) + W) % W;
        b.y = ((b.y % H) + H) % H;
      }
      for (let k = 0; k < 60; k++) step();
      draw();
    }
  }

  function step() {
    // Small interaction radius, so they flock together occasionally rather
    // than constantly — a tight murmuration over a terminal is a distraction.
    const R = 35;
    const R2 = R * R;
    const SEP = 15 * 15;
    for (let i = 0; i < boids.length; i++) {
      const b = boids[i];
      // Still in the stream out of the mark — hatchStep owns it, and it is not
      // a neighbour to anyone yet either (see the inner loop).
      if (b.hatchAt) continue;
      let cx = 0;
      let cy = 0;
      let ax = 0;
      let ay = 0;
      let sx = 0;
      let sy = 0;
      let n = 0;
      for (let j = 0; j < boids.length; j++) {
        if (i === j) continue;
        const o = boids[j];
        if (o.hatchAt) continue;
        const dx = o.x - b.x;
        const dy = o.y - b.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < R2) {
          cx += o.x;
          cy += o.y;
          ax += o.vx;
          ay += o.vy;
          n++;
          if (d2 < SEP && d2 > 0) {
            sx -= dx / d2;
            sy -= dy / d2;
          }
        }
      }
      if (n) {
        // Light cohesion/alignment: they wander independently and flock casually.
        b.vx += (cx / n - b.x) * 0.0012 + (ax / n - b.vx) * 0.035 + sx * 3.2;
        b.vy += (cy / n - b.y) * 0.0012 + (ay / n - b.vy) * 0.035 + sy * 3.2;
      }

      // Aim at the wander target the short way round the torus, so a bird near
      // an edge crosses it instead of turning back across the whole canvas.
      let tdx = b.tx - b.x;
      if (tdx > W / 2) tdx -= W;
      else if (tdx < -W / 2) tdx += W;
      let tdy = b.ty - b.y;
      if (tdy > H / 2) tdy -= H;
      else if (tdy < -H / 2) tdy += H;
      if ((b.tt -= 1) <= 0 || tdx * tdx + tdy * tdy < 625) pickTarget(b);
      b.vx += tdx * 0.0004;
      b.vy += tdy * 0.0004;

      const sp = Math.sqrt(b.vx * b.vx + b.vy * b.vy) || 1;
      const max = 1.1;
      const min = 0.4;
      if (sp > max) {
        b.vx = (b.vx / sp) * max;
        b.vy = (b.vy / sp) * max;
      }
      if (sp < min) {
        b.vx = (b.vx / sp) * min;
        b.vy = (b.vy / sp) * min;
      }
      b.x += b.vx;
      b.y += b.vy;
      if (b.x < -20) b.x = W + 20;
      if (b.x > W + 20) b.x = -20;
      if (b.y < -20) b.y = H + 20;
      if (b.y > H + 20) b.y = -20;
    }
  }

  /** 0→1 with the ends eased, so the flock sets off and lands smoothly rather
   * than snapping into motion and stopping dead. */
  function smoothstep(t: number): number {
    const c = Math.min(1, Math.max(0, t));
    return c * c * (3 - 2 * c);
  }

  /** Arm the flight home: record where each bird set off from, and the scale
   * and opacity it started at, so an interrupted flight carries on from
   * whatever is on screen rather than snapping. Whole-flock and synchronised —
   * everything converges on the mark inside a second. */
  function beginGather(x: number, y: number) {
    hatchFrom = null;
    for (const b of boids) {
      b.hatchAt = 0;
      b.gx = b.x;
      b.gy = b.y;
      b.delay = Math.random() * 0.25;
      b.s0 = b.shrink;
      b.f0 = b.fade;
    }
    transit = { x, y, t0: performance.now() };
  }

  /** One frame of the flight home. Returns false once every bird has arrived. */
  function gatherStep(now: number): boolean {
    const h = transit!;
    const raw = Math.min(1, (now - h.t0) / GATHER_MS);
    for (const b of boids) {
      // Per-bird stagger, so the flock folds into the mark as a stream rather
      // than as one solid block.
      const t = smoothstep((raw - b.delay) / (1 - b.delay));
      const dx = h.x - b.gx;
      const dy = h.y - b.gy;
      const len = Math.hypot(dx, dy) || 1;
      // A shallow bow across the straight line, widest at the half-way point,
      // so a hundred birds on the same errand don't fly it as parallel rulers.
      const arc = Math.sin(t * Math.PI) * b.curve * Math.min(120, len * 0.3);
      const px = b.x;
      const py = b.y;
      b.x = b.gx + dx * t + (-dy / len) * arc;
      b.y = b.gy + dy * t + (dx / len) * arc;
      // Heading follows the path actually flown. Near-zero deltas (the first
      // frame, and the last) would spin the sprite, so keep the old heading.
      const mx = b.x - px;
      const my = b.y - py;
      if (mx * mx + my * my > 1e-4) {
        b.vx = mx;
        b.vy = my;
      }
      b.shrink = b.s0 * (1 - t * 0.82);
      // Hold opacity until they are nearly there, then let the last of them
      // dissolve into the mark instead of blinking out on top of it.
      b.fade = b.f0 * (1 - smoothstep((t - 0.72) / 0.28));
    }
    return raw < 1;
  }

  /** Arm the emergence: park the whole flock on the mark, invisible, and hand
   * each bird a launch time. Deliberately NOT the gather run backwards — over
   * twenty seconds a single whole-flock interpolation is twenty seconds with
   * the flocking rules switched off, which reads as dead. Instead the birds
   * leave one at a time across the window, each flying its own short arc, and
   * every one that lands rejoins the live flock immediately. A few seconds in
   * you have a real murmuration by the edges AND a stream still pouring out of
   * the logo. */
  function beginHatch(x: number, y: number) {
    transit = null;
    const t0 = performance.now();
    const launchWindow = Math.max(0, EMERGE_MS - HATCH_FLIGHT_MS);
    for (let i = 0; i < boids.length; i++) {
      const b = boids[i];
      // Where resize() already put it — the emergence just delivers it there.
      b.gx = b.x;
      b.gy = b.y;
      b.x = x;
      b.y = y;
      b.shrink = 0.16;
      b.fade = 0;
      // Even order (the array is spatially random) with a bird's worth of
      // jitter, so it reads as a stream and not as a metronome.
      b.hatchAt = t0 + (launchWindow * (i + Math.random())) / Math.max(1, boids.length);
    }
    hatchFrom = { x, y };
    hatchEnd = t0 + launchWindow + HATCH_FLIGHT_MS;
  }

  /** Hand a bird that has arrived back to the flocking rules. smoothstep lands
   * with almost no velocity, and step()'s min-speed clamp cannot rescue a bird
   * whose vector is exactly zero — so give it a real cruising speed along the
   * heading it came in on. */
  function land(b: Boid) {
    b.x = b.gx;
    b.y = b.gy;
    b.shrink = 1;
    b.fade = 1;
    const sp = Math.hypot(b.vx, b.vy);
    const a = sp > 1e-3 ? Math.atan2(b.vy, b.vx) : Math.random() * Math.PI * 2;
    b.vx = Math.cos(a) * 0.8;
    b.vy = Math.sin(a) * 0.8;
    b.hatchAt = 0;
    pickTarget(b);
  }

  /** One frame of the emergence. Only touches birds that have not landed yet;
   * step() runs alongside it for the ones that have. */
  function hatchStep(now: number) {
    const h = hatchFrom!;
    for (const b of boids) {
      if (!b.hatchAt) continue;
      const raw = (now - b.hatchAt) / HATCH_FLIGHT_MS;
      if (raw <= 0) {
        // Waiting its turn on the mark, invisible.
        b.x = h.x;
        b.y = h.y;
        b.fade = 0;
        continue;
      }
      if (raw >= 1) {
        land(b);
        continue;
      }
      const t = smoothstep(raw);
      const dx = b.gx - h.x;
      const dy = b.gy - h.y;
      const len = Math.hypot(dx, dy) || 1;
      const arc = Math.sin(t * Math.PI) * b.curve * Math.min(120, len * 0.3);
      const px = b.x;
      const py = b.y;
      b.x = h.x + dx * t + (-dy / len) * arc;
      b.y = h.y + dy * t + (dx / len) * arc;
      const mx = b.x - px;
      const my = b.y - py;
      if (mx * mx + my * my > 1e-4) {
        b.vx = mx;
        b.vy = my;
      }
      // Grow out of the mark, and fade in over the first breath so nobody pops
      // into existence on top of the logo.
      b.shrink = 0.16 + 0.84 * t;
      b.fade = smoothstep(raw / 0.18);
    }
    if (now < hatchEnd) return;
    // Window over: nothing may be left mid-flight, and any resize deferred
    // while the stream was running gets settled now.
    for (const b of boids) if (b.hatchAt) land(b);
    hatchFrom = null;
    resize();
  }

  /** Sprite-less silhouette for the frames before /bird.png decodes: a body
   * along the heading with one wing beating above it. */
  function vectorBird(g: CanvasRenderingContext2D, w: number, flap: number) {
    g.beginPath();
    g.ellipse(0, 0, w * 0.42, w * 0.13, 0, 0, Math.PI * 2);
    g.fill();
    g.beginPath();
    g.moveTo(-w * 0.12, 0);
    g.lineTo(w * 0.1, -w * 0.5 * flap);
    g.lineTo(w * 0.28, 0);
    g.closePath();
    g.fill();
  }

  function draw() {
    const g = ctx!;
    frame++;
    g.clearRect(0, 0, W, H);
    for (const b of boids) {
      // Birds on an errand — out of the mark or back into it — beat their wings
      // harder than birds drifting.
      const flapRate = transit || b.hatchAt ? 2.4 : 1;
      const a = Math.atan2(b.vy, b.vx);
      const color = light ? b.sp.l : b.sp.d;
      const img = tinted(color);
      const bw = 15 * b.size * b.shrink;
      const bh = (bw * SPRITE_H) / SPRITE_W;
      const hy = Math.round(SPRITE_H * HINGE);
      const hp = bh * HINGE;
      const f = 0.65 + 0.4 * Math.sin(frame * b.freq * flapRate + b.phase);
      // Per-bird rather than per-frame: the stagger means they fade at
      // different moments.
      g.globalAlpha = alpha * b.fade;
      g.save();
      g.translate(b.x, b.y);
      g.rotate(a);
      /* birds heading left would render belly-up — mirror them in their own
         frame so the back always faces the sky */
      if (b.vx < 0) g.scale(1, -1);
      if (img) {
        /* body + tail below the hinge, steady */
        g.drawImage(img, 0, hy, SPRITE_W, SPRITE_H - hy, -bw / 2, hp - bh / 2, bw, bh - hp);
        /* wings above the hinge, beating about it */
        g.translate(0, hp - bh / 2);
        g.scale(1, Math.max(0.2, f));
        g.drawImage(img, 0, 0, SPRITE_W, hy, -bw / 2, -hp, bw, hp);
      } else {
        g.fillStyle = color;
        vectorBird(g, bw, Math.max(0.2, f));
      }
      g.restore();
    }
    g.globalAlpha = 1;
  }

  function loop() {
    if (stopped) return;
    // A hidden tab already throttles rAF to a crawl; skipping the work keeps
    // the flock from lurching forward when the tab comes back.
    if (!document.hidden) {
      // performance.now(), not the rAF timestamp it is handed: the flight
      // times are read from the same clock, and the two only share an epoch by
      // specification. One source is one fewer thing that can drift.
      const now = performance.now();
      if (transit) {
        gatherStep(now);
      } else {
        // Not either/or: birds still streaming out of the mark are steered by
        // hatchStep, and step() flocks everyone who has already landed.
        if (hatchFrom) hatchStep(now);
        step();
      }
      draw();
    }
    raf = requestAnimationFrame(loop);
  }

  // Theme toggles flip a `light` class on <html> (see TopBar) — repaint in the
  // other plumage rather than waiting for a remount.
  const themeWatch = new MutationObserver(() => {
    const next = document.documentElement.classList.contains("light");
    if (next === light) return;
    light = next;
    if (reduced) draw();
  });
  themeWatch.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });

  const ro =
    typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => resize()) : null;
  if (ro) ro.observe(canvas);
  window.addEventListener("resize", resize);

  // Under reduced motion the ONLY frames painted are the ones below plus the
  // ones resize/theme ask for — all of which can happen before /bird.png has
  // decoded, leaving the whole flock on the crude vector fallback for its
  // entire life. Repaint once the sprite lands. The animated path needs no
  // such thing: its next frame is 16ms away.
  const spriteImg = birdSprite();
  const onSpriteReady = () => {
    if (!stopped && reduced) draw();
  };
  const spritePending = !!spriteImg && !(spriteImg.complete && spriteImg.naturalWidth);
  if (spritePending) spriteImg!.addEventListener("load", onSpriteReady);

  resize();
  if (reduced) {
    // Fast-forward to a settled flock, then hold it there.
    for (let k = 0; k < 900; k++) step();
    draw();
  } else {
    // Hatch them out of the mark rather than having them simply BE everywhere.
    // resize() has already put every bird where it belongs, so the emergence
    // just plays that placement backwards: collapse onto the point, then stream
    // out to it. Guarded on a populated flock — a canvas with no size yet has
    // none, and arming a transit would lock resize() out of ever giving it any.
    if (opts.emergeFrom && boids.length) {
      beginHatch(opts.emergeFrom.x, opts.emergeFrom.y);
    }
    raf = requestAnimationFrame(loop);
  }

  return {
    stop() {
      if (stopped) return;
      stopped = true;
      cancelAnimationFrame(raf);
      themeWatch.disconnect();
      if (ro) ro.disconnect();
      window.removeEventListener("resize", resize);
      if (spritePending) spriteImg!.removeEventListener("load", onSpriteReady);
    },
    gather(x: number, y: number): number {
      // Reduced motion means the flock never moved in the first place; flying
      // it across the screen on the way out would be the one animation the
      // preference exists to prevent.
      if (reduced || stopped) return 0;
      // Already on its way home (a second click, or Escape after a click) —
      // report what is left of that flight rather than restarting it. An
      // emergence, though, gets cut short: whatever is on screen right now is
      // where they set off from.
      if (transit) return Math.max(0, GATHER_MS - (performance.now() - transit.t0));
      beginGather(x, y);
      return GATHER_MS;
    },
  };
}
