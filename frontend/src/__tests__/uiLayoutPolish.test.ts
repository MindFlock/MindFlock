/** The intake lane's room is its FULL-detail size (lib/skyLayout
 * `FULL_DETAIL_LODS`, SkyView `laneFullHeight`). The lane draws compact at
 * far (its badges, chips and items hidden) and several times taller at 100 %;
 * lines are placed once (`assignSpots`), so a lane measured at Fit let lines
 * settle where the full lane later drew over them. Its measure is now the
 * tallest of what it draws and a hidden copy at each full level of detail;
 * when it grows where it sits, the auto-placed lines it now covers make way
 * (`grownOver`) — a pinned line never moves. */

import { describe, expect, it } from "vitest";
import { code } from "./sourceText";
import { laneFullHeight } from "../components/flights/SkyView";
import {
  FULL_DETAIL_LODS,
  INTAKE_KEY,
  LOD_MID,
  SKY_GAP,
  SKY_MARGIN,
  assignSpots,
  grownOver,
  intakeSize,
  lodFor,
  type PlacedLine,
  type Rect,
} from "../lib/skyLayout";

const overlap = (a: Rect, b: Rect) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

describe("the lane's full-detail levels", () => {
  it("are the levels that draw every part, at the zooms where they draw tallest", () => {
    // mid at its floor: the type's counter-scale is largest there.
    expect(FULL_DETAIL_LODS).toContainEqual({ lod: "mid", z: LOD_MID });
    expect(FULL_DETAIL_LODS.some((l) => l.lod === "near")).toBe(true);
    for (const { lod, z } of FULL_DETAIL_LODS) {
      expect(lod).not.toBe("far");
      if (lod === "mid") expect(lodFor(z)).toBe("mid");
    }
  });
});

/** A stand-in for the lane's slot: `.sky-intake[data-lod]` holding its
 * `.sky-lane`, whose height is what its level of detail draws. */
function fakeSlot(heights: Record<string, number>, lod = "far") {
  const parent = { kids: [] as unknown[], appendChild(n: unknown) { this.kids.push(n); } };
  const seen: Array<{ lod: string; z: string; tour: boolean; hidden: string }> = [];
  const make = (attrs: Record<string, string>) => {
    const style: Record<string, string> & { setProperty(k: string, v: string): void } = {
      setProperty(k: string, v: string) { style[k] = v; },
    } as never;
    const el = {
      attrs,
      style,
      parentElement: parent,
      setAttribute: (k: string, v: string) => void (attrs[k] = v),
      removeAttribute: (k: string) => void delete attrs[k],
      cloneNode: () => make({ ...attrs }),
      querySelector: () => ({
        get offsetHeight() {
          seen.push({ lod: attrs["data-lod"], z: style["--sky-z"], tour: "data-tour" in attrs, hidden: style.visibility });
          return heights[attrs["data-lod"]] ?? 0;
        },
      }),
      remove() {
        parent.kids = parent.kids.filter((k) => k !== el);
      },
    };
    return el;
  };
  const slot = make({ "data-lod": lod, "data-tour": "sky-intake" });
  return { slot: slot as unknown as HTMLElement, real: slot, parent, seen };
}

describe("laneFullHeight", () => {
  it("measures a hidden copy at every full level of detail and keeps the tallest", () => {
    const f = fakeSlot({ far: 300, mid: 1180, near: 1070 });
    expect(laneFullHeight(f.slot)).toBe(1180);
    expect(f.seen.map((s) => s.lod)).toEqual(FULL_DETAIL_LODS.map((l) => l.lod));
    expect(f.seen.map((s) => s.z)).toEqual(FULL_DETAIL_LODS.map((l) => String(l.z)));
    // Hidden, and never a second tour target.
    expect(f.seen.every((s) => s.hidden === "hidden" && !s.tour)).toBe(true);
  });

  it("leaves the drawn lane as it was and nothing behind", () => {
    const f = fakeSlot({ far: 300, mid: 900, near: 950 });
    expect(laneFullHeight(f.slot)).toBe(950);
    expect(f.parent.kids).toEqual([]);
    expect(f.real.attrs["data-lod"]).toBe("far");
    expect(f.real.attrs["data-tour"]).toBe("sky-intake");
  });

  it("is 0 with nothing to measure", () => {
    expect(laneFullHeight(null)).toBe(0);
    const orphan = { ...fakeSlot({ mid: 500 }).real, parentElement: null };
    expect(laneFullHeight(orphan as unknown as HTMLElement)).toBe(0);
  });
});

