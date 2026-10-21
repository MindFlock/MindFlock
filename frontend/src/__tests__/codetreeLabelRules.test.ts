// The Code Tree's label rules (labels.ts) on synthetic leaves, folders and occupancy: every name is ATTACHED to the
// one thing it names (a leaf's tip, else its base with a hairline; a clump's rim where its twig comes in, else on
// the twig; along a branch, else beside its fork on a dot) — or it is not drawn.
import { describe, expect, it } from "vitest";
import {
  BASE_GAP,
  TIP_GAP,
  GAP,
  GUTTER,
  HAIR_MIN,
  LEAF_H,
  LEAF_PAD,
  LOUD_RANK,
  LeafField,
  Occupancy,
  RIM_MAX_TILT,
  candidatesForBranch,
  candidatesForClump,
  candidatesForLeaf,
  convexOverlap,
  insideView,
  leafRank,
  leafScreenGeom,
  leafShown,
  leafSkip,
  placeLabel,
  placeLabels,
  rimPoint,
  tagDoing,
  type R4,
} from "../lib/codetree/labels";
import type { Leaf } from "../lib/codetree/model";

// world == screen (z = 1)
const toS = (x: number, y: number): [number, number] => [x, y];
let nid = 0;
function leaf(x: number, y: number, ang: number, len = 20, extra: Partial<Leaf> = {}, lines = 10): Leaf {
  const id = nid++;
  return { x, y, ang, len, region: "crown", shade: 0, id, file: { id, name: `f${id}.ts`, lines } as unknown as Leaf["file"], ...extra } as Leaf;
}
const TW = 50; // a name's text width
const W = 800,
  H = 600;
const vp = { W, H };
const free = { fits: () => true };

describe("rule 1: a leaf's name is at its TIP, reading outward", () => {
  it("pointing right: left-aligned text starting TIP_GAP px past the tip, centred on it, tied back by a hairline", () => {
    const a = leafScreenGeom(leaf(100, 100, 0), 1, toS);
    expect(a.tip).toEqual([110, 100]);
    const [tip] = candidatesForLeaf(a, TW);
    expect(tip.side).toBe("tip");
    expect(tip.R[0]).toBeCloseTo(110 + TIP_GAP);
    expect((tip.R[1] + tip.R[3]) / 2).toBeCloseTo(100);
    expect(tip.R[2] - tip.R[0]).toBeCloseTo(TW + 2 * LEAF_PAD);
    expect(tip.R[3] - tip.R[1]).toBeCloseTo(LEAF_H);
    // F3: the name stands clear of its tip and a 1 px hairline runs from the tip to the box
    expect(TIP_GAP).toBeGreaterThan(HAIR_MIN);
    expect(tip.hair).not.toBeNull();
    expect(tip.hair!.slice(0, 2)).toEqual([110, 100]);
    expect(tip.hair![2]).toBeCloseTo(110 + TIP_GAP);
  });
  it("pointing left: right-aligned text ending TIP_GAP px before the tip", () => {
    const [tip] = candidatesForLeaf(leafScreenGeom(leaf(100, 100, Math.PI), 1, toS), TW);
    expect(tip.R[2]).toBeCloseTo(90 - TIP_GAP);
    expect((tip.R[1] + tip.R[3]) / 2).toBeCloseTo(100);
  });
  it("pointing up / down: horizontal text centred on the tip, above / below it", () => {
    const [up] = candidatesForLeaf(leafScreenGeom(leaf(100, 100, -Math.PI / 2), 1, toS), TW);
    expect(up.R[3]).toBeCloseTo(90 - TIP_GAP);
    expect((up.R[0] + up.R[2]) / 2).toBeCloseTo(100);
    const [down] = candidatesForLeaf(leafScreenGeom(leaf(100, 100, Math.PI / 2), 1, toS), TW);
    expect(down.R[1]).toBeCloseTo(110 + TIP_GAP);
    expect((down.R[0] + down.R[2]) / 2).toBeCloseTo(100);
  });
  it("then slid to hang off the tip by a corner: raised / lowered for a sideways tip, shifted for an upright one", () => {
    const cs = candidatesForLeaf(leafScreenGeom(leaf(100, 100, -0.5), 1, toS), TW).filter((c) => c.side === "tip");
    expect(cs.length).toBe(3);
    const [, up, dn] = cs;
    // a tip pointing up-right hangs its name above the tip line first
    expect(up.R[3]).toBeLessThan(dn.R[1] + 1e-6);
    expect(up.R[0]).toBeCloseTo(dn.R[0]);
    const us = candidatesForLeaf(leafScreenGeom(leaf(100, 100, -Math.PI / 2), 1, toS), TW).filter((c) => c.side === "tip");
    expect(us[1].R[0]).toBeCloseTo(100); // right of the tip, its left edge on the tip's line
    expect(us[2].R[2]).toBeCloseTo(100);
  });
  it("whatever the slant, no tip spot covers its own leaf, each is TIP_GAP from the tip and carries a hairline", () => {
    for (let k = 0; k < 24; k++) {
      const l = leaf(300, 300, (k / 24) * Math.PI * 2, 30);
      const a = leafScreenGeom(l, 1, toS);
      for (const tip of candidatesForLeaf(a, TW).filter((c) => c.side === "tip")) {
        expect(convexOverlap(tip.q, a.quad)).toBe(false);
        const dx = Math.max(tip.R[0] - a.tip[0], 0, a.tip[0] - tip.R[2]),
          dy = Math.max(tip.R[1] - a.tip[1], 0, a.tip[1] - tip.R[3]);
        expect(Math.hypot(dx, dy)).toBeLessThanOrEqual(TIP_GAP + 1e-6);
        expect(tip.hair).not.toBeNull();
      }
    }
  });
  it("a changed leaf is drawn at least 10.5 px long each way: its tip moves out with it", () => {
    const a = leafScreenGeom(leaf(100, 100, 0, 4), 1, toS, 10.5);
    expect(a.tip[0]).toBeCloseTo(110.5);
  });
});

