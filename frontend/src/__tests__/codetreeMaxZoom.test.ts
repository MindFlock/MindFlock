/// <reference types="vite/client" />
// Code Tree, round 2 (F2): at the camera's closest zoom every leaf you are looking at is named at its tip. The densest
// changed clump (every leaf loud, so no name may lie over a neighbour) is the hard case: centred on it at MAX_Z, every
// leaf of it whose name has room inside the free canvas carries that name. Runs on the in-repo scenario, and on the
// `sc` fixture's observability/dashboards clump (19 changed leaves, the judge's shot) when the prototype fixtures are
// on this machine (~/mindflock-prototypes/map-ux/fixtures/sc_snap.json).
import { beforeAll, describe, expect, it } from "vitest";
import type { CodeMapLive, FeedRecord } from "../api/types";
import { MAX_Z } from "../components/grid/codemap/treeCtl";
import { prepGeometry, render } from "../lib/codetree/draw";
import { rawFromSnapshot } from "../lib/codetree/input";
import { LiveBirds } from "../lib/codetree/live";
import { buildModel, type Model, type TTerm } from "../lib/codetree/model";
import { readPalette } from "../lib/codetree/palette";
import type { AgentState, TreeView } from "../lib/codetree/types";
import { COL_GAP, candidatesForLeaf, columnStrip, insideView, leafScreenGeom, nameColumn, type R4 } from "../lib/codetree/labels";
import scenarioRaw from "./fixtures/codetree_scenario.json?raw";

const NODE = (globalThis as unknown as { process: { env: Record<string, string | undefined> } }).process;
const fs = (await import(/* @vite-ignore */ "node:" + "fs")) as { existsSync(p: string): boolean; readFileSync(p: string, enc: string): string };
const FIXD = (NODE.env.HOME || "/nonexistent") + "/mindflock-prototypes/map-ux/fixtures/";
const HAVE_SC = fs.existsSync(FIXD + "sc_snap.json") && fs.existsSync(FIXD + "sc_live.json");
const HAVE_MF = fs.existsSync(FIXD + "mf_snap.json") && fs.existsSync(FIXD + "mf_live.json");

function stubCtx(): CanvasRenderingContext2D {
  const st: Record<string, unknown> = { font: "10px sans-serif", globalAlpha: 1 };
  const grad = { addColorStop() {} };
  return new Proxy(st, {
    get(o, k: string) {
      if (k in o) return o[k];
      if (k === "measureText")
        return (t: string) => {
          const m = /(\d+(?:\.\d+)?)px/.exec(String(o.font));
          return { width: String(t).length * (m ? parseFloat(m[1]) : 10) * 0.56 };
        };
      if (k === "createLinearGradient" || k === "createRadialGradient" || k === "createConicGradient") return () => grad;
      if (k === "getImageData") return () => ({ data: new Uint8ClampedArray(4) });
      return () => undefined;
    },
    set(o, k: string, val) {
      o[k] = val;
      return true;
    },
  }) as unknown as CanvasRenderingContext2D;
}
function installDom() {
  const g = globalThis as Record<string, unknown>;
  if (typeof g.Path2D === "undefined")
    g.Path2D = class {
      moveTo() {}
      lineTo() {}
      bezierCurveTo() {}
      quadraticCurveTo() {}
      arc() {}
      ellipse() {}
      rect() {}
      closePath() {}
      addPath() {}
    };
  if (typeof g.document === "undefined")
    g.document = {
      createElement: () => ({ width: 1, height: 1, getContext: () => stubCtx() }),
      documentElement: { classList: { contains: () => false } },
    };
}

// one Map pane at 1440x900: the agents column on the left, the activity card bottom right
const SZ = { W: 1157, H: 760, hud: [[5, 5, 235, 656], [846, 684, 1154, 756]] as Array<[number, number, number, number]>, left: 239, bot: 8 };

interface Case {
  M: Model;
  agents: AgentState[];
}

function birds(M: Model, feed: FeedRecord[], live: Partial<CodeMapLive>, title: string, now: number): AgentState[] {
  return new LiveBirds().update(M, {
    feed,
    live: { now, changed: [], ...live } as unknown as CodeMapLive,
    title,
    accent: "#a08cff",
    serverNow: now,
    viewNow: now,
    zones: [],
  });
}

/** the view centred on clump t's middle (in the free canvas) at MAX_Z, the clump hovered or not */
function viewAt(c: Case, t: TTerm, hover = false): TreeView {
  const z = MAX_Z;
  const fx = (SZ.left + SZ.W - 8) / 2,
    fy = (8 + SZ.H - SZ.bot) / 2;
  const cam = { x: t.cx - (fx - SZ.W / 2) / z, y: t.cy - (fy - SZ.H / 2) / z, z };
  return {
    g: stubCtx(), M: c.M, P: readPalette(null), agents: c.agents, W: SZ.W, H: SZ.H, dpr: 1, cam, zones: [], folded: new Set(), tool: "explore", t: 1000,
    playing: false, reducedMotion: true, hover: hover ? { clump: t.node } : null, sel: null, infoNode: null, infoFile: null, pinBlast: null, hoverBlast: null, pulse: null,
    follow: null, markLeaf: null, blastCache: new Map(), hudRects: SZ.hud, free: { x0: SZ.left, y0: 8, x1: SZ.W - 8, y1: SZ.H - SZ.bot }, fitZv: 0.5,
    noCache: true,
  } as TreeView;
}

