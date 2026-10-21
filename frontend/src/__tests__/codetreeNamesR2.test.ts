/// <reference types="vite/client" />
// Code Tree, round 2: the judge's findings as rules (judge/VERDICT.md F3, F4, F5, F7, F9, F12, F14). Each rule is
// tested on its pure helper and, where the drawing decides, on the rig's scenario rendered for real (draw.ts against
// a recording 2D context, as codetreeLabels.test.ts does).
import { beforeAll, describe, expect, it } from "vitest";
import type { CodeMapLive, FeedRecord } from "../api/types";
import { birdWords, IDLE_SAY_S } from "../components/grid/codemap/BirdIndex";
import { FRESH_S, SIGN_ICON, SIGN_PAD, freshness, prepGeometry, render, signW } from "../lib/codetree/draw";
import { rawFromSnapshot } from "../lib/codetree/input";
import { chainTop1, folderNameVariants, loneSrc, sameAsParent } from "../lib/codetree/labels";
import { LiveBirds } from "../lib/codetree/live";
import { buildModel, isUnder, type Model, type TNode } from "../lib/codetree/model";
import { readPalette } from "../lib/codetree/palette";
import type { AgentState, TreeView } from "../lib/codetree/types";
import scenarioRaw from "./fixtures/codetree_scenario.json?raw";

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

interface Scenario {
  label: string;
  title: string;
  activity: string;
  snapshot: { files: Array<[string, number, number]>; edges: Array<[number, number]> };
  feed: FeedRecord[];
  others: CodeMapLive["others"];
  plan: CodeMapLive["plan"];
}
const SC = JSON.parse(scenarioRaw) as Scenario;
const SZ = { W: 1157, H: 760, hud: [[5, 5, 235, 656], [846, 684, 1154, 756]] as Array<[number, number, number, number]>, left: 239, bot: 8 };
let M: Model;
let agents: AgentState[];

function view(z: number | null, at?: [number, number]): TreeView {
  const B = M.bounds;
  const w = SZ.W - SZ.left - 8,
    h = SZ.H - 8 - SZ.bot;
  const fz = Math.min((w * 0.94) / (B.x1 - B.x0), (h * 0.94) / (B.y1 - B.y0));
  const zz = z ?? fz;
  const [cx, cy] = at || [(B.x0 + B.x1) / 2, (B.y0 + B.y1) / 2];
  const cam = { x: cx - (SZ.left - 8) / 2 / zz, y: cy - (8 - SZ.bot) / 2 / zz, z: zz };
  return {
    g: stubCtx(), M, P: readPalette(null), agents, W: SZ.W, H: SZ.H, dpr: 1, cam, zones: [], folded: new Set(), tool: "explore", t: 1000,
    playing: false, reducedMotion: true, hover: null, sel: null, infoNode: null, infoFile: null, pinBlast: null, hoverBlast: null, pulse: null,
    follow: null, markLeaf: null, blastCache: new Map(), hudRects: SZ.hud, free: { x0: SZ.left, y0: 8, x1: SZ.W - 8, y1: SZ.H - SZ.bot }, fitZv: fz,
    noCache: true,
  } as TreeView;
}

beforeAll(() => {
  installDom();
  M = buildModel(rawFromSnapshot(SC.snapshot, SC.label), { quantise: true });
  prepGeometry(M);
  (M as Model & { geom?: boolean }).geom = true;
  agents = new LiveBirds().update(M, {
    feed: SC.feed,
    live: { now: 0, activity: SC.activity, changed: [], others: SC.others, plan: SC.plan } as unknown as CodeMapLive,
    title: SC.title,
    accent: "#a08cff",
    serverNow: 0,
    viewNow: 0,
    zones: [],
  });
});

describe("F3: no file names at the whole view", () => {
  it("zoomed out (a leaf a few px long) no file carries a name — a perched bird's tag names its file instead", () => {
    const v = view(null);
    expect(v.cam.z * 10).toBeLessThan(9);
    render(v, 1000);
    expect((v.labelHits || []).filter((h) => h.leaf)).toEqual([]);
  });
});

describe("F4: never a bare count pill", () => {
  it("full path, then the folder's own name — the bare chip only for a nest, the open card or a rule", () => {
    expect(folderNameVariants("frontend › components", "components", true, false)).toEqual(["frontend › components", "components"]);
    expect(folderNameVariants("frontend › components", "components", true, true)).toEqual(["frontend › components", "components", ""]);
    expect(folderNameVariants("docs", "docs", true, false)).toEqual(["docs"]);
    expect(folderNameVariants("docs", "docs", false, true)).toEqual(["docs"]);
  });
});