describe("rule 1: the one alternative is the BASE side, with a hairline", () => {
  it("reads away from the tip, BASE_GAP off the base, tethered by a hairline", () => {
    const a = leafScreenGeom(leaf(100, 100, 0), 1, toS);
    const cs = candidatesForLeaf(a, TW);
    expect(cs.map((c) => c.side)).toEqual(["tip", "tip", "tip", "base"]);
    const base = cs[3];
    expect(base.R[2]).toBeCloseTo(90 - BASE_GAP);
    expect(base.hair).not.toBeNull();
    expect(base.hair!.slice(0, 2)).toEqual([90, 100]);
    expect(base.hair![2]).toBeCloseTo(base.R[2]);
  });
  it("a hairline exactly when the gap is wider than HAIR_MIN", () => {
    const a = leafScreenGeom(leaf(100, 100, 0), 1, toS);
    expect(candidatesForLeaf(a, TW, LEAF_H, HAIR_MIN)[0].hair).toBeNull();
    expect(candidatesForLeaf(a, TW, LEAF_H, HAIR_MIN + 1)[0].hair).not.toBeNull();
    expect(candidatesForLeaf(a, TW, LEAF_H, TIP_GAP, HAIR_MIN).find((c) => c.side === "base")!.hair).toBeNull();
  });
});

describe("rule 5: edges and ambiguity", () => {
  it("a name crossing the canvas edge is not drawn there: the base side, else nothing", () => {
    const nearRight = leafScreenGeom(leaf(W - 30, 100, 0), 1, toS);
    expect(placeLabel(candidatesForLeaf(nearRight, TW), free, vp)!.side).toBe("base");
    // too long for either side
    expect(placeLabel(candidatesForLeaf(nearRight, W), free, vp)).toBeNull();
    expect(insideView([0, 10, 50, 20], vp)).toBe(false);
    expect(insideView([2, 10, 50, 20], vp)).toBe(true);
  });
  it("a name touching another leaf's outline is not drawn there", () => {
    const self = leaf(100, 100, 0),
      right = leaf(140, 100, 0),
      left = leaf(60, 100, 0);
    const f = new LeafField(W, H);
    for (const l of [self, right]) f.add(l, leafScreenGeom(l, 1, toS).quad);
    const cs = candidatesForLeaf(leafScreenGeom(self, 1, toS), TW);
    const skip = leafSkip(self, false, () => false);
    expect(placeLabel(cs, free, vp, { field: f, skip })!.side).toBe("base");
    f.add(left, leafScreenGeom(left, 1, toS).quad);
    expect(placeLabel(cs, free, vp, { field: f, skip })).toBeNull();
  });
  it("its own leaf never blocks it; a LOUD name may lie over calm leaves, never over another loud one", () => {
    const self = leaf(100, 100, 0),
      calm = leaf(140, 100, 0),
      loud = leaf(60, 100, 0);
    const isLoud = (o: Leaf) => o === loud || o === self;
    expect(leafSkip(self, false, isLoud)(self)).toBe(true);
    expect(leafSkip(self, false, isLoud)(calm)).toBe(false);
    expect(leafSkip(self, true, isLoud)(calm)).toBe(true);
    expect(leafSkip(self, true, isLoud)(loud)).toBe(false);
  });
  it("names keep a GUTTER from what is taken: two never abut", () => {
    const occ = new Occupancy(W, H);
    const [tip] = candidatesForLeaf(leafScreenGeom(leaf(100, 100, 0), 1, toS), TW);
    occ.add(tip.R[2] + GUTTER - 0.5, tip.R[1], tip.R[2] + 40, tip.R[3]);
    expect(placeLabel([tip], occ, vp)).toBeNull();
    const occ2 = new Occupancy(W, H);
    occ2.add(tip.R[2] + GUTTER + 0.5, tip.R[1], tip.R[2] + 40, tip.R[3]);
    expect(placeLabel([tip], occ2, vp)).toBe(tip);
  });
});

