/** The tree's controller: camera, input, selection, paint tools and the frame
 * loop over one canvas (the imperative half of the prototype's ui.js). The
 * React component (CodeTree.tsx) renders the HUD from `snapshot()` and calls
 * back in; zones themselves are never decided here — painting calls
 * `cb.paint`, which goes to the server, and the next poll draws the result.
 *
 * Cost discipline: frames are drawn only while the tab is active, the document
 * visible and something changed (camera, data, hover) — plus a ~12 fps bob for
 * perched birds unless motion is reduced. */

import { blastOf } from "../../../lib/codetree/blast";
import { birdWorldAt, buildMini, drawMini, invalidateStatic, pick, prepGeometry, render, hitNode } from "../../../lib/codetree/draw";
import { S0, isUnder, type Leaf, type Model, type TFile, type TNode } from "../../../lib/codetree/model";
import { readPalette, type Palette } from "../../../lib/codetree/palette";
import type { AgentState, Badge, Cam, Hit, Tool, TreeView, TZone } from "../../../lib/codetree/types";

export type Info =
  | { kind: "file"; f: TFile }
  | { kind: "node"; n: TNode }
  | { kind: "blast"; bd: Badge }
  | null;

export interface Tip {
  x: number;
  y: number;
  hit: Hit;
}

export interface CtlCallbacks {
  /** paint a rule on a folder / file (the tool that was armed) */
  paint: (tool: "keep" | "only", target: { node?: TNode | null; file?: TFile | null }) => void;
  /** a rule tag on the canvas was clicked (remove it) */
  removeZone: (z: TZone) => void;
  note: (msg: string) => void;
  /** HUD state changed (selection, tool, history, follow, tip) */
  changed: () => void;
}

/** Where the HUD covers the canvas (canvas px): kept out of fits. */
export interface Insets {
  top: number;
  bot: number;
  left: number;
  right: number;
}

interface Fly {
  a: Cam;
  b: Cam;
  t0: number;
  dur: number;
}

/** Following a bird: at least this zoom, where its folder's name and the leaf
 * names around it show (leaf labels need S0 * z >= 20). */
export const FOLLOW_Z = 2.6;

/** Per-title view memory (camera, folds, history) — survives remounts. */
const MEMORY = new Map<string, { cam: Cam; hist: Cam[]; folded: string[]; model: Model }>();

export class TreeCtl {
  cv: HTMLCanvasElement;
  mini: HTMLCanvasElement | null = null;
  v: TreeView | null = null;
  M: Model | null = null;
  camT: Cam = { x: 0, y: 0, z: 1 };
  fly: Fly | null = null;
  hist: Cam[] = [];
  info: Info = null;
  tip: Tip | null = null;
  insets: Insets = { top: 8, bot: 8, left: 8, right: 8 };
  /** insets while a card is open (the card sits over the right) */
  cardInset = 0;
  /** zoomed in past the whole-tree view: only then is the minimap worth its corner */
  zoomedIn = false;
  active = false;
  dirty = true;
  private raf = 0;
  private lastNow = 0;
  private lastBob = 0;
  private needPick = false;
  private mouse: [number, number] | null = null;
  private clickAt: [number, number] | null = null;
  private drag: { x: number; y: number; cx: number; cy: number; moved: boolean; id: number } | null = null;
  private lastTap = 0;
  private lastWheel = 0;
  private P: Palette | null = null;
  private ro: ResizeObserver | null = null;
  private mo: MutationObserver | null = null;
  private off: Array<() => void> = [];
  private agents: AgentState[] = [];
  private zones: TZone[] = [];
  reducedMotion = false;
  /** the camera before the first follow (Esc flies back to it) */
  private followFrom: Cam | null = null;