const editedOf = (c: Case) => {
  const e = new Set<number>();
  for (const A of c.agents) for (const k of A.edits.keys()) e.add(k);
  return e;
};

/** Clump t at MAX_Z: the leaves it shows (tip in the free canvas, off every HUD panel), and those of them that carry
 * no name. Not hovered, a leaf counts only when one of its spots (tip, base) lies whole in the free canvas. */
function namesAt(c: Case, t: TTerm, hover = false): { shown: number; unnamed: string[]; v: TreeView } {
  const v = viewAt(c, t, hover);
  render(v, 1000);
  const toS = v.toS!;
  const named = new Set((v.labelHits || []).filter((h) => h.leaf).map((h) => h.leaf!.file.id));
  for (const b of v.birdScreen || []) if (b.A.file) named.add(b.A.file.id); // a perched bird's tag names its file
  const edited = editedOf(c);
  const F = v.free!;
  const hud = v.hudRects || [];
  const inFree = (R: number[]) => R[0] >= F.x0 && R[2] <= F.x1 && R[1] >= F.y0 && R[3] <= F.y1 && !hud.some((r) => R[2] > r[0] && R[0] < r[2] && R[3] > r[1] && R[1] < r[3]);
  let shown = 0;
  const unnamed: string[] = [];
  for (const l of t.leaves) {
    const a = leafScreenGeom(l, MAX_Z, toS, edited.has(l.file.id) ? 10.5 : 0);
    const counts = hover
      ? inFree([a.tip[0], a.tip[1], a.tip[0], a.tip[1]])
      : candidatesForLeaf(a, l.file.name.length * 10.5 * 0.56).some((k) => insideView(k.R, { W: v.W, H: v.H }) && inFree(k.R));
    if (!counts) continue;
    shown++;
    if (!named.has(l.file.id)) unnamed.push(`${l.file.path} tip@${a.tip.map(Math.round)}`);
  }
  return { shown, unnamed, v };
}

/** the densest clump (most leaves, changed ones counted double) of a model */
function densest(c: Case, changedOnly: boolean): TTerm {
  const edited = new Set<number>();
  for (const A of c.agents) for (const k of A.edits.keys()) edited.add(k);
  let best: TTerm | null = null,
    bs = -1;
  for (const t of c.M.terms) {
    if (!t.leaves.length || t.node.fAnc || t.node.kind === "root") continue;
    const ch = t.leaves.filter((l) => edited.has(l.file.id)).length;
    if (changedOnly && !ch) continue;
    const s = t.leaves.length + ch;
    if (s > bs) {
      bs = s;
      best = t;
    }
  }
  return best!;
}

/** a prototype fixture (snapshot + live record) as the Map draws it */
function caseOf(snapF: string, liveF: string, label: string): Case {
  const snap = JSON.parse(fs.readFileSync(FIXD + snapF, "utf8"));
  const live = JSON.parse(fs.readFileSync(FIXD + liveF, "utf8")) as CodeMapLive;
  const M = buildModel(rawFromSnapshot(snap, label), { quantise: true });
  prepGeometry(M);
  (M as Model & { geom?: boolean }).geom = true;
  return { M, agents: birds(M, live.feed as FeedRecord[], live, "sc-17455", live.now) };
}

let RIG: Case;
beforeAll(() => {
  installDom();
  const SC = JSON.parse(scenarioRaw) as { label: string; title: string; activity: string; snapshot: { files: Array<[string, number, number]>; edges: Array<[number, number]> }; feed: FeedRecord[]; others: CodeMapLive["others"]; plan: CodeMapLive["plan"] };
  const M = buildModel(rawFromSnapshot(SC.snapshot, SC.label), { quantise: true });
  prepGeometry(M);
  (M as Model & { geom?: boolean }).geom = true;
  RIG = { M, agents: birds(M, SC.feed, { activity: SC.activity, others: SC.others, plan: SC.plan } as Partial<CodeMapLive>, SC.title, 0) };
});

