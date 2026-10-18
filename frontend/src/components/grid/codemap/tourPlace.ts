/** Where the Map tour's card goes (MapTour.tsx hands this to the shared
 * CoachTour): beside the ring of the part it explains, on the side with the
 * most room, off the tree and the HUD panels; docked in a corner of the canvas
 * (or an edge's middle) when the step is about the tree as a whole — never
 * over the centre of what it explains, and off the canopy, the roots and the
 * ground piles with their names wherever there is room. Pure, for the tests, except `mapScene` (reads the tab's DOM). */

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** What the card should keep off, in the Map root's px: the canvas (`area`),
 * the picture on it (`tree`, from the stage's data-tree) and its parts — the
 * canopy, the roots, each ground pile and its name — and the HUD panels. */
export interface MapScene {
  area: Rect;
  tree: Rect | null;
  parts?: Rect[];
  avoid: Rect[];
}

const CARD_W = 320;
const GAP = 12;
const EDGE = 8;

const overlap = (a: Rect, b: Rect) =>
  Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x)) * Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
const holds = (a: Rect, x: number, y: number) => x > a.x && x < a.x + a.w && y > a.y && y < a.y + a.h;
const inside = (a: Rect, b: Rect) => a.x >= b.x - 1 && a.y >= b.y - 1 && a.x + a.w <= b.x + b.w + 1 && a.y + a.h <= b.y + b.h + 1;

export function placeMapCard(t: Rect | null, W: number, H: number, cardH: number, s: MapScene | null): { left: number; top: number } {
  const cw = Math.min(CARD_W, W - 2 * EDGE);
  const clamp = (x: number, y: number): Rect => ({
    x: Math.max(EDGE, Math.min(x, W - cw - EDGE)),
    y: Math.max(EDGE, Math.min(y, H - cardH - EDGE)),
    w: cw,
    h: cardH,
  });
  const area = s ? s.area : { x: 0, y: 0, w: W, h: H };
  const tree = s && s.tree ? s.tree : null;
  const avoid = s ? s.avoid : [];
  const parts = (s && s.parts) || [];
  // what the card must never sit on the middle of
  const focus = t || tree || area;
  const fx = focus.x + focus.w / 2,
    fy = focus.y + focus.h / 2;
  // covering a part of the picture costs twice its area, the empty rest of the picture's box a little, a panel
  // its area (half for the panel the target sits in)
  const cost = (c: Rect) =>
    (tree ? overlap(c, tree) * (parts.length ? 0.25 : 1) : 0) +
    parts.reduce((a, r) => a + 2 * overlap(c, r), 0) +
    avoid.reduce((a, r) => a + (t && inside(t, r) ? 0.5 : 1) * overlap(c, r), 0);
  const pick = (cands: Rect[]): Rect | null => {
    let best: Rect | null = null,
      bc = Infinity;
    for (const c of cands) {
      if (holds(c, fx, fy) || (t && overlap(c, t) > 0)) continue;
      const k = cost(c);
      if (k < bc - 0.5) {
        best = c;
        bc = k;
      }
    }
    return best;
  };
  const corners = [
    clamp(area.x + area.w - cw - EDGE, area.y + EDGE), // top right: usually sky
    clamp(area.x + area.w - cw - EDGE, area.y + area.h - cardH - EDGE),
    clamp(area.x + EDGE, area.y + area.h - cardH - EDGE),
    clamp(area.x + EDGE, area.y + EDGE),
    // the middles of the edges, when every corner is taken (a card, the agents column, a pile)
    clamp(area.x + (area.w - cw) / 2, area.y + EDGE),
    clamp(area.x + area.w - cw - EDGE, area.y + (area.h - cardH) / 2),
    clamp(area.x + (area.w - cw) / 2, area.y + area.h - cardH - EDGE),
    clamp(area.x + EDGE, area.y + (area.h - cardH) / 2),
  ];
  if (!t) {
    const c = pick(corners) || corners[0];
    return { left: c.x, top: c.y };
  }
  // beside the ring: past any panel the target sits in (the agents column), on the side with the most room
  const box = avoid.filter((r) => inside(t, r));
  const rx = Math.max(t.x + t.w, ...box.map((r) => r.x + r.w)),
    lx = Math.min(t.x, ...box.map((r) => r.x));
  const sides: Array<[number, Rect]> = [
    [(W - rx) * H, clamp(rx + GAP, t.y)],
    [lx * H, clamp(lx - GAP - cw, t.y)],
    [(H - t.y - t.h) * W, clamp(t.x, t.y + t.h + GAP)],
    [t.y * W, clamp(t.x, t.y - GAP - cardH)],
  ];
  sides.sort((a, b) => b[0] - a[0]);
  const c = pick(sides.map((x) => x[1])) || pick(corners) || sides[0][1];
  return { left: c.x, top: c.y };
}

/** The scene for `placeMapCard`, read from the Map root: the stage, the tree's
 * box on it (treeCtl keeps `data-tree` current) and the HUD panels. */
export function mapScene(root: HTMLElement | null): MapScene | null {
  const st = root ? root.querySelector<HTMLElement>(".ct-stage") : null;
  if (!root || !st) return null;
  const r0 = root.getBoundingClientRect();
  const rel = (r: DOMRect): Rect => ({ x: r.left - r0.left, y: r.top - r0.top, w: r.width, h: r.height });
  const area = rel(st.getBoundingClientRect());
  // "x0 y0 x1 y1;…" in stage px: the whole picture first, then its parts (clipped to the stage)
  const boxes: Rect[] = [];
  for (const one of (st.dataset.tree || "").split(";")) {
    const b = one.split(" ").map(Number);
    if (b.length !== 4 || !b.every(Number.isFinite)) continue;
    const x0 = Math.max(0, b[0]),
      y0 = Math.max(0, b[1]),
      x1 = Math.min(area.w, b[2]),
      y1 = Math.min(area.h, b[3]);
    if (x1 > x0 && y1 > y0) boxes.push({ x: area.x + x0, y: area.y + y0, w: x1 - x0, h: y1 - y0 });
  }
  const avoid = Array.from(st.querySelectorAll<HTMLElement>("[data-hud]"))
    .map((el) => rel(el.getBoundingClientRect()))
    .filter((r) => r.w > 2 && r.h > 2);
  return { area, tree: boxes[0] || null, parts: boxes.slice(1), avoid };
}