describe("rule 2: priority and one name per leaf", () => {
  it("hovered > selected > a bird's file > changed > planned > read > could-break > the rest by size", () => {
    const L = Array.from({ length: 9 }, (_, i) => leaf(i * 30, 0, 0, 20, {}, i === 8 ? 5000 : 1));
    const [hov, sel, bird, chg, plan, read, gold, small, big] = L;
    const ctx = {
      hovered: hov,
      selected: sel,
      bird: new Set([bird.file.id]),
      changed: new Set([chg.file.id]),
      planned: new Set([plan.file.id]),
      read: new Set([read.file.id]),
      gold: new Set([gold.file.id]),
    };
    const order = [...L].sort((a, b) => leafRank(b, ctx) - leafRank(a, ctx));
    expect(order).toEqual([hov, sel, bird, chg, plan, read, gold, big, small]);
    // the loud tiers are exactly the ones whose leaf is coloured or ringed
    expect(leafRank(chg, ctx)).toBeGreaterThanOrEqual(LOUD_RANK);
    expect(leafRank(plan, ctx)).toBeLessThan(LOUD_RANK);
  });
  it("placement follows priority: the first name claims the shared spot", () => {
    const a = leafScreenGeom(leaf(100, 100, 0), 1, toS),
      b = leafScreenGeom(leaf(100, 100, 0), 1, toS); // a second name wanting the very same spots
    const out = placeLabels([{ key: "first", cands: candidatesForLeaf(a, TW) }, { key: "second", cands: candidatesForLeaf(b, TW) }], new Occupancy(W, H), vp);
    // (a level tip's slid spots overlap its centred one, so the second goes to the base)
    expect(out.map((o) => [o.key, o.c.side])).toEqual([
      ["first", "tip"],
      ["second", "base"],
    ]);
  });
  it("a bird's tag drops the file when the leaf carries its name, and keeps it when the leaf could not", () => {
    expect(tagDoing("x.py", {}, true, false)).toBe(" · x.py");
    expect(tagDoing("x.py", {}, true, true)).toBe("");
    expect(tagDoing("x.py", { fly: true }, true, false)).toBe(" → x.py");
    expect(tagDoing("x.py", { fly: true }, true, true)).toBe("");
    expect(tagDoing("x.py", { blocked: true }, true, true)).toBe(" ✕");
    expect(tagDoing("x.py", { blocked: true }, true, false)).toBe(" ✕ x.py");
    expect(tagDoing("", { nest: true, fly: true }, false, false)).toBe(" → nest");
  });
});

describe("rule 6: hover reveals one clump's names", () => {
  it("every leaf of the hovered clump; not a calm leaf of another clump", () => {
    const t1 = {},
      t2 = {};
    const a = leaf(0, 0, 0, 20, { term: t1 as Leaf["term"] }),
      b = leaf(0, 0, 0, 20, { term: t2 as Leaf["term"] });
    expect(leafShown(a, 0, t1, null, false)).toBe(true);
    expect(leafShown(b, 0, t1, null, false)).toBe(false);
    expect(leafShown(b, 0, null, t2, false)).toBe(true); // the focused clump keeps its names
    expect(leafShown(b, LOUD_RANK, t1, null, false)).toBe(true); // a ranked file is named anywhere
    expect(leafShown(b, 0, null, null, true)).toBe(true); // inside the selected folder
  });
});