  constructor(
    cv: HTMLCanvasElement,
    private host: HTMLElement,
    private title: string,
    private cb: CtlCallbacks
  ) {
    this.cv = cv;
    const on = <K extends keyof HTMLElementEventMap>(el: HTMLElement, ev: K, fn: (e: HTMLElementEventMap[K]) => void, opt?: AddEventListenerOptions) => {
      el.addEventListener(ev, fn as EventListener, opt);
      this.off.push(() => el.removeEventListener(ev, fn as EventListener, opt));
    };
    on(cv, "pointerdown", (e) => this.onDown(e));
    on(cv, "pointermove", (e) => this.onMove(e));
    on(cv, "pointerup", (e) => this.onUp(e));
    on(cv, "pointercancel", () => {
      this.drag = null;
      cv.classList.remove("dragging");
    });
    on(cv, "pointerleave", () => {
      this.mouse = null;
      if (this.v) {
        this.v.hover = null;
        this.v.hoverBlast = null;
      }
      this.setTip(null);
      this.dirty = true;
    });
    on(cv, "dblclick", (e) => this.onDbl(e));
    on(cv, "wheel", (e) => this.onWheel(e), { passive: false });
    this.ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => this.resize()) : null;
    this.ro?.observe(host);
    // theme / accent / surface flips repaint in the new palette (like lib/flock.ts)
    this.mo = new MutationObserver(() => this.retheme());
    this.mo.observe(document.documentElement, { attributes: true, attributeFilter: ["class", "data-accent", "data-surface", "style"] });
    const vis = () => {
      if (!document.hidden) this.kick();
    };
    document.addEventListener("visibilitychange", vis);
    this.off.push(() => document.removeEventListener("visibilitychange", vis));
  }

  destroy() {
    this.active = false;
    cancelAnimationFrame(this.raf);
    this.ro?.disconnect();
    this.mo?.disconnect();
    for (const f of this.off) f();
    this.remember();
  }

  private remember() {
    if (!this.M || !this.v) return;
    MEMORY.set(this.title, { cam: { ...this.camT }, hist: this.hist.slice(), folded: [...this.v.folded].map((n) => n.path), model: this.M });
    while (MEMORY.size > 16) MEMORY.delete(MEMORY.keys().next().value!);
  }

  // --- data ----------------------------------------------------------------

  setModel(M: Model) {
    if (this.M === M) return;
    const first = !this.M;
    const prevCam = this.v ? { ...this.camT } : null;
    if (!M.geom) {
      prepGeometry(M);
      M.geom = true;
    }
    this.M = M;
    const g = this.cv.getContext("2d")!;
    this.P = this.P || readPalette(this.host);
    const mem = MEMORY.get(this.title);
    const folded = new Set<TNode>();
    for (const p of (this.v ? [...this.v.folded].map((n) => n.path) : mem?.folded) || []) {
      const n = M.nodeOf.get(p);
      if (n) folded.add(n);
    }
    this.v = {
      g, M, P: this.P, agents: this.agents, W: this.v?.W || 1, H: this.v?.H || 1, dpr: this.v?.dpr || 1, cam: { x: 0, y: 0, z: 1 }, zones: this.zones,
      folded, tool: this.v?.tool || "explore", t: performance.now() / 1000, playing: !this.reducedMotion, reducedMotion: this.reducedMotion,
      hover: null, sel: null, infoNode: null, infoFile: null, pinBlast: null, hoverBlast: null, pulse: null, follow: this.v?.follow ?? null,
      markLeaf: null, blastCache: new Map(), hudRects: this.v?.hudRects || [], free: this.v?.free || { x0: 0, y0: 0, x1: 1, y1: 1 }, fitZv: 0,
      riskAll: this.v?.riskAll || false, riskFile: null,
    };
    this.resize();
    // keep the view: the same camera on a re-laid tree (a warm start moved
    // little); a remount restores its camera; the first look fits the tree
    if (prevCam) this.setCam(prevCam);
    else if (mem && mem.model === M) {
      this.setCam(mem.cam);
      this.hist = mem.hist;
    } else if (mem) this.setCam(mem.cam);
    else this.fitAll(true);
    if (this.info) {
      // re-point an open card at the new model's objects
      const i = this.info;
      if (i.kind === "file") {
        const f = M.byPath.get(i.f.path);
        this.info = f ? { kind: "file", f } : null;
      } else if (i.kind === "node") {
        const n = i.n.kind === "pile" ? M.piles.find((p) => p.path === i.n.path) : (i.n.kind === "root" ? M.rootNodeOf : M.nodeOf).get(i.n.path);
        this.info = n ? { kind: "node", n } : null;
      } else this.info = null;
      this.syncSel();
    }
    if (first) this.kick();
    this.dirty = true;
    this.cb.changed();
  }

  setLive(agents: AgentState[], zones: TZone[]) {
    this.agents = agents;
    this.zones = zones;
    if (this.v) {
      this.v.agents = agents;
      this.v.zones = zones;
      // a followed bird that left the tree (a finished helper dropped off): stop quietly
      if (this.v.follow != null && !agents.some((A) => A.ag.key === this.v!.follow)) this.stopFollow();
    }
    this.dirty = true;
    this.kick();
  }

  setActive(a: boolean) {
    this.active = a;
    if (a) {
      this.retheme();
      this.kick();
    } else this.remember();
  }

  setReducedMotion(r: boolean) {
    this.reducedMotion = r;
    if (this.v) {
      this.v.reducedMotion = r;
      this.v.playing = !r;
    }
    this.dirty = true;
  }

  retheme() {
    const P = readPalette(this.host);
    if (this.P && P.key === this.P.key) return;
    this.P = P;
    if (this.v) {
      this.v.P = P;
      invalidateStatic(this.v);
      if (this.mini) buildMini(this.v, this.mini);
    }
    this.dirty = true;
    this.kick();
  }

  setHud(rects: Array<[number, number, number, number]>, insets: Insets) {
    this.insets = insets;
    if (!this.v) return;
    this.v.hudRects = rects;
    this.v.free = { x0: insets.left, y0: insets.top, x1: this.v.W - insets.right, y1: this.v.H - insets.bot };
    this.v.fitZv = this.fitZ();
    this.dirty = true;
    this.kick();
  }

  setMini(c: HTMLCanvasElement | null) {
    this.mini = c;
    if (c && this.v) buildMini(this.v, c);
    this.dirty = true;
  }

  resize() {
    const v = this.v;
    const r = this.host.getBoundingClientRect();
    const W = Math.max(1, Math.round(r.width)),
      H = Math.max(1, Math.round(r.height));
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    if (this.cv.width !== Math.round(W * dpr) || this.cv.height !== Math.round(H * dpr)) {
      this.cv.width = Math.round(W * dpr);
      this.cv.height = Math.round(H * dpr);
    }
    if (v) {
      const was = v.W;
      v.W = W;
      v.H = H;
      v.dpr = dpr;
      v.free = { x0: this.insets.left, y0: this.insets.top, x1: W - this.insets.right, y1: H - this.insets.bot };
      v.fitZv = this.fitZ();
      if (was <= 1 && this.M) this.fitAll(true);
      if (this.mini) buildMini(v, this.mini);
    }
    this.dirty = true;
    this.kick();
  }

  // --- camera (ported from ui.js) --------------------------------------------

  screenToWorld(sx: number, sy: number, cam?: Cam): [number, number] {
    const v = this.v!;
    const c = cam || v.cam;
    return [(sx - v.W / 2) / c.z + c.x, (sy - v.H / 2) / c.z + c.y];
  }
  private setCam(c: Cam) {
    if (!this.v) return;
    this.v.cam = { ...c };
    this.camT = { ...c };
    this.fly = null;
  }
  fitBox(x0: number, y0: number, x1: number, y1: number, pad = 0.86, ignoreCard = false): Cam {
    const v = this.v!;
    const I = { ...this.insets };
    if (!ignoreCard) I.right += this.cardInset;
    const w = Math.max(40, v.W - I.left - I.right),
      h = Math.max(40, v.H - I.top - I.bot);
    const z = Math.min((w * pad) / Math.max(1, x1 - x0), (h * pad) / Math.max(1, y1 - y0));
    return { x: (x0 + x1) / 2 - (I.left - I.right) / 2 / z, y: (y0 + y1) / 2 - (I.top - I.bot) / 2 / z, z };
  }
  /** Whole tree always frames the same way, card or no card. */
  fitZ(): number {
    const B = this.M!.bounds;
    return this.fitBox(B.x0, B.y0, B.x1, B.y1, 0.94, true).z;
  }
  fitAll(instant = false) {
    if (!this.M || !this.v) return;
    const B = this.M.bounds;
    const c = this.fitBox(B.x0, B.y0, B.x1, B.y1, 0.94, true);
    this.v.fitZv = c.z;
    if (instant) this.setCam(c);
    else this.flyTo(c);
  }
  wholeTree() {
    this.pushHist();
    this.stopFollow(true);
    this.closeInfo();
    this.fitAll(false);
  }
  pushHist() {
    if (!this.v) return;
    const c = this.fly ? this.v.cam : this.camT,
      L = this.hist[this.hist.length - 1];
    if (L && Math.abs(Math.log(L.z / c.z)) < 0.02 && Math.hypot(L.x - c.x, L.y - c.y) * c.z < 4) return;
    this.hist.push({ ...c });
    if (this.hist.length > 30) this.hist.shift();
    this.cb.changed();
  }
  goBack() {
    const c = this.hist.pop();
    this.cb.changed();
    if (!c) return;
    this.stopFollow(true);
    this.flyTo(c, 600);
  }
  flyTo(c: Cam, dur = 850) {
    if (!this.v) return;
    if (this.reducedMotion) {
      this.setCam(c);
      this.dirty = true;
      this.kick();
      return;
    }
    this.fly = { a: { ...this.v.cam }, b: c, t0: performance.now(), dur };
    this.camT = { ...c };
    this.dirty = true;
    this.kick();
  }
  navTo(c: Cam, dur?: number) {
    this.pushHist();
    this.stopFollow(true);
    this.flyTo(c, dur);
  }
  nodeInView(n: TNode): boolean {
    const v = this.v;
    if (!v || !n.cnt) return false;
    const f = v.free;
    const toS = (x: number, y: number) => [(x - v.cam.x) * v.cam.z + v.W / 2, (y - v.cam.y) * v.cam.z + v.H / 2];
    const [x0, y0] = toS(n.bx0, n.by0),
      [x1, y1] = toS(n.bx1, n.by1);
    return x0 > f.x0 && x1 < f.x1 && y0 > f.y0 && y1 < f.y1;
  }
  flyToNode(n: TNode, pad?: number) {
    const v = this.v,
      M = this.M;
    if (!v || !M) return;
    if (n.kind === "pile") {
      const c = this.fitBox(n.cx - (n.pw || 30), M.groundY - (n.h || 10) * 3, n.cx + (n.pw || 30), M.groundY + (n.h || 10), 0.7);
      this.navTo(c);
      v.pulse = { x: n.cx, y: M.groundY - (n.h || 10) * 0.5, r: (n.pw || 30) * 0.6, t0: performance.now() };
      return;
    }
    if (n.depth === 0) {
      this.pushHist();
      this.stopFollow(true);
      this.fitAll(false);
      return;
    }
    const c = this.fitBox(n.bx0 - S0 * 2, n.by0 - S0 * 2, n.bx1 + S0 * 2, n.by1 + S0 * 2, pad || 0.7);
    c.z = Math.min(c.z, 4.2);
    this.navTo(c);
    v.pulse = { x: n.cx, y: n.cy, r: n.rad, t0: performance.now() + 500 };
  }
  flyToLeaf(l: Leaf, z?: number) {
    const v = this.v;
    if (!v) return;
    const zz = z || Math.max(v.cam.z, 2.6);
    const I = { ...this.insets, right: this.insets.right + this.cardInset };
    this.navTo({ x: l.x - (I.left - I.right) / 2 / zz, y: l.y - (I.top - I.bot) / 2 / zz, z: zz });
    v.pulse = { x: l.x, y: l.y, r: l.len, t0: performance.now() + 600 };
  }
  private clampZ(z: number) {
    return Math.max(this.fitZ() * 0.85, Math.min(7, z));
  }
  zoomAt(sx: number, sy: number, factor: number) {
    const v = this.v;
    if (!v) return;
    const base = this.fly ? v.cam : this.camT;
    this.fly = null;
    this.stopFollow(true); // a zoom is the user taking the camera back
    const [wx, wy] = this.screenToWorld(sx, sy, base);
    const z = this.clampZ(base.z * factor);
    this.camT = { x: wx - (sx - v.W / 2) / z, y: wy - (sy - v.H / 2) / z, z };
    if (this.reducedMotion) v.cam = { ...this.camT };
    this.dirty = true;
    this.needPick = true;
    this.kick();
  }
  panBy(dx: number, dy: number) {
    const v = this.v;
    if (!v) return;
    const base = this.fly ? v.cam : this.camT;
    this.fly = null;
    this.camT = { x: base.x + dx / base.z, y: base.y + dy / base.z, z: base.z };
    if (this.reducedMotion) v.cam = { ...this.camT };
    this.stopFollow(true);
    this.dirty = true;
    this.needPick = true;
    this.kick();
  }
  private stepCamera(now: number, dt: number) {
    const v = this.v!;
    const fp = v.follow != null ? this.birdAt(v.follow) : null;
    if (this.fly && fp) {
      // gliding to a followed bird that is itself flying: aim at where it is now
      const b = this.followCam(fp, this.fly.b.z);
      this.fly.b.x = b.x;
      this.fly.b.y = b.y;
    }
    if (this.fly) {
      const k = Math.min(1, (now - this.fly.t0) / this.fly.dur),
        e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
      const a = this.fly.a,
        b = this.fly.b;
      const lz = Math.log(a.z) + (Math.log(b.z) - Math.log(a.z)) * e;
      // dip out a little on long hops so the target stays in view (van Wijk-lite)
      const d = (Math.hypot(b.x - a.x, b.y - a.y) * Math.min(a.z, b.z)) / Math.max(v.W, 1);
      const dip = Math.min(0.9, d * 0.5) * Math.sin(Math.PI * e);
      v.cam = { x: a.x + (b.x - a.x) * e, y: a.y + (b.y - a.y) * e, z: Math.exp(lz - dip) };
      if (k >= 1) {
        this.fly = null;
        v.cam = { ...b };
        this.camT = { ...b };
      }
      this.dirty = true;
      this.needPick = true;
      return;
    }
    if (fp) {
      const b = this.followCam(fp, this.camT.z);
      this.camT.x = b.x;
      this.camT.y = b.y;
      // reduced motion: the camera keeps the bird centred without easing after it
      if (this.reducedMotion) v.cam = { ...this.camT };
    }
    const c = v.cam,
      T = this.camT,
      k = 1 - Math.exp(-dt * 0.016);
    const dz = Math.log(T.z) - Math.log(c.z);
    if (Math.abs(dz) > 1e-4 || Math.abs(T.x - c.x) * c.z > 0.05 || Math.abs(T.y - c.y) * c.z > 0.05) {
      c.z = Math.exp(Math.log(c.z) + dz * k);
      c.x += (T.x - c.x) * k;
      c.y += (T.y - c.y) * k;
      this.dirty = true;
      this.needPick = true;
    } else if (c.x !== T.x || c.y !== T.y || c.z !== T.z) {
      c.x = T.x;
      c.y = T.y;
      c.z = T.z;
      this.dirty = true;
      this.needPick = true;
    }
  }

  // --- input ---------------------------------------------------------------

  private local(e: { clientX: number; clientY: number }): [number, number] {
    const r = this.cv.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  }
  private onDown(e: PointerEvent) {
    if (!this.v) return;
    try {
      this.cv.setPointerCapture(e.pointerId);
    } catch {
      /* fine */
    }
    const [x, y] = this.local(e);
    this.drag = { x, y, cx: this.v.cam.x, cy: this.v.cam.y, moved: false, id: e.pointerId };
  }
  private onMove(e: PointerEvent) {
    const v = this.v;
    if (!v) return;
    const [x, y] = this.local(e);
    const d = this.drag;
    if (d && d.id === e.pointerId) {
      const dx = x - d.x,
        dy = y - d.y;
      if (!d.moved && Math.hypot(dx, dy) > 4) {
        this.pushHist();
        d.moved = true;
        this.cv.classList.add("dragging");
        this.fly = null;
        this.stopFollow(true);
        this.setTip(null);
      }
      if (d.moved) {
        v.cam.x = d.cx - dx / v.cam.z;
        v.cam.y = d.cy - dy / v.cam.z;
        this.camT = { ...v.cam };
        this.dirty = true;
        this.kick();
      }
      return;
    }
    // a tooltip follows pointer MOTION: after a click nothing pops up until the hand moves on
    if (this.clickAt && Math.hypot(x - this.clickAt[0], y - this.clickAt[1]) < 6) return;
    this.clickAt = null;
    this.mouse = [x, y];
    this.needPick = true;
    this.dirty = true;
    this.kick();
  }
  private onUp(e: PointerEvent) {
    const d = this.drag;
    if (!d) return;
    this.drag = null;
    this.cv.classList.remove("dragging");
    const [x, y] = this.local(e);
    if (d.moved) {
      this.mouse = [x, y];
      this.needPick = true;
      return;
    }
    const now = performance.now();
    if (e.pointerType !== "mouse" && now - this.lastTap < 300) {
      this.zoomAt(x, y, 2.2);
      this.lastTap = 0;
      return;
    }
    this.lastTap = now;
    this.onClick(x, y, e.shiftKey);
    this.clickAt = [x, y];
    this.setTip(null);
    if (this.v) {
      this.v.hover = null;
      this.v.hoverBlast = null;
    }
    this.dirty = true;
    this.kick();
  }
  private onDbl(e: MouseEvent) {
    const v = this.v;
    if (!v) return;
    const [x, y] = this.local(e);
    const hit = pick(v, x, y);
    if (v.tool !== "explore") return; // a double-click never paints or removes a rule
    const n = hit && (hitNode(hit) || (hit.nest ? hit.nest.nest : null));
    if (n && n.cnt) {
      if (v.folded.has(n)) this.toggleFold(n);
      this.flyToNode(n, 0.8);
      return;
    }
    if (hit && hit.leaf) {
      this.flyToLeaf(hit.leaf, Math.min(7, Math.max(v.cam.z * 2, 3.2)));
      return;
    }
    this.pushHist();
    this.zoomAt(x, y, 2.2);
  }
  private onWheel(e: WheelEvent) {
    if (!this.v) return;
    e.preventDefault();
    const [x, y] = this.local(e);
    const dy = e.deltaMode === 1 ? e.deltaY * 18 : e.deltaY,
      dx = e.deltaMode === 1 ? e.deltaX * 18 : e.deltaX;
    if (e.shiftKey || (!e.ctrlKey && Math.abs(dx) > Math.abs(dy))) {
      this.panBy(e.shiftKey ? dy || dx : dx, e.shiftKey ? 0 : dy);
      return;
    }
    // one wheel notch ≈ x1.15; a flick is clamped so one event never jumps from a leaf to the whole tree
    const f = Math.max(1 / 1.35, Math.min(1.35, Math.exp(-dy * 0.0014)));
    // a wheel gesture is a move Back can undo: one entry per gesture, not per notch
    const tw = performance.now();
    if (tw - this.lastWheel > 700) this.pushHist();
    this.lastWheel = tw;
    this.zoomAt(x, y, f);
  }

  /** Keyboard on the canvas (the component forwards keydown). */
  key(e: KeyboardEvent): boolean {
    const v = this.v;
    if (!v) return false;
    const k = e.key;
    if (k === "Escape") {
      this.setTip(null);
      v.hover = null;
      if (v.tool !== "explore") this.setTool("explore");
      else if (v.follow != null) this.stopFollow(true, true);
      else if (this.info || v.sel) this.closeInfo();
      else if (v.riskFile != null) this.focusRisk(null);
      else if (v.markLeaf) v.markLeaf = null;
      else return false;
    } else if (k === "+" || k === "=") this.zoomAt(v.W / 2, v.H / 2, 1.5);
    else if (k === "-") this.zoomAt(v.W / 2, v.H / 2, 1 / 1.5);
    else if (k === "0" || k === "Home") this.wholeTree();
    else if (k === "Backspace" && this.hist.length) this.goBack();
    else if (k === "ArrowLeft") this.panBy(-60, 0);
    else if (k === "ArrowRight") this.panBy(60, 0);
    else if (k === "ArrowUp") this.panBy(0, -60);
    else if (k === "ArrowDown") this.panBy(0, 60);
    else return false;
    this.dirty = true;
    this.kick();
    return true;
  }

  private onClick(sx: number, sy: number, shift: boolean) {
    const v = this.v,
      M = this.M;
    if (!v || !M) return;
    const hit = pick(v, sx, sy);
    if (v.tool !== "explore") {
      if (!hit) return;
      const tool = v.tool;
      if (hit.tag) {
        this.cb.removeZone(hit.tag);
        if (!shift) this.setTool("explore");
        return;
      }
      let target: { node?: TNode | null; file?: TFile | null } | null = null;
      if (hit.leaf) target = { file: hit.leaf.file };
      else if (hit.nest) target = hit.nest.nest ? { node: hit.nest.nest } : null; // the nest stands for its folder
      else if (hit.bird) {
        const f = hit.bird.file;
        target = f ? (S0 * v.cam.z >= 24 ? { file: f } : { node: f.node }) : null;
      } else if (hit.badge) target = { node: hit.badge.node };
      else if (hit.trunk) {
        this.cb.note("Paint a branch, not the trunk — the whole repo cannot be fenced");
        return;
      } else {
        const n = hitNode(hit);
        target = n ? { node: n } : null;
      }
      if (!target) {
        this.cb.note("Click a folder name, a branch or a leaf");
        return;
      }
      this.cb.paint(tool, target);
      if (!shift) this.setTool("explore");
      return;
    }
    if (!hit) {
      if (v.sel || this.info) this.closeInfo();
      return;
    }
    if (hit.tag) {
      // a rule tag opens the folder (or file) it fences; removing is on the card / Rules list
      if (hit.tag.node) this.selectNode(hit.tag.node);
      else if (hit.tag.file != null && M.files[hit.tag.file].leaf) this.selectLeaf(M.files[hit.tag.file].leaf!);
      return;
    }
    if (hit.bird) {
      this.toggleFollow(hit.bird.ag.key);
      return;
    }
    if (hit.nest) {
      if (hit.nest.nest) this.selectNode(hit.nest.nest);
      return;
    }
    if (hit.badge) {
      this.openBlast(hit.badge);
      return;
    }
    if (hit.leaf) {
      this.selectLeaf(hit.leaf);
      return;
    }
    if (hit.trunk) {
      this.selectNode(M.crown);
      return;
    }
    const n = hitNode(hit);
    if (!n) {
      if (hit.branch) this.selectNode(M.crown);
      return;
    }
    if (hit.clump) {
      if (v.folded.has(n)) {
        this.toggleFold(n);
        return;
      }
      this.selectNode(n);
      this.flyToNode(n);
      return;
    }
    if (hit.pile) {
      this.selectNode(n);
      this.flyToNode(n);
      return;
    }
    this.selectNode(n);
  }

  // --- selection / cards -----------------------------------------------------

  private syncSel() {
    const v = this.v;
    if (!v) return;
    const i = this.info;
    v.infoFile = i && i.kind === "file" ? i.f : null;
    v.infoNode = i && i.kind === "node" ? i.n : null;
    if (i && i.kind === "file" && i.f.leaf) {
      v.sel = { leaf: i.f.leaf, fid: i.f.id };
      v.markLeaf = i.f.leaf;
    } else if (i && i.kind === "node") v.sel = { node: i.n };
    else v.sel = null;
  }
  selectLeaf(l: Leaf, fly = false) {
    if (!this.v) return;
    if (!(this.info && this.info.kind === "file" && this.info.f === l.file)) this.v.pinBlast = null;
    this.info = { kind: "file", f: l.file };
    this.syncSel();
    if (fly) this.flyToLeaf(l);
    this.dirty = true;
    this.cb.changed();
  }
  selectFile(path: string, fly = true, zoom?: number) {
    const f = this.M?.byPath.get(path);
    if (!f || !f.leaf || !this.v) return false;
    // unfold anything hiding it
    for (let n: TNode | null = f.node; n; n = n.parent) this.v.folded.delete(n);
    this.selectLeaf(f.leaf, false);
    if (fly) this.flyToLeaf(f.leaf, zoom || Math.max(3.2, Math.min(5, this.v.cam.z)));
    return true;
  }
  selectNode(n: TNode) {
    if (!this.v) return;
    this.v.pinBlast = null;
    this.info = { kind: "node", n };
    this.syncSel();
    this.dirty = true;
    this.cb.changed();
  }
  selectFolder(path: string) {
    const M = this.M;
    if (!M || !this.v) return false;
    let n = M.nodeOf.get(path) || null;
    if (!n) {
      let d = path;
      while (d && !M.nodeOf.has(d)) d = d.includes("/") ? d.slice(0, d.lastIndexOf("/")) : "";
      n = d ? M.nodeOf.get(d)! : null;
    }
    if (!n) return false;
    for (let q: TNode | null = n; q; q = q.parent) this.v.folded.delete(q);
    this.selectNode(n);
    this.flyToNode(n, 0.8);
    return true;
  }
  openBlast(bd: Badge) {
    const v = this.v,
      M = this.M;
    if (!v || !M) return;
    this.info = { kind: "blast", bd };
    this.syncSel();
    v.pinBlast = null;
    this.frameIds([...bd.ids]);
    this.dirty = true;
    this.cb.changed();
  }
  frameIds(ids: number[]) {
    const M = this.M;
    if (!M) return;
    let x0 = 1e18,
      y0 = 1e18,
      x1 = -1e18,
      y1 = -1e18;
    for (const id of ids) {
      const l = M.files[id]?.leaf;
      if (!l) continue;
      x0 = Math.min(x0, l.x);
      y0 = Math.min(y0, l.y);
      x1 = Math.max(x1, l.x);
      y1 = Math.max(y1, l.y);
    }
    if (x0 > x1) return;
    const c = this.fitBox(x0 - S0 * 3, y0 - S0 * 3, x1 + S0 * 3, y1 + S0 * 3, 0.85);
    c.z = Math.min(c.z, 4.5);
    this.navTo(c);
  }
  /** "Show the N that depend on it": pin the file's blast and frame it. */
  togglePinBlast(f: TFile) {
    const v = this.v,
      M = this.M;
    if (!v || !M) return;
    v.pinBlast = v.pinBlast === f.id ? null : f.id;
    if (v.pinBlast != null && f.leaf) this.frameIds([f.id, ...blastOf(M, f.id).h1]);
    this.dirty = true;
    this.cb.changed();
  }
  /** "⚠ Could break" on / off: the folders that import a change, as quiet gold territories on the map. */
  setRiskAll(on: boolean) {
    const v = this.v;
    if (!v || !!v.riskAll === on) return;
    v.riskAll = on;
    if (!on) v.riskFile = null;
    this.dirty = true;
    this.kick();
    this.cb.changed();
  }
  /** One changed file's dependents on the map, counted per folder (null clears). The camera stays: the whole
   * picture is the point — "which parts of the tree lean on this file". */
  focusRisk(fid: number | null) {
    const v = this.v;
    if (!v) return;
    v.riskFile = v.riskFile === fid ? null : fid;
    this.dirty = true;
    this.kick();
    this.cb.changed();
  }
  /** Closing the card also ends the selection: no ring or pinned gold outlives it. */
  closeInfo() {
    const v = this.v;
    this.info = null;
    if (v) {
      v.sel = null;
      v.infoFile = null;
      v.infoNode = null;
      v.pinBlast = null;
    }
    this.dirty = true;
    this.cb.changed();
  }
  toggleFold(n: TNode) {
    const v = this.v;
    if (!v) return;
    if (v.folded.has(n)) v.folded.delete(n);
    else v.folded.add(n);
    this.dirty = true;
    this.cb.changed();
  }
  setTool(t: Tool) {
    const v = this.v;
    if (!v) return;
    v.tool = v.tool === t && t !== "explore" ? "explore" : t;
    this.cv.classList.toggle("tool-keep", v.tool === "keep");
    this.cv.classList.toggle("tool-only", v.tool === "only");
    this.needPick = true;
    this.dirty = true;
    this.cb.changed();
    this.kick();
  }
  /** Where the followed bird is (world), this frame or — before its first
   * frame — from its pose now. */
  birdAt(key: string): [number, number] | null {
    const v = this.v;
    if (!v) return null;
    const p = v.birdWorld && v.birdWorld[key];
    if (p) return p;
    const A = v.agents.find((a) => a.ag.key === key);
    return A ? birdWorldAt(v, A, performance.now() / 1000) : null;
  }
  /** The camera that puts world point p in the middle of the free canvas. */
  followCam(p: [number, number], z: number): Cam {
    const I = { ...this.insets, right: this.insets.right + (this.info ? this.cardInset : 0) };
    return { x: p[0] - (I.left - I.right) / 2 / z, y: p[1] - (I.top - I.bot) / 2 / z, z };
  }
  /** Follow a bird (a card, a chip or the bird itself was clicked): glide to it
   * — a jump under reduced motion — at a zoom where its folder and leaf names
   * show, then keep it centred as it flies, until the user pans, zooms, drags,
   * presses Escape or asks again. Asking for the followed bird stops; asking
   * for another switches. */
  toggleFollow(key: string) {
    const v = this.v;
    if (!v) return;
    if (v.follow === key) {
      this.stopFollow(false, true);
      return;
    }
    this.pushHist();
    if (this.info && this.info.kind !== "node") this.closeInfo(); // a stale file card would sit over the bird you asked to watch
    // where you were before following anyone: Esc (or asking for the same bird again) flies back there
    if (v.follow == null) this.followFrom = { ...(this.fly ? v.cam : this.camT) };
    v.follow = key;
    const from = this.fly ? v.cam : this.camT;
    const z = Math.min(7, Math.max(from.z, FOLLOW_Z));
    const p = this.birdAt(key);
    const c = p ? this.followCam(p, z) : { x: from.x, y: from.y, z };
    // a longer hop glides a little longer, so an off-screen bird is reached, not jumped to
    const hop = (Math.hypot(c.x - v.cam.x, c.y - v.cam.y) * Math.min(v.cam.z, z)) / Math.max(1, v.W);
    this.flyTo(c, Math.round(650 + Math.min(650, hop * 420)));
    this.cb.changed();
  }
  /** Stop following. `nav` = the user took the camera (a pan, a zoom, a jump): it stays where they put it.
   * `restore` = they let go of the bird (Esc, or its row again): back to the view they had before following. */
  stopFollow(nav = false, restore = false) {
    const v = this.v;
    if (!v || v.follow == null) return;
    const key = v.follow;
    v.follow = null;
    const back = this.followFrom;
    this.followFrom = null;
    if (restore && back) this.flyTo(back, 650);
    this.cb.changed();
    if (nav) {
      const A = v.agents.find((a) => a.ag.key === key);
      if (A) this.cb.note(`Stopped following ${A.ag.name}`);
    }
  }
  /** A path in a list (Activity, Session panel): select its leaf and fly there. */
  revealPath(path: string) {
    if (this.selectFile(path)) return true;
    return this.selectFolder(path);
  }
  /** Paint ring after a rule lands: pulse it if in view, else frame it. */
  showRule(n: TNode | null, f: TFile | null) {
    const v = this.v,
      M = this.M;
    if (!v || !M) return;
    const node = n || (f ? f.node : null);
    if (!node || !node.cnt) return;
    if (this.nodeInView(node)) v.pulse = { x: node.cx, y: node.cy, r: node.rad + S0, t0: performance.now() };
    else if (node.depth >= 1) {
      const c =
        node.kind === "pile"
          ? this.fitBox(node.cx - (node.pw || 30) * 2, M.groundY - (node.h || 10) * 4, node.cx + (node.pw || 30) * 2, M.groundY + (node.h || 10), 0.7)
          : this.fitBox(node.bx0 - S0 * 4, node.by0 - S0 * 4, node.bx1 + S0 * 4, node.by1 + S0 * 4, 0.55);
      c.z = Math.min(c.z, 3.5);
      this.navTo(c);
      v.pulse = { x: node.cx, y: node.cy, r: node.rad + S0, t0: performance.now() + 500 };
    }
    this.dirty = true;
    this.kick();
  }

  // --- hover -----------------------------------------------------------------

  private setTip(t: Tip | null) {
    const was = this.tip;
    if (!t && !was) return;
    this.tip = t;
    this.cv.style.cursor = t ? "pointer" : "";
    this.cb.changed();
  }
  private updateHover() {
    const v = this.v;
    if (!v || !this.needPick || !this.mouse || this.drag) return;
    this.needPick = false;
    if (this.clickAt) {
      if (v.hover) {
        v.hover = null;
        v.hoverBlast = null;
        this.setTip(null);
        this.dirty = true;
      }
      return;
    }
    const hit = pick(v, this.mouse[0], this.mouse[1]);
    const before = v.hover;
    v.hover = hit;
    v.hoverBlast = null;
    if (hit && hit.leaf) {
      const f = hit.leaf.file;
      if (v.agents.some((A) => A.edits.has(f.id))) v.hoverBlast = f.id;
      if (this.info && this.info.kind === "file" && this.info.f === f) {
        this.setTip(null);
        this.cv.style.cursor = "pointer";
        return;
      }
    }
    if (!sameHit(before, hit)) this.dirty = true;
    this.setTip(hit ? { x: this.mouse[0], y: this.mouse[1], hit } : null);
  }

  // --- frame loop ------------------------------------------------------------

  kick() {
    if (!this.raf && this.active) this.raf = requestAnimationFrame((t) => this.frame(t));
  }
  private frame(now: number) {
    this.raf = 0;
    const v = this.v;
    if (!v || !this.active || document.hidden) return;
    const dt = Math.min(100, now - (this.lastNow || now));
    this.lastNow = now;
    v.t = now / 1000;
    this.stepCamera(now, dt);
    const zi = v.fitZv > 0 && v.cam.z > v.fitZv * 1.35;
    if (zi !== this.zoomedIn) {
      this.zoomedIn = zi;
      this.cb.changed();
    }
    // perched birds bob at ~12 fps; flights, pulses and bonks need every frame; reduced motion needs neither
    if (v.anim && !this.reducedMotion) this.dirty = true;
    else if (!this.reducedMotion && v.agents.length && now - this.lastBob > 83) {
      this.lastBob = now;
      this.dirty = true;
    }
    if (this.dirty) {
      try {
        render(v, now);
      } catch (err) {
        console.error(err);
      }
      if (this.mini) {
        try {
          drawMini(v, this.mini, (x, y) => this.screenToWorld(x, y));
        } catch {
          /* minimap is optional */
        }
      }
      this.dirty = false;
    }
    try {
      this.updateHover();
    } catch (err) {
      console.error(err);
    }
    const moving = !!this.fly || this.dirty || this.camMoving() || (v.anim && !this.reducedMotion) || (!this.reducedMotion && v.agents.length > 0);
    if (moving) this.kick();
  }
  private camMoving(): boolean {
    const v = this.v;
    if (!v) return false;
    const c = v.cam,
      T = this.camT;
    return c.x !== T.x || c.y !== T.y || c.z !== T.z || v.follow != null;
  }

  /** Is node n the selected one / under it? (for the cards) */
  under(n: TNode | null, anc: TNode) {
    return isUnder(n, anc);
  }
}

function sameHit(a: Hit | null, b: Hit | null): boolean {
  if (!a || !b) return a === b;
  return a.leaf === b.leaf && a.node === b.node && a.branch === b.branch && a.clump === b.clump && a.pile === b.pile && a.tag === b.tag && a.bird === b.bird && a.badge === b.badge && a.trunk === b.trunk && a.nest === b.nest;
}