describe("F5: one pill per chain, no lone src", () => {
  const mk = (name: string, parent: TNode | null, depth: number): TNode => {
    const n = { name, parent, depth, kids: [] as TNode[] } as unknown as TNode;
    if (parent) parent.kids!.push(n);
    return n;
  };
  it("a chain of equal counts from a top-level folder is named by that folder only", () => {
    const root = mk("repo", null, 0),
      fe = mk("frontend", root, 1),
      src = mk("src", fe, 2),
      comp = mk("components", src, 3),
      lib = mk("lib", src, 3);
    const lit = new Map<TNode, { n: number }>([
      [fe, { n: 121 }],
      [src, { n: 121 }],
      [comp, { n: 86 }],
      [lib, { n: 35 }],
    ]);
    expect(sameAsParent(src, lit)).toBe(true);
    expect(chainTop1(src, lit)).toBe(fe); // "frontend ●121", never also "frontend › src ●121"
    expect(chainTop1(comp, lit)).toBeNull(); // 86 of 121: a place of its own
    expect(chainTop1(fe, lit)).toBeNull();
    // a chain that starts below the top level keeps the old rule (named by its lit sub-folder)
    const be = mk("backend", root, 1),
      web = mk("web", be, 2),
      core = mk("core", web, 3);
    mk("providers", be, 2);
    const lit2 = new Map<TNode, { n: number }>([
      [be, { n: 50 }],
      [web, { n: 43 }],
      [core, { n: 43 }],
    ]);
    expect(chainTop1(core, lit2)).toBeNull();
  });
  it("a src that is its parent's only sub-folder is not a place", () => {
    const root = mk("repo", null, 0),
      fe = mk("frontend", root, 1),
      src = mk("src", fe, 2);
    expect(loneSrc(src)).toBe(true);
    mk("public", fe, 2);
    expect(loneSrc(src)).toBe(false);
  });
  it("on the rig's scenario, at every zoom band: no folder is named together with an ancestor carrying the same count", () => {
    for (const z of [null, 0.9, 1.6, 3]) {
      const v = view(z);
      render(v, 1000);
      const named = (v.labelHits || []).filter((h) => h.n && !h.crumb && h.n.depth >= 1).map((h) => h.n!);
      const lit = v.litInfo!;
      for (const a of named)
        for (const b of named)
          if (a !== b && a.depth === 1 && isUnder(b, a) && lit.get(a) && lit.get(b)) expect(lit.get(b)!.n === lit.get(a)!.n && chainTop1(b, lit) === a).toBe(false);
      for (const n of named) expect(loneSrc(n) && !v.agents.some((A) => A.nest === n)).toBe(false);
    }
  });
});

describe("F7: a code folder's non-code files are its own root, and say so", () => {
  // (the scaffold is gone: what stood on its lifts are support roots — codetreeSupportRoots.test.ts)
  it("on the rig, every root limb is tests for a branch, other tests, test data, a folder's non-code, or the tap root", () => {
    expect(M.roots.kids.length).toBeGreaterThan(0);
    for (const k of M.roots.kids) {
      const lb = k.label || "";
      expect(lb, k.path).toMatch(/^(tests for .+|other tests · .+\/|test data( · .+\/)?|.+ · non-code|docs, CI and config)$/);
      expect(lb).not.toMatch(/^tests: |^tests · tests|for tests for/);
      // a support limb names its folder; a test limb the code it tests
      if (k.support) expect(lb.startsWith("tests")).toBe(false);
      else expect(lb.startsWith("tests") || lb.startsWith("other tests")).toBe(true);
    }
  });
});

describe("F9: glow means now", () => {
  const A = (edits: Array<[number, number]>): AgentState => ({ edits: new Map(edits), clock: { server: 10_000, view: 50 } }) as unknown as AgentState;
  it("a change made seconds ago glows; a 10-day-old one, or one with no known time, does not", () => {
    const f = freshness({ agents: [A([[1, 10_000 - 10], [2, 10_000 - 864_000], [3, 0]])], t: 50 });
    expect(f.get(1)!).toBeCloseTo(1 - 10 / FRESH_S);
    expect(f.get(2)).toBe(0);
    expect(f.get(3)).toBe(0);
  });
  it("the glow fades with the view clock, and is gone FRESH_S after the edit", () => {
    const ag = A([[1, 10_000]]);
    expect(freshness({ agents: [ag], t: 50 + FRESH_S / 2 }).get(1)!).toBeCloseTo(0.5);
    expect(freshness({ agents: [ag], t: 50 + FRESH_S + 1 }).get(1)).toBe(0);
  });
  it("a bird with no clock (no server time known) never glows", () => {
    const ag = { edits: new Map([[1, 10_000]]) } as unknown as AgentState;
    expect(freshness({ agents: [ag], t: 50 }).get(1)).toBe(0);
  });
});

describe("F12: the keep-out sign's icon never covers its text", () => {
  it("the sign is as wide as its padding + the drawn icon + a gap + the rest of its text", () => {
    const g = { measureText: (t: string) => ({ width: t.length * 6 }) } as unknown as CanvasRenderingContext2D;
    expect(signW(g, "⛔ web core")).toBe(8 + SIGN_ICON + SIGN_PAD + 8 * 6 + 8);
    expect(signW(g, "✓ lib")).toBe(8 + SIGN_ICON + SIGN_PAD + 3 * 6 + 8);
    expect(signW(g, "⛔")).toBe(8 + SIGN_ICON + 8);
  });
});

describe("F14: an idle agent is idle", () => {
  it("the sidebar says Idle 481d: the line says how long and where it last was, never 'thinking'", () => {
    const w = birdWords("thinking", true, "", "Idle 481d", 0, { secs: 481 * 86400, where: "backend/web" });
    expect(w.now).toBe("idle 481d · last in backend/web");
    expect(w.now).not.toMatch(/thinking/);
  });
  it("no word from the sidebar, nothing touched for hours: idle 3h", () => {
    expect(birdWords("thinking", true, "", "", 0, { secs: 3 * 3600, where: "backend/web" }).now).toBe("idle 3h · last in backend/web");
  });
  it("a session at work that just hasn't touched a file for a while is still thinking", () => {
    expect(birdWords("thinking", true, "working", "Working", 0, { secs: 3 * 3600, where: "backend/web" }).now).toBe("in the nest, thinking");
    expect(birdWords("thinking", true, "", "", 0, { secs: IDLE_SAY_S - 5, where: "backend/web" }).now).toBe("in the nest, thinking");
  });
});