describe("max zoom names every leaf you are looking at (F2)", () => {
  it.runIf(HAVE_SC)("sc: observability/dashboards (12 changed code leaves) — every leaf at its own tip, no hover needed", () => {
    const c = caseOf("sc_snap.json", "sc_live.json", "sitecheck-bot");
    const t = c.M.terms.find((q) => q.node.path === "observability/dashboards")!;
    // (its 7 dashboard .json files are support roots now, under "observability · non-code")
    expect(t.leaves.length).toBeGreaterThanOrEqual(12);
    expect(t.leaves.every((l) => l.file.kind === "c")).toBe(true);
    expect(c.M.files.some((f) => f.path.startsWith("observability/dashboards/") && f.kind !== "c" && f.leaf!.region === "root")).toBe(true);
    const r = namesAt(c, t);
    expect(r.shown).toBeGreaterThanOrEqual(12);
    expect(r.unnamed).toEqual([]);
  });
  it("the rig's densest changed clump: nearly every leaf at its tip; hovered, every one", () => {
    const t = densest(RIG, true);
    const r = namesAt(RIG, t);
    expect(r.shown).toBeGreaterThan(15);
    expect(r.unnamed.length).toBeLessThanOrEqual(Math.ceil(r.shown * 0.15));
    expect(namesAt(RIG, t, true).unnamed).toEqual([]);
  });
  it("the rig's biggest root fan, hovered: every tip on screen is named (tip or column)", () => {
    const t = RIG.M.terms.filter((q) => q.node.kind === "root" && q.leaves.length).sort((a, b) => b.leaves.length - a.leaves.length)[0];
    const r = namesAt(RIG, t, true);
    expect(r.shown).toBeGreaterThan(10);
    expect(r.unnamed).toEqual([]);
  });
  it.runIf(HAVE_MF)("mf: the biggest root fan (tests for backend › web), hovered: every tip on screen is named", () => {
    const c = caseOf("mf_snap.json", "mf_live.json", "MindFlock");
    const t = c.M.terms.filter((q) => q.node.kind === "root" && q.leaves.length).sort((a, b) => b.leaves.length - a.leaves.length)[0];
    const r = namesAt(c, t, true);
    expect(r.shown).toBeGreaterThan(30);
    expect(r.unnamed).toEqual([]);
  });
});

describe("the name column (rule 6's fallback)", () => {
  const F: R4 = [240, 8, 1150, 752];
  const items = [
    { tx: 600, ty: 300, w: 90 },
    { tx: 620, ty: 296, w: 120 },
    { tx: 640, ty: 420, w: 60 },
    { tx: 610, ty: 100, w: 80 },
  ];
  const W = 120;
  it("stands right of the clump when there is room, rows left-aligned, in tip order, never overlapping", () => {
    const S = columnStrip([580, 90, 660, 430], W, F)!;
    expect(S.side).toBe(1);
    expect(S.over).toBe(false);
    expect(S.x0).toBeCloseTo(660 + COL_GAP);
    const c = nameColumn(items, S)!;
    expect(c.rows.length).toBe(4);
    for (const r of c.rows) expect(r.R[0]).toBeCloseTo(660 + COL_GAP);
    const ty = c.rows.map((r) => items[r.i].ty);
    expect([...ty].sort((a, b) => a - b)).toEqual(ty); // tip order: hairlines from tips to rows never cross
    for (let k = 1; k < c.rows.length; k++) expect(c.rows[k].R[1]).toBeGreaterThanOrEqual(c.rows[k - 1].R[3]);
    // a row sits at its tip's height when nothing pushes it
    expect(c.rows[0].y).toBeCloseTo(100);
    expect(c.rows[3].y).toBeCloseTo(420);
  });
  it("goes left when the right has no room; over the clump at the canvas edge when neither side has", () => {
    const L = columnStrip([580, 90, 1100, 430], W, F)!;
    expect(L.side).toBe(-1);
    expect(L.x1).toBeCloseTo(580 - COL_GAP);
    const O = columnStrip([250, 20, 1140, 740], W, F)!;
    expect(O.over).toBe(true);
    expect(O.x0).toBeGreaterThanOrEqual(F[0]);
    expect(O.x1).toBeLessThanOrEqual(F[2]);
  });
  it("keeps off HUD panels; a root's column stays underground; rows slide past what is taken", () => {
    const S = columnStrip([300, 90, 520, 430], W, F, [[500, 0, 700, 200]])!;
    expect(S.y0).toBeGreaterThanOrEqual(200);
    const U = columnStrip([580, 90, 660, 430], W, F, [], 350)!;
    for (const r of nameColumn(items, U)!.rows) expect(r.R[1]).toBeGreaterThanOrEqual(350);
    const S2 = columnStrip([580, 90, 660, 430], W, F)!;
    const taken: R4 = [600, 280, 900, 330];
    const c = nameColumn(items, S2, (R) => !(R[2] > taken[0] && R[0] < taken[2] && R[3] > taken[1] && R[1] < taken[3]))!;
    for (const r of c.rows) expect(r.R[3] <= taken[1] || r.R[1] >= taken[3]).toBe(true);
    expect(c.rows.length).toBe(4);
  });
  it("too many for the height: the first ones (priority order) keep rows, the last row says how many more", () => {
    const many = Array.from({ length: 80 }, (_, i) => ({ tx: 600, ty: 10 + i * 9, w: 50 }));
    const c = nameColumn(many, columnStrip([580, 10, 620, 730], W, F)!)!;
    expect(c.more).toBeGreaterThan(0);
    expect(c.rows.length + c.more).toBe(80);
    expect(Math.max(...c.rows.map((r) => r.i))).toBe(c.rows.length - 1);
    expect(c.moreR![3]).toBeLessThanOrEqual(F[3]);
  });
});