describe("rule 3: folder names on their own wood", () => {
  it("a clump hangs its name just outside the rim where its stem comes in, tangent to the rim", () => {
    // stem rising from below into a clump at (200, 200), r 40
    const stem: Array<[number, number]> = [
      [200, 320],
      [200, 280],
      [200, 230],
    ];
    const { u, q } = rimPoint(200, 200, 40, stem);
    expect(u[0]).toBeCloseTo(0);
    expect(u[1]).toBeCloseTo(1);
    expect(q).toEqual([200, 240]);
    const [rim, twig] = candidatesForClump(200, 200, 40, stem, 60, 16);
    expect(rim.side).toBe("rim");
    expect(rim.ang).toBeCloseTo(0);
    expect(rim.R[1]).toBeCloseTo(240 + GAP);
    expect((rim.R[0] + rim.R[2]) / 2).toBeCloseTo(200);
    // the alternative straddles the stem just past the rim
    expect(twig.side).toBe("twig");
    expect(twig.R[0] < 200 && twig.R[2] > 200).toBe(true);
    expect(twig.R[1]).toBeGreaterThan(240);
  });
  it("a slanted rim tilts the name; a steep one lays it flat, its nearest edge GAP off the rim", () => {
    const slant: Array<[number, number]> = [
      [200 + 100 * Math.sin(0.3), 200 + 100 * Math.cos(0.3)],
      [200, 200],
    ];
    const [r1] = candidatesForClump(200, 200, 40, slant, 60, 16);
    expect(Math.abs(r1.ang)).toBeGreaterThan(0.2);
    expect(Math.abs(r1.ang)).toBeLessThanOrEqual(RIM_MAX_TILT);
    const side: Array<[number, number]> = [
      [320, 200],
      [200, 200],
    ];
    const [r2] = candidatesForClump(200, 200, 40, side, 60, 16);
    expect(r2.ang).toBe(0);
    expect(r2.R[0]).toBeCloseTo(240 + GAP);
  });
  it("a long straight branch carries its name ALONG the wood, touching it", () => {
    const pts: Array<[number, number, number]> = [];
    for (let i = 0; i <= 10; i++) pts.push([100 + i * 30, 300, 4]);
    const cs = candidatesForBranch(pts, [400, 300], 80, 16);
    const along = cs.filter((c) => c.side === "along");
    expect(along.length).toBeGreaterThan(0);
    expect(along[0].ang).toBeCloseTo(0);
    expect(along[0].R[3]).toBeCloseTo(300 - 4 - 1); // on the outer side, 1 px off the wood's edge
    expect(along[0].cx).toBeCloseTo(250);
  });
  it("a short or steep branch puts its name beside its fork, touching a dot on it", () => {
    const steep: Array<[number, number, number]> = [];
    for (let i = 0; i <= 10; i++) steep.push([300, 500 - i * 30, 4]);
    const cs = candidatesForBranch(steep, [300, 200], 80, 16);
    expect(cs.every((c) => c.side === "fork")).toBe(true);
    for (const c of cs) {
      expect(c.dot).toEqual([300, 200]);
      const dx = Math.max(c.R[0] - 300, 0, 300 - c.R[2]),
        dy = Math.max(c.R[1] - 200, 0, 200 - c.R[3]);
      expect(Math.hypot(dx, dy)).toBeCloseTo(2 + GAP);
    }
    const short: Array<[number, number, number]> = [
      [100, 300, 3],
      [140, 300, 3],
    ];
    expect(candidatesForBranch(short, [140, 300], 80, 16).every((c) => c.side === "fork")).toBe(true);
  });
  it("a curved run is not used for a name along it", () => {
    const arc: Array<[number, number, number]> = [];
    for (let i = 0; i <= 20; i++) {
      const a = Math.PI + (i / 20) * Math.PI;
      arc.push([300 + Math.cos(a) * 60, 300 + Math.sin(a) * 60, 3]);
    }
    expect(candidatesForBranch(arc, [360, 300], 150, 16).some((c) => c.side === "along")).toBe(false);
  });
});

describe("occupancy grid", () => {
  it("answers exactly like a linear scan", () => {
    let seed = 7;
    const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
    const occ = new Occupancy(W, H, 48);
    const list: R4[] = [];
    for (let i = 0; i < 200; i++) {
      const x = rnd() * (W + 200) - 100,
        y = rnd() * (H + 200) - 100,
        r: R4 = [x, y, x + rnd() * 120, y + rnd() * 40];
      occ.add(...r);
      list.push(r);
    }
    for (let i = 0; i < 500; i++) {
      const x = rnd() * W,
        y = rnd() * H,
        q: R4 = [x, y, x + rnd() * 80, y + rnd() * 20];
      const lin = !list.some((o) => q[2] > o[0] && q[0] < o[2] && q[3] > o[1] && q[1] < o[3]);
      expect(occ.fits(...q)).toBe(lin);
    }
  });
  it("a thing's own reserved room (a selection ring's square) does not block its own name", () => {
    const occ = new Occupancy(W, H);
    occ.add(90, 90, 110, 110);
    expect(occ.fits(100, 95, 140, 105)).toBe(false);
    expect(occ.fits(100, 95, 140, 105, [85, 85, 115, 115])).toBe(true);
  });
});

describe("geometry", () => {
  it("convex overlap", () => {
    const sq = (x: number, y: number, s: number) => [x, y, x + s, y, x + s, y + s, x, y + s];
    expect(convexOverlap(sq(0, 0, 10), sq(5, 5, 10))).toBe(true);
    expect(convexOverlap(sq(0, 0, 10), sq(11, 0, 10))).toBe(false);
    // a diamond whose bounding box overlaps the square, but not the diamond itself
    expect(convexOverlap(sq(0, 0, 10), [8, 13, 13, 8, 18, 13, 13, 18])).toBe(false);
  });
});