describe("placing lines round the lane", () => {
  const area = { width: 1500, left: SKY_MARGIN, top: SKY_MARGIN };
  const lines = Array.from({ length: 12 }, (_, i) => ({ id: `l${i}`, w: 300, h: 190 }));

  it("the bug: a lane measured compact lets lines settle under the full lane", () => {
    const compact = { x: SKY_MARGIN, y: SKY_MARGIN, ...intakeSize(undefined, { w: 240, h: 550 }) };
    const spots = assignSpots([{ id: INTAKE_KEY, ...compact }], lines, area);
    const full: Rect = { ...compact, h: 1180 };
    expect(lines.some((l) => overlap({ ...spots[l.id], w: l.w, h: l.h }, full))).toBe(true);
  });

  it("the fix: placed round the full-detail lane, no line is ever under it", () => {
    const lane = { x: SKY_MARGIN, y: SKY_MARGIN, ...intakeSize(undefined, { w: 240, h: 1180 }) };
    const spots = assignSpots([{ id: INTAKE_KEY, ...lane }], lines, area);
    for (const l of lines) expect(overlap({ ...spots[l.id], w: l.w, h: l.h }, lane)).toBe(false);
  });
});

describe("grownOver", () => {
  const was: Rect = { x: 40, y: 40, w: 240, h: 600 };
  const beside: PlacedLine = { id: "beside", x: 40 + 240 + SKY_GAP, y: 40, w: 300, h: 190 };
  const below: PlacedLine = { id: "below", x: 40, y: 40 + 600 + SKY_GAP, w: 300, h: 190 };
  const far: PlacedLine = { id: "far", x: 40, y: 3000, w: 300, h: 190 };

  it("names the lines a lane that grew taller now covers", () => {
    expect(grownOver(was, { ...was, h: 1000 }, [beside, below, far])).toEqual(["below"]);
  });

  it("names the lines a lane expanded wider now covers", () => {
    expect(grownOver(was, { ...was, w: 420 }, [beside, below, far])).toEqual(["beside"]);
  });

  it("shoves nothing for a move, a shrink, or a first measure", () => {
    expect(grownOver(was, { ...was, x: 400, h: 1000 }, [beside, below])).toEqual([]);
    expect(grownOver(was, { ...was, h: 300 }, [beside, below])).toEqual([]);
    expect(grownOver(null, { ...was, h: 1000 }, [below])).toEqual([]);
    expect(grownOver(was, null, [below])).toEqual([]);
  });

  it("leaves a line the lane already covered (dropped there) alone", () => {
    const under: PlacedLine = { id: "under", x: 60, y: 100, w: 100, h: 100 };
    expect(grownOver(was, { ...was, h: 1000 }, [under, below])).toEqual(["below"]);
  });
});

describe("SkyView keeps the lane's room at full detail", () => {
  const v = code("components/flights/SkyView.tsx");

  it("measures the lane at full detail, from its slot", () => {
    expect(v).toContain("if (id === INTAKE_KEY) h = Math.max(h, laneFullHeight(intakeEl.current));");
    expect(v).toContain("ref={intakeSlot}");
    // Items arriving where far hides them change nothing drawn: watched.
    expect(v).toMatch(/new MutationObserver\(/);
    expect(v).toContain('onMeasure(INTAKE_KEY, lane.offsetWidth, lane.offsetHeight)');
  });

  it("frees the spots of the auto-placed lines a grown lane covers — never a pinned one's", () => {
    expect(v).toContain("const r = id !== INTAKE_KEY && !layout[id]?.pinned ? rectOf(id) : null;");
    expect(v).toContain("const out = grownOver(was, now, movable);");
    expect(v).toContain("const now = mode !== \"one\" && intakeOn && !drag && at ? { ...at, ...intakeBox } : null;");
  });
});
